"""Public projection of the Firstmate fleet for the fleetnotes overlay.

``build_feed(snapshot, rules, salt, extra=...)`` turns Firstmate's read-only
fleet view (``bin/fm-fleet-snapshot.sh --json``, schema fm-fleet-snapshot.v1)
of the main home, of each local secondmate home and of each remote machine
into the public feed ``fleet.json``, contract fleetnotes-feed.v1::

    {"schema": "fleetnotes-feed.v1", "generated": "2026-11-04T12:39:50Z",
     "summary": {"running": 4, "validating": 1, "needs_captain": 1,
                 "problem": 0, "paused": 1, "done_recent": 0},
     "notes": [{"key": "3f9c0a1b7e", "project": "Driftwood",
                "who": "Secondmate of Driftwood", "label": "Slack integration",
                "label_from": "title", "kind": "ship", "state": "validating",
                "step": "ci", "started": "2026-11-04T12:22:00Z",
                "changed": "2026-11-04T12:38:00Z"}]}

The overlay draws each note as one sentence, "who · what" ("Lighthouse ·
the chat bot answers questions", "Secondmate of Driftwood · Slack
integration", "Studio Mac: Harbor · new store screenshots"), or
"who · a crewmate at work" when ``label`` is null.

Only this leaves the module:
  - closed vocabulary: ``kind`` (ship, scout), ``state``, ``step``,
    ``label_from`` (label, title);
  - counts and UTC times: ``summary``, ``started`` (the crewmate's spawn),
    ``changed`` (its last status event);
  - ``project``, ``who``: the allowlisted public name of the project, alone or
    with "Secondmate of" or a remote machine's public name before it;
  - ``label``: what the task does, either the approved label of the rules
    file or the task's backlog title after ``public_title`` cleaned it
    (prefix, asides, ids, paths, links, emails and blocked words removed,
    cut at a word boundary), checked against the blocklist on every build;
  - ``key``: HMAC of the task id with a local salt (opaque, stable).
Task ids, backlog notes, hold reasons, status-log text, details, paths, PRs,
secondmate ids and ssh hosts are read only to decide, never copied.

Rules, deterministic, no model:
  - only ship and scout tasks count (a secondmate row stands for a whole
    home, whose own tasks come from reading that home);
  - unknown or stale state (``freshness`` other than "fresh"): no note, not counted;
  - project not allowed, or its public name (or the machine's) blocked: only
    counted, anonymously;
  - blocked or malformed label, or a public text carrying any task id: no note
    (the task is still counted);
  - a title with nothing public left after cleaning: ``label: null``;
  - the main snapshot without a valid ``generated`` time is refused, never
    re-dated; another source without one is skipped and counted as unread;
  - done: shown for 10 minutes after its last status event, then dropped.

The fleet is read only through the view's process (locally, or over ssh on a
remote machine); this module never opens a file of Firstmate's data/ or state/.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

from fleetnotes.config import FLEET_MAX_SECONDS, FleetConfig, Remote
from fleetnotes.notes import (
    FLEET_SCHEMA, LABEL_MAX, PROJECT_MAX, KEY_RE, NoteStore, Rules,
    contains_id, fold, iso_utc, parse_iso, task_key, text_width,
)

SNAPSHOT_SCHEMA = "fm-fleet-snapshot.v1"
SNAPSHOT_VIEW = os.path.join("bin", "fm-fleet-snapshot.sh")

KINDS = ("ship", "scout")
STATE_ORDER = ("needs_captain", "problem", "done", "validating", "running", "paused")
SUMMARY_KEYS = ("running", "validating", "needs_captain", "problem", "paused", "done_recent")
STEPS = ("review", "test", "lint", "docs", "push", "pr", "ci", "rebase")
LABEL_FROM = ("label", "title")
FEED_KEYS = frozenset({"schema", "generated", "summary", "notes"})
NOTE_KEYS = frozenset({"key", "project", "who", "label", "label_from", "kind", "state", "step",
                       "position", "total", "started", "changed"})
REQUIRED_NOTE_KEYS = frozenset({"key", "project", "who", "label", "label_from", "kind", "state",
                                "started", "changed"})

# A fixed form of the contract, never translated here: the overlay with ?lang=es shows
# "Secondmate of <project>" as "El secondmate de <project>" (docs/contracts.md).
SECONDMATE_PREFIX = "Secondmate of "
WHO_MAX = max(len(SECONDMATE_PREFIX) + PROJECT_MAX, PROJECT_MAX + len(": ") + PROJECT_MAX)  # 34
IDLE_TEXT = "a crewmate at work"

DONE_VISIBLE_SECONDS = 10 * 60
MAX_FEED_NOTES = 10
MAX_SECONDMATES = 8
STALE_SECONDS = 180  # the overlay hides fleet notes from an older feed
SSH_CONNECT_TIMEOUT = 5
# The view keeps cached copies of its remote secondmates' summaries under
# state/; pointing that cache at a path that can never be created keeps the
# reads of secondmate homes and remote Macs free of writes.
NO_CACHE_DIR = "/var/empty/fleetnotes-no-cache/cache"
_MAX_FUTURE_SKEW = 120
_MAX_AGE = 30 * 24 * 3600
_SPAWN_GEN_RE = re.compile(r"s(\d{9,11})\..+")
_KEY_HEX_RE = re.compile(r"[0-9a-f]{10}")

ORIGINS = ("main", "secondmate", "remote")

# Why a task got a note or not: `fleetnotes fleet plan` and the watcher's log print
# these, with counts; only `plan --ids` pairs them with task ids.
COUNTED = "counted only"
HIDDEN = "neither shown nor counted"
PUBLISHED = "published"
PRIVATE_PROJECT = f"{COUNTED}: project not allowed"
BLOCKED_PROJECT = f"{COUNTED}: public name blocked or not valid"
BLOCKED_LABEL = f"{COUNTED}: label blocked or not valid"
NO_ID = f"{COUNTED}: no usable id"
TRIMMED = f"{COUNTED}: does not fit in the feed"
DONE_OLD = f"{HIDDEN}: done more than 10 min ago"
UNKNOWN_STATE = f"{HIDDEN}: unknown state"
OTHER_KIND = f"{HIDDEN}: not a ship or scout task"

PHRASES = {"running": "running", "validating": "validating", "needs_captain": "waiting on the captain",
           "problem": "stuck", "paused": "paused", "done": "done!"}


class SnapshotError(Exception):
    """The fleet view could not be read. Messages never carry fleet content."""


@dataclass(frozen=True)
class Decision:
    task_id: str      # private: only `plan --ids` shows it; never logged or published
    reason: str
    detail: str = ""  # which rule decided, never the blocked text
    source: str = ""  # private too (a secondmate id or an ssh host): only `plan --ids`


@dataclass(frozen=True)
class Source:
    """One fleet view read for the feed, other than the main home's."""
    origin: str               # "secondmate" or "remote"
    snapshot: dict
    tag: str                  # private: the secondmate id or the ssh host
    place: str | None = None  # a remote machine's configured public name


