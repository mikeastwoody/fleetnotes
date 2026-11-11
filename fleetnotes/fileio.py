"""Atomic, locked files shared by the CLI and the fleet watcher.

Two things keep those files whole:

- every write goes to a temp file of its own (``mkstemp``: unique per
  process and thread) and is then moved over the target with ``os.replace``,
  so a reader (the overlay included) sees the old file or the new one, never
  a mix;
- ``file_lock(path)`` (``fcntl.flock`` on ``<path>.lock``) serialises a
  read-modify-write, so one process cannot overwrite what another one just
  saved.

POSIX only (macOS and Linux): ``fcntl`` does not exist on Windows.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from typing import Iterator

PRIVATE = 0o600
"""Rules file, salt, lock: only the owner reads them."""

PUBLIC = 0o644
"""The two JSON files the overlay reads: public by design."""


@contextlib.contextmanager
def file_lock(path: str) -> Iterator[None]:
    """Hold an exclusive lock for ``path`` (on ``<path>.lock``) across processes."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, PRIVATE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def atomic_write_text(path: str, text: str, mode: int = PRIVATE) -> None:
    """Write ``text`` to ``path`` through a temp file of this process, with ``mode``."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_public_json(path: str, data: dict) -> None:
    """Write one of the overlay's JSON files (world-readable), creating its folder."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_text(path, json.dumps(data), mode=PUBLIC)


def read_json(path: str) -> dict | None:
    """Return the JSON object in ``path``, or None if it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None
