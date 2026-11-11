"""Captain's notes, public-text checks and the private rules file.

Two public JSON files feed the overlay (fleetnotes.html). Both live in the
output folder, which ``fleetnotes serve`` serves over HTTP, so they only ever
hold public text:

    notes.json  the captain's own notes, written by ``fleetnotes add|pin|rm|...``
                (contract fleetnotes-notes.v1)
    fleet.json  what the Firstmate crewmates are doing, written by
                ``fleetnotes fleet watch`` (contract fleetnotes-feed.v1,
                built in fleetnotes.fleet)

The privacy rules live in a private file (``rules_file``, mode 600 because
it names private projects and task ids). Without ``project`` lines no fleet
note is published::

    # allowlist: project folder -> public name; approved label of one task
    # (what it does; without one, the task title is cleaned and used);
    # blocked words (a regex, any case). Comments only on lines of their own.
    project   lighthouse         "Lighthouse"
    label     lh-ticker-notes    "notes that update themselves"
    block     secretproject|client-name

Any line it cannot read exactly (a value without quotes, text after the
closing quote, a ``block`` with an inline comment) is an error, and while an
error remains nothing of the fleet is published and manual notes are refused.

One blocklist (built-in rules, the config's ``blocked_phrases`` and the
rules file's ``block`` lines) applies to every public text: project names,
labels and manual notes. A blocked or malformed fleet text, or a label that
contains a task id, suppresses that note; a blocked manual note is refused.
Messages name the rule, never the text.

Usage:
    from fleetnotes.notes import NoteStore
    store = NoteStore.from_config()
    store.add_note("Today: notes that update themselves", pinned=True)
    rules = store.load_rules()
    rules.check_label("lh-ticker-notes")   # -> ("notes that update themselves", None)
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator

from fleetnotes.config import Config, load_config
from fleetnotes.fileio import PRIVATE, atomic_write_text, file_lock, read_json, write_public_json

MANUAL_SCHEMA = "fleetnotes-notes.v1"
FLEET_SCHEMA = "fleetnotes-feed.v1"
MANUAL_FILENAME = "notes.json"
FLEET_FILENAME = "fleet.json"

NOTE_MAX = 90
"""Width of a captain's note (pinned or temporary): about as long as the longest
fleet sentence (97 characters), so it reads in one pass of the overlay's ticker
(which shows any length whole). Emoji and other wide characters count double
(``text_width``)."""

PROJECT_MAX = 16
"""Characters of a public name: a project, or a remote machine ("Studio Mac")."""

LABEL_MAX = 60
"""Characters of what a task does: its approved label, or its cleaned title.
The overlay's ticker draws "who · what" whole on one line; the longest
sentence is a 34-character "who", " · " and 60 characters."""

DEFAULT_NOTE_MINUTES = 30
MAX_NOTE_MINUTES = 12 * 60
MAX_STORED_NOTES = 20

KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
"""Project folder names and task ids accepted in the rules file."""

RULES_HEADER = """\
# fleetnotes — privacy rules for the fleet feed (private: keep it out of git, mode 600).
# Without "project" lines no fleet note is published.
#   project <project folder> "<public name>"    (up to 16 characters)
#   label   <task id>        "<what it does>"   (up to 60 characters; without a
#                                                label the cleaned title is used)
#   block   <regular expression>                (blocked words, any case)
# Edit it with: fleetnotes fleet allow|deny|label|unlabel ...
"""


class PublicTextError(ValueError):
    """A text that may not be shown on stream (empty, too long, blocked...)."""


# ---------------------------------------------------------------------------
# Time helpers (UTC, ISO 8601 with Z, as both contracts use)
# ---------------------------------------------------------------------------

def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: object) -> float | None:
    """Epoch seconds of an ISO 8601 time with a zone, or None."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo is not None else None


