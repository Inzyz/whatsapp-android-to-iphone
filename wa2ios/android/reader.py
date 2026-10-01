"""Read chats and messages out of a decrypted Android msgstore.db.

Only the modern schema (WhatsApp 2021 and later: tables `message`, `chat`, `jid`) is
supported. Every optional table and column is looked up first, so a newer or older
WhatsApp build that lacks one degrades gracefully instead of crashing.

Message-type codes follow published decoders (IPED, WhatsApp-Chat-Exporter) and the
evidence in amberkeep's format notes.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from wa2ios.model import Chat, Kind, Location, Media, Message
from wa2ios.util import Wa2iosError

# Android message_type -> what it is.
ANDROID_KINDS: dict[int, Kind] = {
    0: Kind.TEXT,
    1: Kind.IMAGE,
    2: Kind.AUDIO,  # becomes VOICE when origin == 1
    3: Kind.VIDEO,
    4: Kind.CONTACT,
    5: Kind.LOCATION,
    7: Kind.SYSTEM,
    9: Kind.DOCUMENT,
    10: Kind.CALL,
    11: Kind.CALL,
    13: Kind.GIF,
    14: Kind.CONTACT,  # several cards
    15: Kind.DELETED,
    16: Kind.LOCATION,  # live location
    20: Kind.STICKER,
    42: Kind.VIEW_ONCE,
    43: Kind.VIEW_ONCE,
    64: Kind.DELETED,  # deleted by a group admin
    66: Kind.POLL,
    82: Kind.VIEW_ONCE,
    90: Kind.CALL,
    99: Kind.SYSTEM,  # album container; the photos themselves are separate messages
    106: Kind.POLL,
}

# Only these kinds of address are real conversations that exist on an iPhone too.
CHAT_SERVERS = {"s.whatsapp.net", "lid", "g.us"}


@dataclass
class Jid:
    user: str
    server: str

    def __str__(self) -> str:
        return f"{self.user}@{self.server}"


@dataclass
class Summary:
    chats: int = 0
    groups: int = 0
    messages: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    kinds: Counter = field(default_factory=Counter)
    skipped_chats: Counter = field(default_factory=Counter)


class AndroidStore:
    def __init__(self, path: Path):
        if not path.is_file():
            raise Wa2iosError(f"{path} does not exist.")
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise Wa2iosError(f"{path} is not a decrypted database. Run `wa2ios decrypt` first.")
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        self.db = sqlite3.connect(uri, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.text_factory = lambda b: b.decode("utf-8", errors="replace")

        self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for needed in ("message", "chat", "jid"):
            if needed not in self.tables:
                raise Wa2iosError(
                    f"This msgstore.db has no '{needed}' table. It is either not a WhatsApp database or "
                    "from a very old WhatsApp version. Update WhatsApp on the Android phone and back up again."
                )
        self._cols: dict[str, set[str]] = {}
        self._load_identities()

    # ------------------------------------------------------------------ schema
    def cols(self, table: str) -> set[str]:
        if table not in self._cols:
            if table not in self.tables:
                self._cols[table] = set()
            else:
                self._cols[table] = {r[1] for r in self.db.execute(f"PRAGMA table_info('{table}')")}
        return self._cols[table]

    def _pick(self, table: str, alias: str, *wanted: str) -> str:
        """SELECT fragment for optional columns: missing ones become NULL."""
        have = self.cols(table)
        parts = []
        for name in wanted:
            parts.append(f"{alias}.{name} AS {alias}_{name}" if name in have else f"NULL AS {alias}_{name}")
        return ", ".join(parts)

    # -------------------------------------------------------------- identities
    def _load_identities(self) -> None:
        self.jids: dict[int, Jid] = {}
        for r in self.db.execute("SELECT _id, user, server FROM jid"):
            if r["user"] is None or r["server"] is None:
                continue
            user = str(r["user"]).split(":", 1)[0]  # strip any linked-device suffix
            self.jids[r["_id"]] = Jid(user, str(r["server"]))

        # Hidden identities (@lid) and the phone number behind them, both directions.
        self.lid_to_phone: dict[int, int] = {}
        self.phone_to_lid: dict[int, int] = {}
        if {"lid_row_id", "jid_row_id"} <= self.cols("jid_map"):
            for r in self.db.execute("SELECT lid_row_id, jid_row_id FROM jid_map"):
                self.lid_to_phone[r[0]] = r[1]
                self.phone_to_lid.setdefault(r[1], r[0])

        # The name somebody chose for themselves ("push name"), keyed by @lid row.
        self.push_names: dict[int, str] = {}
        if {"lid_row_id", "display_name"} <= self.cols("lid_display_name"):
            for r in self.db.execute("SELECT lid_row_id, display_name FROM lid_display_name"):
                if r[1]:
                    self.push_names[r[0]] = r[1]

    def address(self, jid_row: int | None) -> str | None:
        """The address to use on the iPhone: the phone number if we know it, otherwise as is."""
        if not jid_row or jid_row not in self.jids:
            return None
        if self.jids[jid_row].server == "lid" and jid_row in self.lid_to_phone:
            mapped = self.lid_to_phone[jid_row]
            if mapped in self.jids:
                return str(self.jids[mapped])
        return str(self.jids[jid_row])

    def push_name(self, jid_row: int | None) -> str | None:
        if not jid_row:
            return None
        if jid_row in self.push_names:
            return self.push_names[jid_row]
        lid = self.phone_to_lid.get(jid_row)
        return self.push_names.get(lid) if lid else None

    # ------------------------------------------------------------------ chats
    def chats(self, summary: Summary | None = None) -> list[Chat]:
        c = self.cols("chat")
        sel = ["_id", "jid_row_id"] + [x for x in ("subject", "archived", "created_timestamp", "hidden") if x in c]
        out: list[Chat] = []
        for r in self.db.execute(f"SELECT {', '.join(sel)} FROM chat"):
            jid = self.jids.get(r["jid_row_id"])
            if jid is None:
                continue
            if jid.server not in CHAT_SERVERS or jid.user in ("status", "0"):
                if summary is not None:
                    summary.skipped_chats[jid.server] += 1
                continue
            primary = self.address(r["jid_row_id"])
            alts = {str(jid)}
            lid = self.phone_to_lid.get(r["jid_row_id"])
            if lid and lid in self.jids:
                alts.add(str(self.jids[lid]))
            alts.discard(primary)
            is_group = jid.server == "g.us"
            out.append(
                Chat(
                    row_id=r["_id"],
                    jid=primary,
                    alt_jids=sorted(alts),
                    is_group=is_group,
                    subject=r["subject"] if "subject" in r.keys() else None,
                    display_name=None if is_group else self.push_name(r["jid_row_id"]),
                    archived=bool(r["archived"]) if "archived" in r.keys() and r["archived"] else False,
                    created_ms=r["created_timestamp"] if "created_timestamp" in r.keys() else None,
                )
            )
        return out

    # --------------------------------------------------------------- messages
    def messages(self, chat: Chat) -> Iterator[Message]:
        mc = self.cols("message")
        msel = ", ".join(
            f"m.{x}" if x in mc else f"NULL AS {x}"
            for x in ("_id", "key_id", "from_me", "timestamp", "message_type", "text_data",
                      "sender_jid_row_id", "starred", "origin")
        )
        joins, extra = [], []
        if "message_media" in self.tables:
            joins.append("LEFT JOIN message_media mm ON mm.message_row_id = m._id")
            extra.append(self._pick("message_media", "mm", "file_path", "mime_type", "media_name",
                                    "media_caption", "file_size", "file_length", "media_duration",
                                    "width", "height"))
        if "message_location" in self.tables:
            joins.append("LEFT JOIN message_location ml ON ml.message_row_id = m._id")
            extra.append(self._pick("message_location", "ml", "latitude", "longitude",
                                    "place_name", "place_address"))
        if "message_vcard" in self.tables and "message_row_id" in self.cols("message_vcard"):
            extra.append(
                "(SELECT group_concat(vcard, '\x1e') FROM message_vcard v WHERE v.message_row_id = m._id) AS vcards"
            )
        query = (
            f"SELECT {msel}{', ' if extra else ''}{', '.join(extra)} FROM message m {' '.join(joins)} "
            "WHERE m.chat_row_id = ? ORDER BY m.timestamp, m._id"
        )
        for r in self.db.execute(query, (chat.row_id,)):
            yield self._to_message(r, chat)

    def _to_message(self, r: sqlite3.Row, chat: Chat) -> Message:
        keys = r.keys()
        get = lambda k: r[k] if k in keys else None  # noqa: E731
        atype = get("message_type")
        kind = ANDROID_KINDS.get(atype, Kind.UNKNOWN) if atype is not None else Kind.UNKNOWN
        if kind is Kind.AUDIO and get("origin") == 1:
            kind = Kind.VOICE

        from_me = bool(get("from_me"))
        sender_jid = sender_name = None
        if chat.is_group and not from_me:
            sender_row = get("sender_jid_row_id")
            sender_jid = self.address(sender_row)
            sender_name = self.push_name(sender_row)

        media = None
        if get("mm_file_path") or get("mm_mime_type") or get("mm_media_name"):
            media = Media(
                file_path=get("mm_file_path"),
                mime_type=get("mm_mime_type"),
                name=get("mm_media_name"),
                caption=get("mm_media_caption"),
                size=get("mm_file_size") or get("mm_file_length"),
                duration=get("mm_media_duration"),
                width=get("mm_width"),
                height=get("mm_height"),
            )

        location = None
        if get("ml_latitude") is not None or get("ml_longitude") is not None:
            location = Location(get("ml_latitude"), get("ml_longitude"), get("ml_place_name"), get("ml_place_address"))

        vcard_name = None
        if get("vcards"):
            names = [m.group(1).strip() for m in re.finditer(r"^FN[^:]*:(.*)$", get("vcards"), re.MULTILINE)]
            vcard_name = ", ".join(n for n in names if n) or None

        text = get("text_data")
        if not text and media is not None and media.caption:
            text = media.caption

        return Message(
            row_id=get("_id"),
            key_id=get("key_id"),
            from_me=from_me,
            timestamp_ms=get("timestamp"),
            kind=kind,
            android_type=atype,
            text=text,
            sender_jid=sender_jid,
            sender_name=sender_name,
            starred=bool(get("starred")),
            media=media,
            location=location,
            vcard_name=vcard_name,
        )

    # ---------------------------------------------------------------- summary
    def summarise(self) -> Summary:
        s = Summary()
        for chat in self.chats(s):
            s.chats += 1
            s.groups += chat.is_group
            for m in self.messages(chat):
                s.messages += 1
                s.kinds[m.kind.value] += 1
                if m.timestamp_ms and m.timestamp_ms > 0:
                    s.first_ms = m.timestamp_ms if s.first_ms is None else min(s.first_ms, m.timestamp_ms)
                    s.last_ms = m.timestamp_ms if s.last_ms is None else max(s.last_ms, m.timestamp_ms)
        return s
