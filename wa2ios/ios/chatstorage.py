"""Write Android messages into an iPhone WhatsApp ChatStorage.sqlite (a Core Data store).

Rules this follows:
  * Only columns the store actually has are written (checked with PRAGMA table_info),
    so a different WhatsApp version is written in its own shape.
  * Core Data's own primary-key counters (Z_PRIMARYKEY) are used and advanced, never
    SQLite's rowid, otherwise WhatsApp would later reuse an id and overwrite a row.
  * Values whose meaning is undocumented (ZFLAGS, ZMESSAGESTATUS, spotlight codes...)
    are copied from the most common value in the store's own existing messages, with
    fallbacks observed on real 2026 stores.
  * Existing iPhone messages are never changed except ZSORT (display order) and the
    "last message" pointer of a conversation that receives new messages.
  * Messages already present (same WhatsApp message id) are skipped, so running it on
    a store that already has some of the history does not duplicate anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from wa2ios.util import Wa2iosError

# iPhone ZMESSAGETYPE codes (see docs/HOW-IT-WORKS.md).
IOS_TEXT = 0
IOS_IMAGE = 1
IOS_VIDEO = 2
IOS_AUDIO = 3
IOS_DOCUMENT = 8
IOS_GIF = 11
IOS_STICKER = 15

SESSION_PRIVATE = 0
SESSION_GROUP = 1

REQUIRED_TABLES = ("ZWAMESSAGE", "ZWACHATSESSION", "Z_PRIMARYKEY")


@dataclass
class Conventions:
    out_flags: int = 16777280
    out_status: int = 6
    out_event: int = 0
    in_flags: int = 16777216
    in_status: int = 13
    in_event: int = 2
    msg_spotlight: int = -32768
    session_flags: int = 272
    spotlight_private: int = -5
    spotlight_group: int = 1
    sampled: int = 0


@dataclass
class WriteStats:
    sessions_created: int = 0
    sessions_merged: int = 0
    messages_added: int = 0
    duplicates_skipped: int = 0
    media_items: int = 0
    members_created: int = 0
    touched_sessions: set = field(default_factory=set)


class ChatStorage:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        check = self.db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise Wa2iosError(f"The iPhone's ChatStorage.sqlite is damaged ({check}).")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise Wa2iosError(
                f"The iPhone's WhatsApp store does not look as expected (missing {', '.join(missing)}). "
                "Is WhatsApp installed and activated on the iPhone?"
            )
        self.tables = tables
        self.columns: dict[str, set[str]] = {
            t: {r[1] for r in self.db.execute(f"PRAGMA table_info('{t}')")} for t in tables if t.startswith("Z")
        }
        self._load_entities()
        self.conv = self._sample()
        self.stats = WriteStats()
        self._members: dict[tuple[int, str], int] = {}
        self._sessions = {
            r["ZCONTACTJID"]: r["Z_PK"]
            for r in self.db.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION WHERE ZCONTACTJID IS NOT NULL")
        }
        self.db.execute("BEGIN")

    # ---------------------------------------------------------------- Core Data
    def _load_entities(self) -> None:
        self.ent: dict[str, int] = {}
        self.max_pk: dict[str, int] = {}
        for r in self.db.execute("SELECT Z_ENT, Z_NAME, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME IS NOT NULL"):
            self.ent[r["Z_NAME"]] = r["Z_ENT"]
            self.max_pk[r["Z_NAME"]] = r["Z_MAX"] or 0
        for needed in ("WAMessage", "WAChatSession"):
            if needed not in self.ent:
                raise Wa2iosError(f"The iPhone's WhatsApp store has no {needed} entity; unsupported version.")
        # Never trust the counter blindly: if rows exist above it, start after them.
        for name in self.ent:
            table = "Z" + name.upper()
            if table in self.tables:
                highest = self.db.execute(f"SELECT max(Z_PK) FROM {table}").fetchone()[0]
                if highest and highest > self.max_pk[name]:
                    self.max_pk[name] = highest

    def _next(self, entity: str) -> int:
        self.max_pk[entity] += 1
        return self.max_pk[entity]

    def _insert(self, table: str, values: dict) -> None:
        have = self.columns.get(table, set())
        names = sorted(k for k in values if k in have)
        sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
        self.db.execute(sql, [values[n] for n in names])

    def _sample(self) -> Conventions:
        c = Conventions()

        def commonest(table: str, column: str, where: str):
            if column not in self.columns.get(table, set()):
                return None
            row = self.db.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {where} "
                "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

        picks = {
            "out_flags": ("ZWAMESSAGE", "ZFLAGS", "ZMESSAGETYPE = 0 AND ZISFROMME = 1"),
            "out_status": ("ZWAMESSAGE", "ZMESSAGESTATUS", "ZMESSAGETYPE = 0 AND ZISFROMME = 1"),
            "out_event": ("ZWAMESSAGE", "ZGROUPEVENTTYPE", "ZMESSAGETYPE = 0 AND ZISFROMME = 1"),
            "in_flags": ("ZWAMESSAGE", "ZFLAGS", "ZMESSAGETYPE = 0 AND ZISFROMME = 0"),
            "in_status": ("ZWAMESSAGE", "ZMESSAGESTATUS", "ZMESSAGETYPE = 0 AND ZISFROMME = 0"),
            "in_event": ("ZWAMESSAGE", "ZGROUPEVENTTYPE", "ZMESSAGETYPE = 0 AND ZISFROMME = 0"),
            "msg_spotlight": ("ZWAMESSAGE", "ZSPOTLIGHTSTATUS", "ZMESSAGETYPE = 0"),
            "session_flags": ("ZWACHATSESSION", "ZFLAGS", "ZSESSIONTYPE IN (0, 1)"),
            "spotlight_private": ("ZWACHATSESSION", "ZSPOTLIGHTSTATUS", "ZSESSIONTYPE = 0"),
            "spotlight_group": ("ZWACHATSESSION", "ZSPOTLIGHTSTATUS", "ZSESSIONTYPE = 1"),
        }
        for attr, (table, column, where) in picks.items():
            value = commonest(table, column, where)
            if isinstance(value, int):
                setattr(c, attr, value)
                c.sampled += 1
        return c

    # ---------------------------------------------------------------- sessions
    def find_session(self, jids: list[str]) -> int | None:
        for j in jids:
            if j in self._sessions:
                return self._sessions[j]
        return None

    def existing_ids(self, session: int) -> set[str]:
        return {
            r[0]
            for r in self.db.execute(
                "SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND ZSTANZAID IS NOT NULL", (session,)
            )
        }

    def create_session(self, jid: str, title: str, is_group: bool, archived: bool, created: float | None) -> int:
        pk = self._next("WAChatSession")
        group_info = None
        if is_group and "WAGroupInfo" in self.ent and "ZWAGROUPINFO" in self.tables:
            group_info = self._next("WAGroupInfo")
            self._insert("ZWAGROUPINFO", {
                "Z_PK": group_info, "Z_ENT": self.ent["WAGroupInfo"], "Z_OPT": 1,
                "ZSTATE": 0, "ZCHATSESSION": pk, "ZCREATIONDATE": created,
            })
        self._insert("ZWACHATSESSION", {
            "Z_PK": pk, "Z_ENT": self.ent["WAChatSession"], "Z_OPT": 1,
            "ZARCHIVED": int(archived), "ZCONTACTABID": 0, "ZFLAGS": self.conv.session_flags,
            "ZHIDDEN": 0, "ZIDENTITYVERIFICATIONEPOCH": 0, "ZIDENTITYVERIFICATIONSTATE": 0,
            "ZMESSAGECOUNTER": 0, "ZREMOVED": 0,
            "ZSESSIONTYPE": SESSION_GROUP if is_group else SESSION_PRIVATE,
            "ZSPOTLIGHTSTATUS": self.conv.spotlight_group if is_group else self.conv.spotlight_private,
            "ZUNREADCOUNT": 0, "ZGROUPINFO": group_info, "ZCONTACTJID": jid, "ZPARTNERNAME": title,
        })
        self._sessions[jid] = pk
        self.stats.sessions_created += 1
        return pk

    def member(self, session: int, jid: str, name: str | None) -> int | None:
        if "ZWAGROUPMEMBER" not in self.tables or "WAGroupMember" not in self.ent:
            return None
        key = (session, jid)
        if key in self._members:
            return self._members[key]
        row = self.db.execute(
            "SELECT Z_PK FROM ZWAGROUPMEMBER WHERE ZCHATSESSION = ? AND ZMEMBERJID = ?", (session, jid)
        ).fetchone()
        if row:
            self._members[key] = row[0]
            return row[0]
        pk = self._next("WAGroupMember")
        self._insert("ZWAGROUPMEMBER", {
            "Z_PK": pk, "Z_ENT": self.ent["WAGroupMember"], "Z_OPT": 1,
            "ZCONTACTABID": 0, "ZISACTIVE": 1, "ZISADMIN": 0, "ZCHATSESSION": session,
            "ZCONTACTNAME": name or "", "ZMEMBERJID": jid,
        })
        self._members[key] = pk
        self.stats.members_created += 1
        return pk

    # ---------------------------------------------------------------- messages
    def add_message(
        self, *, session: int, chat_jid: str, is_group: bool, from_me: bool, date: float | None,
        msg_type: int, text: str | None, stanza_id: str | None, starred: bool,
        sender_jid: str | None, sender_name: str | None, media: dict | None = None,
    ) -> int:
        pk = self._next("WAMessage")
        member = None
        if is_group and not from_me and sender_jid:
            member = self.member(session, sender_jid, sender_name)
        media_pk = None
        if media is not None and "ZWAMEDIAITEM" in self.tables and "WAMediaItem" in self.ent:
            media_pk = self._next("WAMediaItem")
            row = {"Z_PK": media_pk, "Z_ENT": self.ent["WAMediaItem"], "Z_OPT": 1, "ZMESSAGE": pk}
            row.update(media)
            self._insert("ZWAMEDIAITEM", row)
            self.stats.media_items += 1
        c = self.conv
        self._insert("ZWAMESSAGE", {
            "Z_PK": pk, "Z_ENT": self.ent["WAMessage"], "Z_OPT": 1,
            "ZCHILDMESSAGESDELIVEREDCOUNT": 0, "ZCHILDMESSAGESPLAYEDCOUNT": 0, "ZCHILDMESSAGESREADCOUNT": 0,
            "ZDATAITEMVERSION": 3, "ZDOCID": 0, "ZENCRETRYCOUNT": 0, "ZFILTEREDRECIPIENTCOUNT": 0,
            "ZFLAGS": c.out_flags if from_me else c.in_flags,
            "ZGROUPEVENTTYPE": c.out_event if from_me else c.in_event,
            "ZISFROMME": int(from_me), "ZMESSAGEERRORSTATUS": 0,
            "ZMESSAGESTATUS": c.out_status if from_me else c.in_status,
            "ZMESSAGETYPE": msg_type, "ZSORT": 0, "ZSPOTLIGHTSTATUS": c.msg_spotlight,
            "ZSTARRED": int(starred), "ZCHATSESSION": session, "ZGROUPMEMBER": member,
            "ZMEDIAITEM": media_pk, "ZMESSAGEDATE": date, "ZSENTDATE": date,
            # Outgoing: says who it went to. Incoming: who it came from (the group, in a group).
            "ZFROMJID": None if from_me else chat_jid,
            "ZTOJID": chat_jid if from_me else None,
            "ZPUSHNAME": sender_name if (not from_me and sender_name) else None,
            "ZSTANZAID": stanza_id, "ZTEXT": text,
        })
        self.stats.messages_added += 1
        self.stats.touched_sessions.add(session)
        return pk

    # ---------------------------------------------------------------- finishing
    def _fix_session(self, session: int) -> None:
        """Renumber ZSORT 1..N by date and point the conversation at its newest message."""
        rows = self.db.execute(
            "SELECT Z_PK FROM ZWAMESSAGE WHERE ZCHATSESSION = ? ORDER BY ZMESSAGEDATE, Z_PK", (session,)
        ).fetchall()
        self.db.executemany("UPDATE ZWAMESSAGE SET ZSORT = ? WHERE Z_PK = ?",
                            [(i + 1, r[0]) for i, r in enumerate(rows)])
        if not rows:
            return
        last = self.db.execute(
            "SELECT Z_PK, ZMESSAGEDATE, ZTEXT FROM ZWAMESSAGE WHERE Z_PK = ?", (rows[-1][0],)
        ).fetchone()
        preview = last["ZTEXT"][:200] if last["ZTEXT"] else None
        sets = {"ZLASTMESSAGE": last["Z_PK"], "ZLASTMESSAGEDATE": last["ZMESSAGEDATE"],
                "ZLASTMESSAGETEXT": preview}
        have = self.columns["ZWACHATSESSION"]
        assignments = [f"{k} = ?" for k in sets if k in have]
        params = [v for k, v in sets.items() if k in have]
        if "ZMESSAGECOUNTER" in have:
            assignments.append("ZMESSAGECOUNTER = max(?, coalesce(ZMESSAGECOUNTER, 0))")
            params.append(len(rows) + 1)
        if assignments:
            self.db.execute(f"UPDATE ZWACHATSESSION SET {', '.join(assignments)} WHERE Z_PK = ?", params + [session])
        if "ZLASTSESSION" in self.columns["ZWAMESSAGE"]:
            self.db.execute(
                "UPDATE ZWAMESSAGE SET ZLASTSESSION = NULL WHERE ZCHATSESSION = ? AND ZLASTSESSION = ? AND Z_PK != ?",
                (session, session, last["Z_PK"]),
            )
            self.db.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION = ? WHERE Z_PK = ?", (session, last["Z_PK"]))

    def finish(self) -> None:
        for session in sorted(self.stats.touched_sessions):
            self._fix_session(session)
        for name, highest in self.max_pk.items():
            self.db.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = ?", (highest, name))
        self.db.commit()
        # Fold the write-ahead log into the file: only the main file goes back into the backup.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        self.db.close()
        for side in ("-wal", "-shm"):
            p = Path(str(self.path) + side)
            if p.exists():
                p.unlink()
        if check != "ok":
            raise Wa2iosError(f"The merged ChatStorage.sqlite failed its integrity check: {check}")

    def abort(self) -> None:
        try:
            self.db.rollback()
        finally:
            self.db.close()