# ---------------------------------------------------------------------------
# Public text: shape and blocklist
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """Text as the blocklist sees it: NFKC, casefolded, no invisible characters."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def text_width(text: str) -> int:
    """Characters as the overlay's line sees them: an emoji or a wide character counts 2."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def clean_public_text(text: str, max_len: int, *, allow_quotes: bool = True) -> str:
    """One line of visible text, at most ``max_len`` wide (``text_width``); raises PublicTextError."""
    if not isinstance(text, str):
        raise PublicTextError("is not text")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise PublicTextError("is empty")
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        raise PublicTextError("has control or invisible characters")
    if not allow_quotes and '"' in cleaned:
        raise PublicTextError('cannot contain quotes (")')
    width = text_width(cleaned)
    if width > max_len:
        if width == len(cleaned):
            raise PublicTextError(f"has {len(cleaned)} characters and {max_len} fit")
        raise PublicTextError(f"is {width} wide and {max_len} fit (each emoji or wide character counts 2)")
    return cleaned


BUILTIN_BLOCKS: tuple[tuple[str, str], ...] = (
    ("credentials", r"contrase[ñn]a|passw(or)?d|token|api[\s._-]?key|secret|\.env\b|keychain"
                    r"|credencial|credential|clave\s+de\s+(acceso|api|transmisi[oó]n)|stream\s*key"),
    ("money", r"€|\$\s?\d|\d\s?\$|\bprecios?\b|\bfacturas?\b|\bpresupuestos?\b|\bgastos?\b"
              r"|\bingresos?\b|\bventas?\b|\brevenue\b|\binvoices?\b|\bprices?\b|\bads\b|\banuncios?\b"),
    ("clients", r"\bclientes?\b|\bclients?\b|\bcustomers?\b"),
    ("paths", r"/users/|/home/|/private/|/volumes/|~/|\b[a-z]:\\"),
    ("emails", r"[\w.+-]+@[\w-]+\.\w"),
    ("links", r"://|\bwww\."),
)
"""Always on, whatever the rules file says: things a sticky note never needs.
English and Spanish words, so a bilingual stream is covered."""


@dataclass(frozen=True)
class BlockRule:
    origin: str  # "builtin", "phrases" (the config's blocked_phrases) or "local" (rules file)
    ref: str     # category, phrase number or line number: never the pattern itself
    regex: re.Pattern

    def describe(self, rules_name: str = "rules.conf") -> str:
        if self.origin == "builtin":
            return f"built-in rule '{self.ref}'"
        if self.origin == "phrases":
            return f"blocked_phrases entry {self.ref}"
        return f"line {self.ref} of {rules_name}"


class Blocklist:
    """Case-insensitive search of every rule in a folded text; first match wins."""

    def __init__(self, rules: Iterable[BlockRule]):
        self.rules = tuple(rules)

    def match(self, text: str) -> BlockRule | None:
        folded = fold(text)
        for rule in self.rules:
            if rule.regex.search(folded):
                return rule
        return None

    def __len__(self) -> int:
        return len(self.rules)


def builtin_rules() -> list[BlockRule]:
    return [BlockRule("builtin", name, re.compile(pattern, re.IGNORECASE))
            for name, pattern in BUILTIN_BLOCKS]


def phrase_rules(phrases: Iterable[str]) -> list[BlockRule]:
    """The config's ``blocked_phrases``, as case-insensitive substrings."""
    return [BlockRule("phrases", str(n), re.compile(re.escape(fold(p.strip())), re.IGNORECASE))
            for n, p in enumerate(phrases, 1) if isinstance(p, str) and p.strip()]


# ---------------------------------------------------------------------------
# Rules file
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"(project|label)\s+(\S+)\s+(.+)")
_QUOTED_RE = re.compile(r'"([^"]*)"')
_BLOCK_RE = re.compile(r"block\s+(.+)")
_INLINE_COMMENT_RE = re.compile(r"\s#")


_ID_SEPARATORS_RE = re.compile(r"[\s_.\-‐-―−]+")