@dataclass
class SourceStatus:
    """How reading one fleet view went, for `fleetnotes fleet plan` and the log."""
    origin: str
    tag: str                  # private (secondmate id, ssh host)
    place: str | None = None  # public (a remote machine's name)
    error: str | None = None  # SnapshotError text: never fleet content


@dataclass
class FleetRead:
    main: dict
    extra: list[Source] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)

    def unread(self) -> Counter:
        return Counter(s.origin for s in self.statuses if s.error)


@dataclass
class FeedResult:
    feed: dict
    decisions: list[Decision] = field(default_factory=list)
    read: Counter = field(default_factory=Counter)    # origin -> views used
    unread: Counter = field(default_factory=Counter)  # origin -> views not read or refused

    def counts(self) -> Counter:
        return Counter(d.reason for d in self.decisions)


# ---------------------------------------------------------------------------
# Snapshot -> public feed
# ---------------------------------------------------------------------------

def public_state(task: dict) -> tuple[str | None, str | None]:
    """(state, step) in the feed's vocabulary, or (None, None) when unknown.

    An open decision or a parked run waits on the captain, blocked or failed is
    a problem; a working crewmate whose state comes from a validation run step
    is validating.
    """
    current = task.get("current_state")
    if not isinstance(current, dict) or current.get("freshness") != "fresh":
        return None, None
    state = current.get("state")
    if state not in ("working", "parked", "done", "blocked", "paused", "failed"):
        return None, None
    hints = task.get("hints") if isinstance(task.get("hints"), dict) else {}
    if hints.get("pending_decision") is True or state == "parked":
        return "needs_captain", None
    if hints.get("blocked_event") is True or state in ("blocked", "failed"):
        return "problem", None
    if state in ("done", "paused"):
        return state, None
    if current.get("source") == "run-step":
        detail = current.get("detail")
        return "validating", ("ci" if isinstance(detail, str) and detail.startswith("ci running") else None)
    return "running", None


def _plausible(ts: float | None, generated: float) -> float | None:
    if ts is None or not generated - _MAX_AGE <= ts <= generated + _MAX_FUTURE_SKEW:
        return None
    return ts


def _started(task: dict, generated: float) -> float | None:
    """When this crewmate was spawned: spawn_gen is ``s<epoch>.<pid>.<random>``."""
    gen = task.get("spawn_gen")
    match = _SPAWN_GEN_RE.fullmatch(gen) if isinstance(gen, str) else None
    return _plausible(float(match.group(1)), generated) if match else None


def _changed(task: dict, generated: float) -> float | None:
    """When the task's last status event was written (generated - its age)."""
    paths = task.get("paths") if isinstance(task.get("paths"), dict) else {}
    log = paths.get("status_log") if isinstance(paths.get("status_log"), dict) else {}
    event = log.get("last_event") if isinstance(log.get("last_event"), dict) else {}
    age = event.get("age_seconds")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 <= age < _MAX_AGE:
        return None
    return _plausible(generated - age, generated)


def _project_key(task: dict) -> str:
    project = task.get("project")
    return os.path.basename(project.rstrip("/")) if isinstance(project, str) else ""


def _tasks(snapshot: dict) -> list[dict]:
    tasks = snapshot.get("tasks")
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def _backlog(snapshot: dict) -> list[dict]:
    backlog = snapshot.get("backlog") if isinstance(snapshot.get("backlog"), dict) else {}
    records = backlog.get("records")
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _titles(snapshot: dict) -> dict[str, str]:
    """Task id -> backlog title (private: only public_title's output leaves)."""
    return {r["id"]: r["title"] for r in _backlog(snapshot)
            if isinstance(r.get("id"), str) and isinstance(r.get("title"), str)}


