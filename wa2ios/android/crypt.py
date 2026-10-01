"""Decrypt an Android WhatsApp msgstore.db.crypt15 with your own 64-digit key.

The heavy lifting is done by wa-crypt-tools (GPL-3.0), which runs entirely offline.
"""

from __future__ import annotations

import io
import re
import zlib
from pathlib import Path

from wa2ios.util import Wa2iosError

SQLITE_HEADER = b"SQLite format 3\x00"


def normalise_key(key: str) -> str:
    """Accept the key as WhatsApp shows it: 64 hex digits, with any spaces or dashes."""
    cleaned = re.sub(r"[\s\-]", "", key).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise Wa2iosError(
            "The key must be the 64-digit end-to-end backup key WhatsApp showed you "
            f"(64 characters of 0-9 and a-f); got {len(cleaned)} usable characters."
        )
    return cleaned


def _load_key(key: str):
    from wa_crypt_tools.lib.key.keyfactory import KeyFactory

    path = Path(key)
    if path.is_file():
        text = path.read_bytes()
        # A text file holding the hex key, or WhatsApp's own encrypted_backup.key file.
        try:
            return KeyFactory.from_hex(normalise_key(text.decode("ascii")))
        except (UnicodeDecodeError, Wa2iosError):
            loaded = KeyFactory.from_file(path)
            if loaded is None:
                raise Wa2iosError(f"{path} is neither a 64-digit key nor an encrypted_backup.key file.")
            return loaded
    return KeyFactory.from_hex(normalise_key(key))


def decrypt(key: str, encrypted: Path, output: Path) -> Path:
    """Decrypt encrypted -> output (a plain SQLite msgstore.db). Returns output."""
    from wa_crypt_tools.lib.db.db15 import Database15
    from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory

    if not encrypted.is_file():
        raise Wa2iosError(f"{encrypted} does not exist.")
    k = _load_key(key)

    raw = encrypted.read_bytes()
    stream = io.BufferedReader(io.BytesIO(raw))
    db = DatabaseFactory.from_file(stream)
    if db is None:
        raise Wa2iosError(f"{encrypted.name} is not a WhatsApp backup this tool understands.")
    if not isinstance(db, Database15):
        raise Wa2iosError(
            f"{encrypted.name} is a crypt12/crypt14 backup. Those need a key hidden inside the phone. "
            "In WhatsApp turn on Settings > Chats > Chat backup > End-to-end encrypted backup, choose "
            "'Use 64-digit encryption key', back up again, and use the new msgstore.db.crypt15."
        )

    try:
        decrypted = db.decrypt(k, stream.read())
    except ValueError as e:
        raise Wa2iosError(f"Decryption failed ({e}). Is this the right 64-digit key for this backup?") from e

    try:
        plain = zlib.decompressobj().decompress(decrypted)
    except zlib.error:
        plain = bytes(decrypted)
    if not plain.startswith(SQLITE_HEADER):
        raise Wa2iosError("Decryption produced garbage: the 64-digit key does not match this backup.")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plain)
    return output
