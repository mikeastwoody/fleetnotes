"""Terminal output for the CLI: ``ok`` to stdout, ``warn`` and ``fail`` to stderr.

Colour is off when NO_COLOR is set (https://no-color.org/) or when the
stream is not a terminal.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


def _color(stream: TextIO, code: str) -> str:
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return ""
    return code


def _line(stream: TextIO, code: str, tag: str, msg: str) -> None:
    on, off = _color(stream, code), _color(stream, "\033[0m")
    print(f"  {on}{tag}{off}  {msg}", file=stream, flush=True)


def ok(msg: str) -> None:
    _line(sys.stdout, "\033[32m", "ok", msg)


def warn(msg: str) -> None:
    _line(sys.stderr, "\033[33m", "warn", msg)


def fail(msg: str) -> None:
    _line(sys.stderr, "\033[31m", "FAIL", msg)


def log(tag: str, msg: str, when: str) -> None:
    """One line of the fleet watcher's log: time, a three-letter tag, the message."""
    code = {"OK": "\033[32m", "PUB": "\033[36m", "WRN": "\033[33m", "ERR": "\033[31m"}.get(tag, "")
    dim, off = _color(sys.stdout, "\033[2m"), _color(sys.stdout, "\033[0m")
    print(f"  {dim}{when}{off}  {_color(sys.stdout, code)}[{tag:<3}]{off}  {msg}", flush=True)