def _ids(snapshot: dict) -> set[str]:
    """Every task id the view names (tasks and backlog rows): none may be published."""
    return {r["id"] for r in (*_tasks(snapshot), *_backlog(snapshot))
            if isinstance(r.get("id"), str) and len(r["id"]) >= 4}


def _generated(snapshot: object, now: float) -> float | None:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        return None
    generated = parse_iso(snapshot.get("generated"))
    if generated is None or generated > now + _MAX_FUTURE_SKEW:
        return None
    return generated


def who_for(origin: str, project: str, place: str | None = None) -> str:
    """Who does the work, as the stream says it."""
    if origin == "secondmate":
        return SECONDMATE_PREFIX + project
    if origin == "remote" and place:
        return f"{place}: {project}"
    return project


# -- task titles -> public "what" --------------------------------------------

_ASIDE_RE = re.compile(r"\s*(\([^()]*\)|\[[^\[\]]*\])")
_UNCLOSED_RE = re.compile(r"\s*[(\[].*$")
_MARKER_RE = re.compile(r"(SHIP|SCOUT)\b[\s:·-]*")
_PREFIX_RE = re.compile(r"([^:]{1,32}):\s+(\S.*)")
_EDGE = "«»\"'“”‘’()[]{}<>,.;:!?¡¿…*_`"
_DROP_TOKEN_RES = (
    re.compile(r"://|^www\.", re.IGNORECASE),                       # links
    re.compile(r"@"),                                                # emails, user@host
    re.compile(r"^(/|~|\.\.?/)|\\|/[^/]*/"),                        # paths
    re.compile(r"^[a-z0-9_.-]*[a-z][a-z0-9_.-]*/[a-z0-9_./-]+$"),    # relative paths: src/fleet
    re.compile(r"^(?!\d+(\.\d+)*$)[\w-]+(\.[\w-]+)+$"),              # file names, domains, hosts (not 1.5.0)
    re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$"),                   # IPv4
    re.compile(r"^(#\d+|[A-Za-z]{1,6}#\d+|[A-Za-z]{1,6}-\d+)$"),    # tickets, PRs: ABC-123, #12
    re.compile(r"^(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}$", re.IGNORECASE),  # hashes
    re.compile(r"^[a-z0-9]+(-[a-z0-9]+){2,}$"),                      # slugs: lh-ticker-notes
    re.compile(r"^\w+_\w+$"),                                        # snake_case identifiers
)
_STOPWORDS = frozenset(
    "a al ante bajo como con contra cuando de del desde donde e el en entre hacia hasta la las "
    "le les lo los o para pero por que se según sin sobre su sus tras u un una y "
    "and as at for from in of on or the to with".split())


def _drop_token(token: str, ids: Iterable[str]) -> bool:
    core = token.strip(_EDGE)
    if not core:
        return not any(ch.isalnum() for ch in token) and token not in ("—", "–", "-", "·", "/", "+", "&")
    return (any(r.search(core) for r in _DROP_TOKEN_RES)
            or any(contains_id(core, task_id) for task_id in ids))


