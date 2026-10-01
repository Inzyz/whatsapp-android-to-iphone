"""Platform-neutral description of chats and messages read from Android."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    CONTACT = "contact"
    LOCATION = "location"
    DELETED = "deleted"
    POLL = "poll"
    VIEW_ONCE = "view_once"
    SYSTEM = "system"
    CALL = "call"
    UNKNOWN = "unknown"


# Kinds that carry a file which can be copied to the iPhone.
FILE_KINDS = {Kind.IMAGE, Kind.VIDEO, Kind.GIF, Kind.AUDIO, Kind.VOICE, Kind.DOCUMENT, Kind.STICKER}


@dataclass
class Media:
    file_path: str | None = None  # path as recorded on the Android phone, e.g. "Media/WhatsApp Images/x.jpg"
    mime_type: str | None = None
    name: str | None = None  # document file name
    caption: str | None = None
    size: int | None = None
    duration: int | None = None  # seconds
    width: int | None = None
    height: int | None = None


@dataclass
class Location:
    latitude: float | None
    longitude: float | None
    name: str | None = None
    address: str | None = None


@dataclass
class Message:
    row_id: int
    key_id: str | None  # WhatsApp's own message id, stable across devices
    from_me: bool
    timestamp_ms: int | None
    kind: Kind
    android_type: int | None
    text: str | None = None
    sender_jid: str | None = None  # group messages only: who wrote it
    sender_name: str | None = None  # their self-chosen WhatsApp name, if known
    starred: bool = False
    media: Media | None = None
    location: Location | None = None
    vcard_name: str | None = None


@dataclass
class Chat:
    row_id: int
    jid: str  # address used on the iPhone, e.g. "4915112345678@s.whatsapp.net" or "...@g.us"
    alt_jids: list[str] = field(default_factory=list)  # other addresses for the same chat (lid <-> phone)
    is_group: bool = False
    subject: str | None = None  # group name
    display_name: str | None = None  # best name for a 1:1 chat
    archived: bool = False
    created_ms: int | None = None

    def title(self) -> str:
        if self.is_group and self.subject:
            return self.subject
        if self.display_name:
            return self.display_name
        user = self.jid.split("@", 1)[0]
        return f"+{user}" if user.isdigit() else user