def contains_id(text: str, task_id: str) -> bool:
    """True when ``text`` carries ``task_id`` (any case, any width, with or
    without its separators: "lh ticker notes" carries lh-ticker-notes)."""
    if len(task_id) < 4:
        return False
    bare = lambda s: _ID_SEPARATORS_RE.sub("", fold(s))
    return bare(task_id) in bare(text)


@dataclass
class Rules:
    """What may be published about the fleet, read from the rules file."""
    path: str
    exists: bool = False
    projects: dict[str, str] = field(default_factory=dict)  # project folder -> public name
    labels: dict[str, str] = field(default_factory=dict)    # task id -> public label
    local_blocks: list[BlockRule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)          # any error keeps publishing off
    blocklist: Blocklist = field(default_factory=lambda: Blocklist(builtin_rules()))

    @property
    def name(self) -> str:
        return os.path.join(os.path.basename(os.path.dirname(self.path)), os.path.basename(self.path))

    @property
    def publishing(self) -> bool:
        """True when fleet notes may be published at all."""
        return self.exists and not self.errors and bool(self.projects)

    def check_project(self, key: str) -> tuple[str | None, str | None]:
        """(public name, problem); (None, None) when the project is not allowed."""
        if key not in self.projects:
            return None, None
        return self._check(self.projects[key], PROJECT_MAX, "public name")

    def check_place(self, name: str) -> tuple[str | None, str | None]:
        """(public name, problem) of a remote machine ([[fleet.remotes]] name)."""
        return self._check(name, PROJECT_MAX, "machine name")

    def check_label(self, task_id: str) -> tuple[str | None, str | None]:
        """(public label, problem); (None, None) when the task has no label."""
        if task_id not in self.labels:
            return None, None
        label, problem = self._check(self.labels[task_id], LABEL_MAX, "label")
        if label is not None and contains_id(label, task_id):
            return None, "label with the task's id"
        return label, problem

    def _check(self, text: str, max_len: int, what: str) -> tuple[str | None, str | None]:
        try:
            text = clean_public_text(text, max_len, allow_quotes=False)
        except PublicTextError as e:
            return None, f"{what} not valid: {e}"
        rule = self.blocklist.match(text)
        if rule is not None:
            return None, f"{what} blocked by the {rule.describe(self.name)}"
        return text, None


def parse_rules(text: str, path: str, blocked_phrases: Iterable[str] = ()) -> Rules:
    rules = Rules(path=path, exists=True)
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        block = _BLOCK_RE.fullmatch(line)
        if block:
            if _INLINE_COMMENT_RE.search(block.group(1)):
                rules.errors.append(f"line {n}: 'block' followed by a comment; "
                                    "comments go on a line of their own")
                continue
            try:
                regex = re.compile(unicodedata.normalize("NFKC", block.group(1).strip()), re.IGNORECASE)
            except re.error:
                rules.errors.append(f"line {n}: the 'block' expression is not valid")
                continue
            if regex.search(""):
                rules.errors.append(f"line {n}: the 'block' expression matches any text")
                continue
            rules.local_blocks.append(BlockRule("local", str(n), regex))
            continue
        entry = _ENTRY_RE.fullmatch(line)
        if not entry:
            rules.errors.append(f"line {n}: not understood (expected project, label or block)")
            continue
        verb, key, value = entry.groups()
        if not KEY_RE.fullmatch(key):
            rules.errors.append(f"line {n}: '{verb}' has a name that is not valid")
            continue
        quoted = _QUOTED_RE.fullmatch(value.strip())
        if not quoted:
            rules.errors.append(f"line {n}: the text of '{verb}' goes in quotes with nothing after it "
                                "(comments go on a line of their own)")
            continue
        (rules.projects if verb == "project" else rules.labels)[key] = quoted.group(1)
    rules.blocklist = Blocklist([*builtin_rules(), *phrase_rules(blocked_phrases), *rules.local_blocks])
    return rules