def _names_any(folded: str, words: Iterable[str]) -> bool:
    """True when the folded text has one of ``words`` as a whole word."""
    return any(re.search(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", folded) for w in words)


def _trim_tail(words: list[str]) -> list[str]:
    """No dangling connector or separator at the end ("... of", "... —")."""
    while words and (words[-1].strip(_EDGE + "—–-·/+&").casefold() in _STOPWORDS
                     or not any(ch.isalnum() for ch in words[-1])):
        words = words[:-1]
    if words:
        words[-1] = words[-1].rstrip(",;:—–-·")
    return words


def public_title(title: object, rules: Rules, ids: Iterable[str],
                 max_len: int = LABEL_MAX, private: Iterable[str] = ()) -> str | None:
    """What a task does, from its backlog title, fit to publish; or None.

    Drops bracketed and parenthetical asides (and anything after one left
    open), a SHIP/SCOUT marker, a leading "Project:" prefix, and every word
    that is an id (any fleet task id, even written with spaces; tickets,
    hashes, slugs, snake_case), a path, a file name, a domain or host, an IP,
    a link or an email; then cuts at a word boundary to ``max_len`` wide
    ("…" when it cut). Nothing is published (None) when a block rule matches
    anywhere in what is left, when it names one of the ``private`` words (the
    folders of projects that are not allowed, ssh hosts), or when fewer than
    two words that are not connectors remain: like a label, a title is
    published whole or not at all.
    """
    if not isinstance(title, str):
        return None
    ids = tuple(ids)
    text = "".join("" if unicodedata.category(ch) == "Cf"
                   else " " if unicodedata.category(ch).startswith(("C", "Z")) else ch for ch in title)
    text = " ".join(text.split())
    while True:  # nested asides go from the inside out
        shorter = _ASIDE_RE.sub("", text)
        if shorter == text:
            break
        text = shorter
    text = " ".join(_UNCLOSED_RE.sub("", text).split())
    text = _MARKER_RE.sub("", text, count=1) if _MARKER_RE.match(text) else text
    prefix = _PREFIX_RE.fullmatch(text)
    if prefix:
        text = prefix.group(2)
    words = [w.replace('"', "") for w in text.split()]
    words = [w for w in words if w and not _drop_token(w, ids)]
    # Separators left alone by a removal: none at the start or twice in a row.
    words = [w for i, w in enumerate(words)
             if any(ch.isalnum() for ch in w) or (i > 0 and any(ch.isalnum() for ch in words[i - 1]))]
    while words and not any(ch.isalnum() for ch in words[0]):
        words = words[1:]
    words = _trim_tail(words)
    text = " ".join(words)
    if text_width(text) > max_len:
        kept: list[str] = []
        for word in words:
            if text_width(" ".join([*kept, word])) > max_len - 1:
                break
            kept.append(word)
        kept = _trim_tail(kept)
        while " ".join(kept).count("«") > " ".join(kept).count("»"):  # no quote left open
            kept = _trim_tail(kept[:max(i for i, w in enumerate(kept) if "«" in w)])
        text = " ".join(kept) + "…" if kept else ""
    content = [w for w in text.split() if any(ch.isalpha() for ch in w)
               and w.strip(_EDGE + "—–-·").casefold() not in _STOPWORDS]
    if len(content) < 2:  # "the stream one" or a lone word says nothing: no text rather than noise
        return None
    if (rules.blocklist.match(text) is not None or any(contains_id(text, i) for i in ids)
            or _names_any(fold(text), private)):
        return None  # a client's name next to "client", a figure next to "price": drop it whole
    return text


# -- the feed -----------------------------------------------------------------

def build_feed(snapshot: dict, rules: Rules, salt: bytes, now: float | None = None,
               extra: Iterable[Source] = (), unread: Counter | None = None) -> FeedResult:
    """Public feed for the main ``snapshot`` and the ``extra`` views under ``rules``.

    Raises SnapshotError when the main snapshot has a foreign schema or a
    ``generated`` time that is missing or in the future: the feed's age is
    what lets the overlay hide a stale fleet, so it is never made up. An extra
    view like that is skipped and counted in ``unread``.
    """
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotError("the fleet view does not have the expected schema")
    generated = _generated(snapshot, now)
    if generated is None:
        raise SnapshotError("the fleet view has no valid reading time")

    read = Counter({"main": 1})
    unread = Counter(unread or {})
    views: list[tuple[str, str, str | None, dict, float]] = [("main", "", None, snapshot, generated)]
    for source in extra:
        when = _generated(source.snapshot, now)
        if when is None or source.origin not in ("secondmate", "remote"):
            unread[source.origin] += 1
            continue
        read[source.origin] += 1
        views.append((source.origin, source.tag, source.place, source.snapshot, when))

    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    decisions: list[Decision] = []
    candidates: list[tuple[dict, float, int]] = []  # (note, changed, decision index)
    # No published text may carry a task id, from whichever home, whoever approved it.
    task_ids = set().union(*(_ids(view[3]) for view in views))
    # Nor may a title name a project that is not allowed, or an ssh host.
    private = {fold(key) for view in views for task in _tasks(view[3])
               if (key := _project_key(task)) and len(key) >= 3 and key not in rules.projects}
    for origin, tag, _, _, _ in views:
        if origin == "remote" and tag:  # "user@host.example": user@host.example, user, host
            user, _, host = fold(tag).rpartition("@")
            private.update(p for p in (fold(tag), user, host.split(".")[0]) if len(p) >= 3)
    used_keys: set[str] = set()

    def carries_id(text: str | None) -> bool:
        return text is not None and any(contains_id(text, other) for other in task_ids)

    for origin, tag, place, view, view_generated in views:
        titles = _titles(view)
        source = "" if origin == "main" else f"{origin} {tag}"
        for task in _tasks(view):
            task_id = task.get("id") if isinstance(task.get("id"), str) else ""

            def decide(reason: str, detail: str = "") -> None:
                decisions.append(Decision(task_id, reason, detail, source))

            kind = task.get("kind")
            if kind not in KINDS:
                decide(OTHER_KIND)
                continue
            state, step = public_state(task)
            if state is None:
                decide(UNKNOWN_STATE)
                continue
            changed = _changed(task, view_generated)
            if state == "done":
                if changed is None or view_generated - changed > DONE_VISIBLE_SECONDS:
                    decide(DONE_OLD)
                    continue
                summary["done_recent"] += 1
            else:
                summary[state] += 1

            if not KEY_RE.fullmatch(task_id):
                decide(NO_ID)
                continue
            project, problem = rules.check_project(_project_key(task))
            if project is not None and origin == "remote":
                place_name, place_problem = rules.check_place(place or "")
                if place_name is None:
                    project, problem = None, place_problem
                else:
                    place = place_name
            who = who_for(origin, project, place) if project is not None else None
            if who is not None and carries_id(who):
                project, problem = None, "public name with a task's id"
            elif who is not None and (rule := rules.blocklist.match(who)) is not None:
                project, problem = None, f"'who' blocked by the {rule.describe(rules.name)}"
            if project is None:
                decide(BLOCKED_PROJECT if problem else PRIVATE_PROJECT, problem or "")
                continue
            label, problem = rules.check_label(task_id)
            if carries_id(label):
                problem = "label with a task's id"
            if problem:
                decide(BLOCKED_LABEL, problem)
                continue
            label_from = "label" if label is not None else None
            if label is None:
                label = public_title(titles.get(task_id), rules, task_ids, private=private)
                label_from = "title" if label is not None else None
            started = _started(task, view_generated)
            key = task_key(salt, task_id)
            if key in used_keys:  # the same id in two homes: still two opaque keys
                key = task_key(salt, f"{origin}\x00{tag}\x00{task_id}")
            used_keys.add(key)
            note = {"key": key, "project": project, "who": who, "label": label,
                    "label_from": label_from, "kind": kind, "state": state}
            if step:
                note["step"] = step
            note["started"] = iso_utc(started) if started is not None else None
            note["changed"] = iso_utc(changed) if changed is not None else None
            decide(PUBLISHED)
            candidates.append((note, changed or 0.0, len(decisions) - 1))

    candidates.sort(key=lambda c: (STATE_ORDER.index(c[0]["state"]), -c[1]))
    for _, _, index in candidates[MAX_FEED_NOTES:]:
        d = decisions[index]
        decisions[index] = Decision(d.task_id, TRIMMED, "", d.source)
    feed = {"schema": FLEET_SCHEMA, "generated": iso_utc(generated), "summary": summary,
            "notes": [note for note, _, _ in candidates[:MAX_FEED_NOTES]]}
    validate_feed(feed)
    return FeedResult(feed, decisions, read, unread)


def validate_feed(feed: object) -> None:
    """Raise ValueError unless ``feed`` holds only contract keys and public values."""
    def need(ok: bool, what: str) -> None:
        if not ok:
            raise ValueError(f"feed outside the contract: {what}")

    def text(value: object, top: int) -> bool:
        return isinstance(value, str) and 0 < len(value) <= top

    need(isinstance(feed, dict) and set(feed) == FEED_KEYS, "top-level keys")
    need(feed["schema"] == FLEET_SCHEMA, "schema")
    need(parse_iso(feed["generated"]) is not None, "generated")
    summary = feed["summary"]
    need(isinstance(summary, dict) and set(summary) == set(SUMMARY_KEYS), "summary keys")
    need(all(type(v) is int and v >= 0 for v in summary.values()), "summary values")
    need(isinstance(feed["notes"], list) and len(feed["notes"]) <= MAX_FEED_NOTES, "notes")
    keys = [note.get("key") for note in feed["notes"] if isinstance(note, dict)]
    need(len(keys) == len(set(keys)), "repeated keys")
    for note in feed["notes"]:
        need(isinstance(note, dict) and REQUIRED_NOTE_KEYS <= set(note) <= NOTE_KEYS, "keys of a note")
        need(isinstance(note["key"], str) and bool(_KEY_HEX_RE.fullmatch(note["key"])), "key")
        need(text(note["project"], PROJECT_MAX), "project")
        need(text(note["who"], WHO_MAX) and note["who"].endswith(note["project"]), "who")
        need(note["label"] is None or text(note["label"], LABEL_MAX), "label")
        need(note["label_from"] in ((None,) if note["label"] is None else LABEL_FROM), "label_from")
        need(note["kind"] in KINDS, "kind")
        need(note["state"] in STATE_ORDER, "state")
        need(note.get("step") is None or note["step"] in STEPS, "step")
        for key in ("position", "total"):
            need(note.get(key) is None or (type(note[key]) is int and note[key] >= 0), key)
        for key in ("started", "changed"):
            need(note[key] is None or parse_iso(note[key]) is not None, key)


def refilter_feed(feed: dict | None, rules: Rules, salt: bytes) -> dict | None:
    """Re-apply ``rules`` to a feed already published, without reading the fleet.

    It only takes away, so ``deny`` and ``unlabel`` reach the stream within one
    overlay poll: notes of a project no longer allowed (or whose "who" is now
    blocked) go, a label that is no longer the approved one and a title that
    is now blocked become null. It never adds or changes text: a new label, or
    the title that replaces a removed label, waits for the next build_feed,
    which also checks them against every task id of the fleet. Returns None
    when nothing may be published.
    """
    if not rules.publishing or feed is None:
        return None
    try:
        validate_feed(feed)
    except (ValueError, KeyError, TypeError):
        return None
    allowed = {name for key in rules.projects if (name := rules.check_project(key)[0])}
    approved = {task_key(salt, task_id): rules.check_label(task_id)[0] for task_id in rules.labels}
    notes = []
    for note in feed["notes"]:
        if note["project"] not in allowed or rules.blocklist.match(note["who"]) is not None:
            continue
        label = note["label"]
        if label is not None and note["label_from"] == "label":
            keep = approved.get(note["key"]) == label
        else:
            keep = label is not None and rules.blocklist.match(label) is None
        notes.append({**note, "label": label if keep else None, "label_from": note["label_from"] if keep else None})
    return {**feed, "notes": notes}


# ---------------------------------------------------------------------------
# Text for people (plan, watcher log): counts only
# ---------------------------------------------------------------------------

def header_text(summary: dict) -> str | None:
    """The overlay's header line ("FIRSTMATE · 7 crewmates · 1 waiting"), or None."""
    total = sum(summary.get(k, 0) for k in ("running", "validating", "needs_captain", "problem", "paused"))
    if not total:
        return None
    parts = [f"FIRSTMATE · {total} crewmate{'s' if total != 1 else ''}"]
    if summary.get("needs_captain"):
        n = summary["needs_captain"]
        parts.append(f"{n} waiting")
    if summary.get("problem"):
        n = summary["problem"]
        parts.append(f"{n} stuck")
    return " · ".join(parts)


def note_text(note: dict) -> tuple[str, str]:
    """(sentence, state) of a note as the overlay draws it."""
    sentence = f"{note.get('who') or note['project']} · {note['label'] or IDLE_TEXT}"
    return sentence, PHRASES.get(note["state"], note["state"])


def sources_text(read: Counter, unread: Counter) -> str:
    """"main + 3 secondmates + 1 remote" (counts, never ids or hosts)."""
    names = {"main": ("main", "main"), "secondmate": ("secondmate", "secondmates"),
             "remote": ("remote", "remotes")}

    def part(origin: str, n: int) -> str:
        one, many = names[origin]
        return one if origin == "main" else f"{n} {one if n == 1 else many}"

    used = " + ".join(part(o, read[o]) for o in ORIGINS if read.get(o))
    missing = ", ".join(part(o, unread[o]) for o in ORIGINS if unread.get(o))
    return (used or "none") + (f" (unread: {missing})" if missing else "")


def outcome_line(result: FeedResult) -> str:
    """One log line about a build: counts and the header, no names."""
    counts = result.counts()
    shown = counts.get(PUBLISHED, 0)
    parts = [f"{shown} note{'s' if shown != 1 else ''}"]
    counted = sum(n for reason, n in counts.items() if reason.startswith(COUNTED))
    if counted:
        parts.append(f"{counted} {COUNTED}")
    hidden = sum(n for reason, n in counts.items() if reason.startswith(HIDDEN))
    if hidden:
        parts.append(f"{hidden} {HIDDEN}")
    blocked = [d.detail for d in result.decisions if d.detail]
    if blocked:
        parts.append("suppressed: " + "; ".join(sorted(set(blocked))))
    parts.append(f"sources: {sources_text(result.read, result.unread)}")
    parts.append(f"header: {header_text(result.feed['summary']) or '(none)'}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Reading the fleet (Firstmate's read-only view, as a child process)
# ---------------------------------------------------------------------------

def _run_view(argv: list[str], env: dict, cwd: str | None, timeout: float,
              should_stop: Callable[[], bool] | None, failed_exit: Callable[[int], str]) -> dict:
    """Run one fleet view to completion and return its JSON object.

    The view runs in its own process group (killed whole on timeout or stop)
    and its stderr is discarded: it may quote private text. Raises SnapshotError.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
    except OSError as e:
        raise SnapshotError(f"could not start the fleet view ({e.__class__.__name__})") from None
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    failure = "read interrupted"
                elif time.monotonic() >= deadline:
                    failure = f"the view took longer than {timeout:g} s"
                if failure:
                    break
    except BaseException:  # Ctrl-C in `fleetnotes fleet plan`: leave no view running
        _kill_group(proc)
        raise
    if failure:
        _kill_group(proc)
        raise SnapshotError(failure)
    if proc.returncode != 0:
        raise SnapshotError(failed_exit(proc.returncode))
    try:
        data = json.loads(out)
    except ValueError:
        raise SnapshotError("the fleet view did not return JSON") from None
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotError("the fleet view does not have the expected schema")
    return data


def run_snapshot(firstmate_home: str, timeout: float = 45.0,
                 should_stop: Callable[[], bool] | None = None, fm_home: str | None = None) -> dict:
    """Run ``<firstmate_home>/bin/fm-fleet-snapshot.sh --json`` and return its object.

    ``fm_home`` (default: ``firstmate_home``) is the home it reads: a local
    secondmate home is read with the main home's view and that home as
    FM_HOME, and with the view's summary cache pointed nowhere so the read
    writes nothing in it. Every other FM_* variable is dropped (a shell
    override must not point the view elsewhere) and forge reads are off
    (FM_CREW_STATE_NO_FORGE=1). Raises SnapshotError.
    """
    home = os.path.expanduser(firstmate_home)
    view = os.path.join(home, SNAPSHOT_VIEW)
    if not os.access(view, os.X_OK):
        raise SnapshotError(f"{SNAPSHOT_VIEW} not found in fleet.firstmate_home")
    target = os.path.expanduser(fm_home) if fm_home else home
    env = {k: v for k, v in os.environ.items() if not k.startswith("FM_")}
    env.update(FM_HOME=target, FM_CREW_STATE_NO_FORGE="1")
    if target != home:
        env["FM_SNAPSHOT_CACHE_DIR"] = NO_CACHE_DIR
    return _run_view([view, "--json"], env, target, timeout, should_stop,
                     lambda code: f"the fleet view exited with code {code}")


def remote_command(home: str, timeout: float = 45.0) -> str:
    """The command run on a remote machine: its own view, with a clean environment.

    It runs under /bin/sh whatever the login shell is. ``env -i`` drops the
    login shell's FM_* overrides; PATH gains the usual Homebrew folders (for
    jq) for a non-login ssh shell, harmless where they do not exist; the
    summary cache points nowhere, so nothing is written in that machine's
    Firstmate home (the view's own scratch files under $TMPDIR are made and
    removed by it). ``perl -e alarm`` (perl ships with macOS and most Linux
    systems) ends the view on that machine once our read has given up, since
    closing ssh does not signal it.
    """
    view = shlex.quote(os.path.join(home, SNAPSHOT_VIEW))
    seconds = max(5, int(timeout) + 5)
    inner = ('exec env -i HOME="$HOME" PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" '
             'TMPDIR="${TMPDIR:-/tmp}" LANG=en_US.UTF-8 '
             f"FM_HOME={shlex.quote(home)} FM_CREW_STATE_NO_FORGE=1 FM_SNAPSHOT_CACHE_DIR={NO_CACHE_DIR} "
             f"perl -e 'alarm shift; exec @ARGV' {seconds} {view} --json")
    return f"exec /bin/sh -c {shlex.quote(inner)}"


def run_remote_snapshot(remote: Remote, timeout: float = 45.0,
                        should_stop: Callable[[], bool] | None = None) -> dict:
    """Run a remote machine's view over ssh (BatchMode: never a prompt). Raises SnapshotError."""
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "--", remote.host, remote_command(remote.home, timeout)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("FM_")}

    def failed(code: int) -> str:
        if code == 255:
            return "the remote machine does not answer over ssh"
        return f"the remote machine's fleet view exited with code {code}"
    return _run_view(argv, env, None, timeout, should_stop, failed)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.communicate(timeout=3)
            break
        except subprocess.TimeoutExpired:
            continue


def secondmate_homes(snapshot: dict) -> list[tuple[str, str]]:
    """(secondmate id, home) of every local secondmate home the main view registers.

    The view lists each registered secondmate as a ``kind: secondmate`` task
    whose ``paths.home.path`` is its home; a remote one (``remote`` or a
    ``host`` in secondmate_current) belongs to another machine and is skipped.
    """
    try:
        return _secondmate_homes(snapshot)
    except (TypeError, ValueError, OSError):  # a malformed view (a NUL in a path…) names no home
        return []


def _secondmate_homes(snapshot: dict) -> list[tuple[str, str]]:
    remote_ids = set()
    current = snapshot.get("secondmate_current") if isinstance(snapshot.get("secondmate_current"), dict) else {}
    records = current.get("records")
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and (record.get("remote") is True or record.get("host")):
            remote_ids.add(record.get("id"))
    main = os.path.realpath(snapshot["fm_home"]) if isinstance(snapshot.get("fm_home"), str) else None
    homes, seen = [], set()
    for task in _tasks(snapshot):
        task_id = task.get("id")
        if (task.get("kind") != "secondmate" or not isinstance(task_id, str)
                or not KEY_RE.fullmatch(task_id) or task_id in remote_ids):
            continue
        paths = task.get("paths") if isinstance(task.get("paths"), dict) else {}
        home = (paths.get("home") or {}).get("path") if isinstance(paths.get("home"), dict) else None
        if not isinstance(home, str) or not os.path.isabs(home) or not os.path.isdir(home):
            continue
        real = os.path.realpath(home)
        if real == main or real in seen:
            continue
        seen.add(real)
        homes.append((task_id, home))
    return homes[:MAX_SECONDMATES]


def read_fleet(firstmate_home: str, timeout: float = 45.0,
               should_stop: Callable[[], bool] | None = None,
               remotes: Iterable[Remote] = (),
               local: Callable[..., dict] = run_snapshot,
               remote: Callable[..., dict] = run_remote_snapshot) -> FleetRead:
    """Read the main home, then its local secondmate homes, and every remote machine.

    Everything shares one deadline (``timeout``): the remote machines start at
    once, the secondmate homes as soon as the main view names them. Only the
    main view is required (SnapshotError); another view that fails is left
    out and recorded in ``statuses``.
    """
    deadline = time.monotonic() + timeout
    aborted = threading.Event()

    def stop() -> bool:
        return aborted.is_set() or (should_stop is not None and should_stop())

    def left() -> float:
        return max(0.5, deadline - time.monotonic())

    remotes = list(remotes)
    with ThreadPoolExecutor(max_workers=MAX_SECONDMATES + max(1, len(remotes))) as pool:
        try:
            return _read_all(pool, firstmate_home, timeout, stop, left, remotes, local, remote)
        except BaseException:
            aborted.set()  # every other read sees it within 0.5 s and kills its view or ssh
            raise


def _read_all(pool: ThreadPoolExecutor, firstmate_home: str, timeout: float, stop: Callable[[], bool],
              left: Callable[[], float], remotes: list[Remote],
              local: Callable[..., dict], remote: Callable[..., dict]) -> FleetRead:
    """read_fleet's body: remote machines at once, the main home, then its secondmate homes."""
    remote_jobs = [(r, pool.submit(remote, r, timeout, stop)) for r in remotes]
    main = local(firstmate_home, timeout, stop)
    jobs = [("secondmate", sm_id, None, pool.submit(local, firstmate_home, left(), stop, fm_home=home))
            for sm_id, home in secondmate_homes(main)]
    jobs += [("remote", r.host, r.name, job) for r, job in remote_jobs]
    read = FleetRead(main)
    for origin, tag, place, job in jobs:
        try:
            snap = job.result()
        except SnapshotError as e:
            read.statuses.append(SourceStatus(origin, tag, place, str(e)))
            continue
        read.statuses.append(SourceStatus(origin, tag, place))
        read.extra.append(Source(origin, snap, tag, place))
    return read


# ---------------------------------------------------------------------------
# One refresh (the watcher's cycle)
# ---------------------------------------------------------------------------

def refresh_feed(store: NoteStore, firstmate_home: str, timeout: float = 45.0,
                 should_stop: Callable[[], bool] | None = None,
                 remotes: Iterable[Remote] = (),
                 read: Callable[..., FleetRead] = read_fleet) -> str:
    """Publish the current fleet feed, or remove it when nothing may be published.

    Returns one log line with counts only. A failed read of the main home
    keeps the last feed: the overlay hides fleet notes once it is older than
    STALE_SECONDS. A secondmate home or remote machine that cannot be read only
    drops out (and is counted in the line).
    """
    disabled = _disabled(store, store.load_rules())
    if disabled:  # a switched-off panel does not even read the fleet
        store.clear_feed()
        return disabled
    failed = f"Keeping the last feed; the overlay hides it after {STALE_SECONDS} s"
    try:
        fleet = read(firstmate_home, timeout, should_stop, remotes=remotes)  # slow: outside the lock
    except SnapshotError as e:
        return f"Could not read the fleet: {e}. {failed}"
    with store.locked():
        # Rules and switch as they are now: a `fleetnotes off`, `deny` or `unlabel`
        # that landed during the read wins.
        rules = store.load_rules()
        disabled = _disabled(store, rules)
        if disabled:
            store.clear_feed()
            return disabled
        try:
            result = build_feed(fleet.main, rules, store.salt(), extra=fleet.extra, unread=fleet.unread())
        except SnapshotError as e:
            return f"Could not read the fleet: {e}. {failed}"
        store.write_feed(result.feed)  # build_feed validated it against the contract
    return "Published: " + outcome_line(result)


def apply_rules_now(store: NoteStore) -> bool:
    """Re-filter the published feed with the current rules (CLI, after a change)."""
    with store.locked():
        current = store.read_feed()
        if current is None:
            return False
        rules = store.load_rules()
        feed = None if _disabled(store, rules) else refilter_feed(current, rules, store.salt())
        if feed is None:
            store.clear_feed()
            return True
        if feed != current:
            validate_feed(feed)
            store.write_feed(feed)
            return True
        return False


def _disabled(store: NoteStore, rules: Rules) -> str | None:
    """Why nothing may be published now, or None."""
    if store.read_manual()[0]:
        return "Switched off with 'fleetnotes off': publishing nothing"
    if rules.errors:
        lines = ", ".join(e.split(":")[0] for e in rules.errors)
        return f"{rules.name} has errors ({lines}): publishing nothing until they are fixed"
    if not rules.publishing:
        return f"No allowed projects in {rules.name}: publishing nothing"
    return None


# ---------------------------------------------------------------------------
# The watcher (`fleetnotes fleet watch`)
# ---------------------------------------------------------------------------

def watch(store: NoteStore, fleet: FleetConfig, *, once: bool = False, interval: float | None = None,
          should_stop: Callable[[], bool] | None = None,
          log: Callable[[str, str], None] = lambda tag, msg: None,
          read: Callable[..., FleetRead] = read_fleet) -> int:
    """Refresh the feed every ``interval`` seconds until stopped; returns an exit code.

    It runs until ``should_stop()`` says so (checked about once a second, also
    during a fleet read) or, with ``once``, after one refresh. It needs no
    stream state: a caller that has one (a streaming toolkit) passes
    ``should_stop=lambda: not stream_is_live()``. Except with ``once``, it
    removes the feed of a previous session when it starts and its own when it
    stops, so a stopped watcher never leaves an old fleet on screen.
    ``log(tag, line)`` gets one line per change: counts and rule names only.
    """
    if not fleet.enabled:
        log("WRN", "The fleet feed is off: set fleet.firstmate_home in the config")
        return 2
    top = FLEET_MAX_SECONDS["refresh_seconds"]  # the overlay hides a feed older than 180 s
    interval = min(top, max(5.0, interval if interval is not None else fleet.refresh_seconds))
    stop = should_stop or (lambda: False)
    remotes = ", ".join(r.name for r in fleet.remotes) or "none"
    log("OK", f"fleet watch: every {interval:g}s · main home and its secondmates · remotes: {remotes}")
    if not once and store.clear_feed():
        log("OK", "Removed the feed of the previous session")
    last = None
    while not stop():
        # Fixed cadence: the next read starts `interval` after this one started,
        # however long a slow machine kept it, so a feed is at most interval + one
        # read old (90 + 60 s at the caps) when the next one lands: under 180 s.
        cycle_start = time.monotonic()
        try:
            line = refresh_feed(store, fleet.firstmate_home, fleet.snapshot_timeout,
                                should_stop=stop, remotes=fleet.remotes, read=read)
        except Exception as e:  # keep the watcher alive; the feed ages out in the overlay
            line = f"Could not refresh: {e.__class__.__name__}"
        if stop():
            break  # stopped mid-read
        if line != last:
            log("PUB" if line.startswith("Published") else "WRN" if line.startswith("Could not") else "OK", line)
            last = line
        if once:
            break
        deadline = cycle_start + interval
        while not stop() and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    if not once and store.clear_feed():
        log("OK", "Feed removed")
    log("OK", "Stopped")
    return 0
