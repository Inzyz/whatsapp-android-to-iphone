"""Small helpers shared by the Android and iPhone sides."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Core Data (iPhone) counts seconds from 2001-01-01 UTC; Android counts
# milliseconds from 1970-01-01 UTC. This is the gap between the two epochs.
APPLE_EPOCH_OFFSET = 978307200


class Wa2iosError(Exception):
    """An error with a message meant for the person running the tool."""


def android_ms_to_coredata(ms: int | None) -> float | None:
    if not ms or ms <= 0:
        return None
    return ms / 1000.0 - APPLE_EPOCH_OFFSET


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def make_console_safe() -> None:
    """Windows consoles may not be able to print every character; never crash on it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def folder_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def ask_yes_no(question: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
