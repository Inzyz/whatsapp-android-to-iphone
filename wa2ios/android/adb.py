"""Copy WhatsApp's backup files off an Android phone over USB, using adb.

Only shared storage is read (Android/media/com.whatsapp/...), which needs no root.
Nothing on the phone is changed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from wa2ios.util import Wa2iosError, info, step, warn

# Where WhatsApp keeps its local backup and media, newest layout first.
REMOTE_ROOTS = [
    "/sdcard/Android/media/com.whatsapp/WhatsApp",
    "/sdcard/WhatsApp",  # phones that last updated before Android 11
]


def find_adb(explicit: str | None = None) -> str:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise Wa2iosError(f"adb was not found at {explicit}")
    found = shutil.which("adb")
    if found:
        return found
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    candidates += [
        Path("C:/platform-tools/adb.exe"),
        Path.home() / "platform-tools" / "adb.exe",
        Path.home() / "Downloads" / "platform-tools" / "adb.exe",
        Path.cwd() / "platform-tools" / "adb.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise Wa2iosError(
        "adb was not found. Download 'SDK Platform-Tools for Windows' from "
        "https://developer.android.com/tools/releases/platform-tools , unzip it, and either "
        "add the folder to PATH or pass --adb C:\\path\\to\\platform-tools\\adb.exe.\n"
        "Alternatively, copy the files by hand (see README) and skip this step."
    )


def _run(adb: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([adb, *args], capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        raise Wa2iosError(f"adb {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc


def _single_device(adb: str) -> str:
    out = _run(adb, "devices").stdout.splitlines()[1:]
    devices = [line.split() for line in out if line.strip()]
    ready = [d[0] for d in devices if len(d) > 1 and d[1] == "device"]
    if any(len(d) > 1 and d[1] == "unauthorized" for d in devices):
        raise Wa2iosError(
            "The phone is connected but has not allowed this computer yet. Unlock the phone and "
            "tap 'Allow' on the 'Allow USB debugging?' prompt, then run this again."
        )
    if not ready:
        raise Wa2iosError(
            "No Android phone found. Connect it by USB, turn on Developer options > USB debugging, "
            "and set the USB mode to 'File transfer'."
        )
    if len(ready) > 1:
        raise Wa2iosError(f"More than one Android device is connected ({', '.join(ready)}). Unplug the others.")
    return ready[0]


def _remote_exists(adb: str, serial: str, path: str) -> bool:
    proc = _run(adb, "-s", serial, "shell", f'ls -d "{path}"', check=False)
    return proc.returncode == 0 and "No such file" not in proc.stdout + proc.stderr


def pull(out_dir: Path, adb_path: str | None = None, with_media: bool = True) -> Path:
    """Copy WhatsApp/Databases (and optionally WhatsApp/Media) into out_dir/WhatsApp."""
    adb = find_adb(adb_path)
    serial = _single_device(adb)
    info(f"Using adb at {adb}, phone {serial}")

    root = next((r for r in REMOTE_ROOTS if _remote_exists(adb, serial, f"{r}/Databases")), None)
    if root is None:
        raise Wa2iosError(
            "Could not find WhatsApp's Databases folder on the phone. Open WhatsApp > Settings > Chats > "
            "Chat backup and tap 'Back up' first (with end-to-end encryption and the 64-digit key)."
        )

    local_root = out_dir / "WhatsApp"
    local_root.mkdir(parents=True, exist_ok=True)

    step(f"Copying {root}/Databases from the phone")
    _run(adb, "-s", serial, "pull", f"{root}/Databases", str(local_root))
    db = local_root / "Databases" / "msgstore.db.crypt15"
    if not db.is_file():
        found = sorted((local_root / "Databases").glob("msgstore*.crypt*"))
        names = ", ".join(p.name for p in found) or "nothing"
        raise Wa2iosError(
            f"msgstore.db.crypt15 was not on the phone (found: {names}). If you only see .crypt14 files, "
            "turn on end-to-end encrypted backup with a 64-digit key in WhatsApp and back up again."
        )

    if with_media:
        if _remote_exists(adb, serial, f"{root}/Media"):
            step(f"Copying {root}/Media from the phone (this can take a long time)")
            _run(adb, "-s", serial, "pull", f"{root}/Media", str(local_root))
        else:
            warn("No WhatsApp Media folder on the phone; only messages will be available.")

    info(f"Done. Files are in {local_root}")
    return local_root