def load_rules(path: str, blocked_phrases: Iterable[str] = ()) -> Rules:
    """Rules from ``path``; a missing file publishes nothing (empty allowlist)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return Rules(path=path, blocklist=Blocklist([*builtin_rules(), *phrase_rules(blocked_phrases)]))
    except (OSError, UnicodeDecodeError):
        rules = Rules(path=path, exists=True,
                      blocklist=Blocklist([*builtin_rules(), *phrase_rules(blocked_phrases)]))
        rules.errors.append("could not read the file")
        return rules
    return parse_rules(text, path, blocked_phrases)


def task_key(salt: bytes, task_id: str) -> str:
    """Opaque, stable key of a task: HMAC-SHA256 of its id with the local salt."""
    return hmac.new(salt, task_id.encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def _read_salt(path: str) -> bytes | None:
    try:
        with open(path, encoding="ascii") as f:
            value = bytes.fromhex(f.read().strip())
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return None
    return value if len(value) >= 16 else None


# ---------------------------------------------------------------------------
# Manual notes (notes.json)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManualNote:
    key: str
    text: str
    pinned: bool = False
    expires: str | None = None  # ISO UTC; None = until removed

    def expired(self, now: float) -> bool:
        if self.expires is None:
            return False
        ts = parse_iso(self.expires)
        return ts is None or ts <= now

    def to_json(self) -> dict:
        return {"key": self.key, "text": self.text, "pinned": self.pinned, "expires": self.expires}


def _notes_from_json(data: dict | None) -> tuple[bool, list[ManualNote]]:
    if not data or data.get("schema") != MANUAL_SCHEMA:
        return False, []
    notes = []
    for item in data.get("notes") or []:
        if not isinstance(item, dict):
            continue
        key, text, expires = item.get("key"), item.get("text"), item.get("expires")
        if not (isinstance(key, str) and isinstance(text, str) and text.strip()):
            continue
        notes.append(ManualNote(key, text, item.get("pinned") is True,
                                expires if isinstance(expires, str) else None))
    return data.get("off") is True, notes


def visible_manual(notes: list[ManualNote], now: float) -> list[ManualNote]:
    """What the overlay shows: the newest pinned note, then the newest temporary one."""
    live = [n for n in notes if not n.expired(now)]
    pinned = [n for n in live if n.pinned][-1:]
    temporary = [n for n in live if not n.pinned][-1:]
    return pinned + temporary


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class NoteStore:
    """Files of fleetnotes: manual notes, fleet feed, rules file and salt.

    ``output_dir`` holds the two public JSON files; ``state_dir`` (private)
    holds the lock, the salt of the task keys and the private copy of the
    ``off`` switch.
    """

    def __init__(self, output_dir: str, rules_path: str, state_dir: str,
                 blocked_phrases: Iterable[str] = ()):
        self.output_dir = output_dir
        self.rules_path = rules_path
        self.state_dir = state_dir
        self.blocked_phrases = tuple(blocked_phrases)
        self.manual_path = os.path.join(output_dir, MANUAL_FILENAME)
        self.feed_path = os.path.join(output_dir, FLEET_FILENAME)
        self.off_path = os.path.join(state_dir, "off")

    @classmethod
    def from_config(cls, cfg: Config | None = None, rules_path: str | None = None) -> "NoteStore":
        cfg = cfg or load_config()
        return cls(
            output_dir=cfg.output_dir,
            rules_path=rules_path or cfg.rules_file,
            state_dir=cfg.state_dir,
            blocked_phrases=cfg.blocked_phrases,
        )

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """One lock for every read-modify-write of these files (CLI and watcher)."""
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        with file_lock(os.path.join(self.state_dir, "fleetnotes")):
            yield

    # -- rules ------------------------------------------------------------

    def load_rules(self) -> Rules:
        return load_rules(self.rules_path, self.blocked_phrases)

    def check_text(self, text: str, max_len: int, *, allow_quotes: bool = True) -> str:
        """Clean ``text`` and refuse it if the blocklist or a broken rules file says so."""
        text = clean_public_text(text, max_len, allow_quotes=allow_quotes)
        rules = self.load_rules()
        if rules.errors:
            raise PublicTextError(f"{rules.name} has errors ({'; '.join(rules.errors)}); "
                                  "fix them before publishing anything")
        rule = rules.blocklist.match(text)
        if rule is not None:
            raise PublicTextError(f"matches the {rule.describe(rules.name)}")
        return text

    def allow_project(self, key: str, name: str) -> str:
        if not KEY_RE.fullmatch(key):
            raise PublicTextError("the project must be its folder name (letters, digits, . _ -)")
        name = self.check_text(name, PROJECT_MAX, allow_quotes=False)
        self._set_entry("project", key, f'project {key} "{name}"')
        return name

    def deny_project(self, key: str) -> bool:
        return self._set_entry("project", key, None)

    def set_label(self, task_id: str, text: str) -> str:
        if not KEY_RE.fullmatch(task_id):
            raise PublicTextError("a task id may only have letters, digits, . _ -")
        text = self.check_text(text, LABEL_MAX, allow_quotes=False)
        if contains_id(text, task_id):
            raise PublicTextError("the label cannot contain the task's id (it is private)")
        self._set_entry("label", task_id, f'label {task_id} "{text}"')
        return text

    def remove_label(self, task_id: str) -> bool:
        return self._set_entry("label", task_id, None)

    def _set_entry(self, verb: str, key: str, line: str | None) -> bool:
        """Replace (or with ``line=None`` remove) every ``verb key`` line. True if it changed."""
        with self.locked():
            try:
                with open(self.rules_path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                if line is None:
                    return False
                lines = RULES_HEADER.splitlines()
            out, found = [], False
            for raw in lines:
                entry = _ENTRY_RE.fullmatch(raw.strip())
                if entry and entry.group(1) == verb and entry.group(2) == key:
                    if line is not None and not found:
                        out.append(line)
                    found = True
                    continue
                out.append(raw)
            if line is not None and not found:
                out.append(line)
            if out == lines:
                return False
            os.makedirs(os.path.dirname(self.rules_path) or ".", exist_ok=True)
            atomic_write_text(self.rules_path, "\n".join(out) + "\n", mode=PRIVATE)
            return True

    # -- salt -------------------------------------------------------------

    def salt(self) -> bytes:
        """Local secret of the task keys (``<state_dir>/salt``, created once)."""
        path = os.path.join(self.state_dir, "salt")
        value = _read_salt(path)
        if value is None:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            atomic_write_text(tmp, secrets.token_hex(32) + "\n", mode=PRIVATE)
            try:
                os.link(tmp, path)  # create-if-absent: a concurrent first run keeps one salt
            except FileExistsError:
                if _read_salt(path) is None:  # an unreadable leftover: replace it
                    os.replace(tmp, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
            value = _read_salt(path)
        if value is None:
            raise OSError(f"could not create {path}")
        return value

    # -- manual notes -----------------------------------------------------

    def read_manual(self) -> tuple[bool, list[ManualNote]]:
        """(off, notes). Off when ``fleetnotes off`` is set in either copy of
        the switch, or when the public file is there but unreadable or foreign
        (fails closed); a missing file is (False, [])."""
        flag = os.path.exists(self.off_path)
        if not os.path.exists(self.manual_path):
            return flag, []
        data = read_json(self.manual_path)
        if data is None or data.get("schema") != MANUAL_SCHEMA:
            return True, []
        off, notes = _notes_from_json(data)
        return off or flag, notes

    def _write_manual(self, off: bool, notes: list[ManualNote], now: float) -> None:
        write_public_json(self.manual_path, {
            "schema": MANUAL_SCHEMA,
            "updated": iso_utc(now),
            "off": off,
            "notes": [n.to_json() for n in notes],
        })

    def add_note(self, text: str, minutes: float | None = DEFAULT_NOTE_MINUTES,
                 pinned: bool = False, now: float | None = None) -> ManualNote:
        """Add a note; a pinned one replaces the previous pinned note."""
        now = time.time() if now is None else now
        text = self.check_text(text, NOTE_MAX)
        if minutes is not None and not (1 <= minutes <= MAX_NOTE_MINUTES):
            raise PublicTextError(f"a note lasts from 1 min to {MAX_NOTE_MINUTES // 60} h")
        with self.locked():
            off, notes = self.read_manual()
            notes = [n for n in notes if not n.expired(now) and not (pinned and n.pinned)]
            used = {n.key for n in notes}
            key = secrets.token_hex(3)
            while key in used:
                key = secrets.token_hex(3)
            note = ManualNote(key, text, pinned,
                              iso_utc(now + minutes * 60) if minutes is not None else None)
            notes.append(note)
            temporary = [n for n in notes if not n.pinned]
            if len(notes) > MAX_STORED_NOTES and temporary:
                notes.remove(temporary[0])
            self._write_manual(off, notes, now)
        return note

    def remove_note(self, index: int, now: float | None = None) -> ManualNote:
        """Remove note number ``index`` (1-based, as ``list_notes`` numbers them)."""
        now = time.time() if now is None else now
        with self.locked():
            off, notes = self.read_manual()
            notes = [n for n in notes if not n.expired(now)]
            if not 1 <= index <= len(notes):
                raise IndexError(index)
            note = notes.pop(index - 1)
            self._write_manual(off, notes, now)
        return note

    def pinned_note(self, now: float | None = None) -> ManualNote | None:
        """The pinned note the overlay shows now (the newest live one), or None."""
        now = time.time() if now is None else now
        pinned = [n for n in self.list_notes(now) if n.pinned]
        return pinned[-1] if pinned else None

    def remove_pinned(self, now: float | None = None) -> int:
        """Remove every pinned note. Returns how many were live."""
        now = time.time() if now is None else now
        with self.locked():
            off, notes = self.read_manual()
            live = [n for n in notes if not n.expired(now)]
            kept = [n for n in live if not n.pinned]
            if len(kept) != len(notes):
                self._write_manual(off, kept, now)
        return len(live) - len(kept)

    def list_notes(self, now: float | None = None) -> list[ManualNote]:
        now = time.time() if now is None else now
        return [n for n in self.read_manual()[1] if not n.expired(now)]

    def clear_notes(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.locked():
            off, notes = self.read_manual()
            self._write_manual(off, [], now)
        return len([n for n in notes if not n.expired(now)])

    def set_off(self, off: bool, now: float | None = None) -> None:
        """``fleetnotes off``: the overlay draws nothing and the fleet feed is removed.

        Off removes the feed first, then sets the private switch (an empty
        file: creating it needs no free space) and only then rewrites the
        public file, so a failed write still leaves the watcher switched off.
        """
        now = time.time() if now is None else now
        with self.locked():
            _, notes = self.read_manual()
            notes = [n for n in notes if not n.expired(now)]
            if off:
                self.clear_feed()
                os.close(os.open(self.off_path, os.O_WRONLY | os.O_CREAT, PRIVATE))
                self._write_manual(True, notes, now)
            else:
                self._write_manual(False, notes, now)
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.off_path)

    # -- fleet feed -------------------------------------------------------

    def read_feed(self) -> dict | None:
        return read_json(self.feed_path)

    def write_feed(self, feed: dict) -> None:
        write_public_json(self.feed_path, feed)

    def clear_feed(self) -> bool:
        """Remove the public fleet feed. True if there was one."""
        try:
            os.remove(self.feed_path)
            return True
        except FileNotFoundError:
            return False
