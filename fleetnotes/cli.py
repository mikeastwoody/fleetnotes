"""fleetnotes — sticky notes for your stream, and what your Firstmate crew is doing.

Captain's notes (public: they go on stream):
    fleetnotes add "Chat: what is a crewmate?"          # temporary, 30 min
    fleetnotes add "Long break at 18:00" --for 2h
    fleetnotes pin "Today: notes that update themselves" # the fixed note
    fleetnotes list | rm 2 | clear
    fleetnotes off | on                                 # panic: hide everything
    fleetnotes serve                                    # the overlay, for a browser source

Fleet notes (optional; rules in a private file, nothing is published without it):
    fleetnotes fleet allow lighthouse "Lighthouse"   # project folder -> public name
    fleetnotes fleet deny lighthouse
    fleetnotes fleet label <task-id> "live notes"    # what it does, approved
    fleetnotes fleet unlabel <task-id>
    fleetnotes fleet plan                            # what would be published; writes nothing
    fleetnotes fleet plan --ids                      # + task ids (private: never on stream)
    fleetnotes fleet watch                           # keep fleet.json fresh until Ctrl-C

Each fleet note says who and what: "Lighthouse · <what>", "Secondmate of
Driftwood · <what>" or "Studio Mac: Harbor · <what>". "What" is the task's
label, or else its backlog title, cleaned. Secondmate homes come from the
main home; other machines from [[fleet.remotes]] in the config (read over ssh).

Every text passes the blocklist (credentials, money, clients, paths, emails,
links, the config's blocked_phrases and the rules file's block lines).
Limits: 90 characters per note (an emoji counts 2), 16 per public project
name, 60 per label.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from typing import Callable, Sequence

from fleetnotes import __version__
from fleetnotes.config import Config, ConfigError, load_config
from fleetnotes.fleet import (
    PUBLISHED, SnapshotError, apply_rules_now, build_feed, header_text, note_text, read_fleet,
    sources_text, watch,
)
from fleetnotes.notes import (
    DEFAULT_NOTE_MINUTES, LABEL_MAX, MAX_NOTE_MINUTES, NOTE_MAX, PROJECT_MAX, NoteStore, PublicTextError,
    parse_iso, visible_manual,
)
from fleetnotes.output import fail, log, ok, warn

_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:m|min)?)?")


def env_on_air() -> bool:
    """On air when FLEETNOTES_ON_AIR=1 (export it in the terminal you stream from)."""
    return os.environ.get("FLEETNOTES_ON_AIR") == "1"


def parse_minutes(text: str) -> int:
    """'30m', '45', '2h', '1h30m' -> minutes."""
    match = _DURATION_RE.fullmatch(text.strip().lower())
    if not match or not any(match.groups()):
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (examples: 30m, 2h, 1h30m)")
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def short(path: str) -> str:
    """A path with the home folder as ~ (the terminal may be on stream)."""
    home = os.path.expanduser("~")
    if home not in ("", "/") and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def tilde(text: str) -> str:
    """Any path under the home folder inside ``text``, shown as ~ (config messages)."""
    home = os.path.expanduser("~").rstrip(os.sep)
    if not home:
        return text
    return re.sub(re.escape(home) + r"(?=$|" + re.escape(os.sep) + r")", "~", text)


def human_left(expires: str | None, now: float) -> str:
    if expires is None:
        return "no expiry"
    ts = parse_iso(expires)
    if ts is None:
        return "unreadable expiry"
    minutes = max(1, round((ts - now) / 60))
    return f"expires in {minutes} min" if minutes < 60 else f"expires in {minutes // 60} h {minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Captain's notes
# ---------------------------------------------------------------------------

def cmd_add(store: NoteStore, args, cfg: Config) -> int:
    text = " ".join(args.text)
    minutes = args.minutes
    if minutes is None and not args.pin:  # a pinned note stays until replaced or removed
        minutes = DEFAULT_NOTE_MINUTES
    try:
        note = store.add_note(text, minutes=minutes, pinned=args.pin)
    except PublicTextError as e:
        fail(f"Note refused: {e}")
        return 1
    kind = "Pinned note" if note.pinned else "Temporary note"
    ok(f"{kind} published: \"{note.text}\" ({human_left(note.expires, time.time())})")
    if store.read_manual()[0]:
        warn("The board is off (fleetnotes off): it will not show until 'fleetnotes on'.")
    return 0


def cmd_list(store: NoteStore, args, cfg: Config) -> int:
    now = time.time()
    off, _ = store.read_manual()
    notes = store.list_notes(now)
    shown = {n.key for n in visible_manual(notes, now)}
    print(f"Captain's notes — {short(store.manual_path)}" + ("  [OFF: fleetnotes off]" if off else ""))
    if not notes:
        print("  (none)")
    for i, note in enumerate(notes, 1):
        tag = "pinned" if note.pinned else "temporary"
        extra = "" if note.key in shown else "  · not shown: only the newest of each kind is"
        print(f"  {i}. [{tag}] {note.text}  · {human_left(note.expires, now)}{extra}")

    rules = store.load_rules()
    print(f"Fleet rules — {short(store.rules_path)}: ", end="")
    if not rules.exists:
        print("none (nothing of the fleet is published)")
    else:
        print(f"{len(rules.projects)} allowed project(s), {len(rules.labels)} label(s), "
              f"{len(rules.local_blocks)} block line(s)"
              + ("" if rules.publishing else " · publishes nothing"))
        for error in rules.errors:
            warn(error)
    feed = store.read_feed()
    if feed is None:
        print(f"Fleet feed — {short(store.feed_path)}: none")
    else:
        age = parse_iso(feed.get("generated"))
        when = f"read {round(now - age)} s ago" if age else "undated"
        print(f"Fleet feed — {short(store.feed_path)}: {len(feed.get('notes') or [])} note(s), {when}")
    return 0


def cmd_rm(store: NoteStore, args, cfg: Config) -> int:
    try:
        note = store.remove_note(args.number)
    except IndexError:
        fail(f"There is no note {args.number} (see 'fleetnotes list').")
        return 1
    ok(f"Removed: \"{note.text}\"")
    return 0


def cmd_clear(store: NoteStore, args, cfg: Config) -> int:
    n = store.clear_notes()
    ok(f"Removed {n} captain's note(s)." if n else "There were no captain's notes.")
    return 0


def cmd_off(store: NoteStore, args, cfg: Config) -> int:
    store.set_off(True)
    ok("Board off: the overlay draws nothing (within 5 s) and the fleet feed is removed.")
    return 0


def cmd_on(store: NoteStore, args, cfg: Config) -> int:
    store.set_off(False)
    ok("Board on. The fleet comes back at the watcher's next refresh (within a minute).")
    return 0


def cmd_serve(store: NoteStore, args, cfg: Config) -> int:
    from fleetnotes.server import make_server

    host = args.host or cfg.serve.host
    port = args.port or cfg.serve.port
    try:
        server = make_server(host, port, store.output_dir)
    except OSError as e:
        fail(f"Could not listen on {host}:{port} ({e.strerror or e.__class__.__name__})")
        return 1
    url_host = f"[{host}]" if ":" in host else host
    ok(f"Serving the overlay at http://{url_host}:{server.server_address[1]}/ "
       f"(browser source 968x132). Ctrl-C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# Fleet rules
# ---------------------------------------------------------------------------

def after_rules_change(store: NoteStore) -> None:
    if apply_rules_now(store):
        ok("Applied to the published feed already.")
    for error in store.load_rules().errors:
        warn(f"{short(store.rules_path)}: {error} — nothing of the fleet is published until it is fixed")


def cmd_allow(store: NoteStore, args, cfg: Config) -> int:
    try:
        name = store.allow_project(args.project, " ".join(args.name))
    except PublicTextError as e:
        fail(f"Not allowed: {e}")
        return 1
    ok(f"Project '{args.project}' allowed as \"{name}\". It shows at the watcher's next refresh.")
    after_rules_change(store)
    return 0


def cmd_deny(store: NoteStore, args, cfg: Config) -> int:
    if store.deny_project(args.project):
        ok(f"Project '{args.project}' removed from the list: its tasks only count in the header.")
        after_rules_change(store)
    else:
        warn(f"'{args.project}' was not on the list.")
    return 0


def cmd_label(store: NoteStore, args, cfg: Config) -> int:
    try:
        label = store.set_label(args.task, " ".join(args.label))
    except PublicTextError as e:
        fail(f"Label refused: {e}")
        return 1
    ok(f"Label saved: \"{label}\". It shows at the watcher's next refresh, "
       "once it is checked to hold no task's id.")
    after_rules_change(store)
    return 0


def cmd_unlabel(store: NoteStore, args, cfg: Config) -> int:
    if store.remove_label(args.task):
        ok("Label removed: the task shows with its cleaned title again (or 'a crewmate at work').")
        after_rules_change(store)
    else:
        warn("That task had no label.")
    return 0


def cmd_plan(store: NoteStore, args, cfg: Config, on_air: Callable[[], bool] = env_on_air) -> int:
    fleet = cfg.fleet
    if args.ids and on_air():
        fail("On air: task ids are not printed (your terminal may be on screen). "
             "Use 'fleet plan' without --ids, or wait until you are off air.")
        return 1
    if not fleet.enabled:
        fail("The fleet feed is off: set fleet.firstmate_home in the config.")
        return 2
    rules = store.load_rules()
    print("fleetnotes fleet plan — preview: writes and publishes nothing")
    print(f"Rules: {short(store.rules_path)} — ", end="")
    if not rules.exists:
        print("none")
    else:
        print(f"{len(rules.projects)} project(s), {len(rules.labels)} label(s), "
              f"{len(rules.local_blocks)} block line(s) + {len(rules.blocklist) - len(rules.local_blocks)} built in")
    for error in rules.errors:
        warn(error)
    if store.read_manual()[0]:
        warn("The board is off (fleetnotes off): nothing is published right now.")

    remotes = ", ".join(r.name for r in fleet.remotes) or "none"
    print(f"Reading the fleet with {short(fleet.firstmate_home)}/bin/fm-fleet-snapshot.sh --json "
          f"(and its secondmates; remotes: {remotes}) ...", flush=True)
    try:
        read = read_fleet(fleet.firstmate_home, fleet.snapshot_timeout, remotes=fleet.remotes)
        result = build_feed(read.main, rules, store.salt(), extra=read.extra, unread=read.unread())
    except SnapshotError as e:
        fail(f"Could not read the fleet: {e}")
        return 1
    feed = result.feed
    print(f"Fleet read: {feed['generated']} · sources: {sources_text(result.read, result.unread)}")
    for st in read.statuses:
        if st.origin == "remote":  # its configured name is public; its host is not printed
            print(f"  {st.place}: " + (f"unread ({st.error})" if st.error else "read"))
    failed_homes = [st for st in read.statuses if st.origin == "secondmate" and st.error]
    for error in sorted({st.error for st in failed_homes}):
        n = sum(st.error == error for st in failed_homes)
        print(f"  {n} secondmate home(s) unread: {error}")

    if rules.publishing:
        print(f"Would publish to {short(store.feed_path)}:")
    else:
        why = "the rules have errors" if rules.errors else "no project is allowed"
        print(f"Would publish nothing ({why}). With at least one allowed project the feed would be:")
    print(json.dumps(feed, ensure_ascii=False, indent=2))

    print("As the overlay would draw it (a continuous ticker, right to left):")
    print(f"  {header_text(feed['summary']) or '(no header)'}")
    if not feed["notes"]:
        print("  (no fleet notes)")
    for note in feed["notes"]:
        sentence, state = note_text(note)
        origin = {"label": "label", "title": "title"}.get(note["label_from"], "no text")
        print(f"  {sentence}  — {state}  [{origin}]")

    print("Decisions per task (no ids):")
    for reason, n in result.counts().most_common():
        print(f"  {n:>3}  {reason}")
    for detail in sorted({d.detail for d in result.decisions if d.detail}):
        print(f"       · {detail}")

    if args.ids:
        warn("Private list with task ids: do not show it on stream.")
        for d in sorted(result.decisions, key=lambda d: (d.reason != PUBLISHED, d.reason, d.source, d.task_id)):
            where = f"[{d.source}] " if d.source else ""
            print(f"  {where}{d.task_id or '(no id)':<40} {d.reason}" + (f" — {d.detail}" if d.detail else ""))
    return 0


def cmd_watch(store: NoteStore, args, cfg: Config,
              should_stop: Callable[[], bool] | None = None) -> int:
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def stop() -> bool:
        return not running or (should_stop is not None and should_stop())

    def say(tag: str, msg: str) -> None:
        log(tag, msg, datetime.now().strftime("%H:%M:%S"))

    return watch(store, cfg.fleet, once=args.once, interval=args.interval, should_stop=stop, log=say)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleetnotes",
        description="Sticky notes for your stream: the captain's notes and, optionally, "
                    "what your Firstmate crew is doing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    parser.add_argument("--version", action="version", version=f"fleetnotes {__version__}")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="config file (default: $FLEETNOTES_CONFIG or ~/.config/fleetnotes/config.toml)")
    parser.add_argument("--rules", default=None, metavar="PATH",
                        help="rules file instead of the config's rules_file")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("add", help=f"temporary note ({DEFAULT_NOTE_MINUTES} min by default), or pinned with --pin")
    p.add_argument("text", nargs="+", help=f"text (up to {NOTE_MAX} characters; an emoji counts 2)")
    p.add_argument("--for", dest="minutes", type=parse_minutes, default=None, metavar="DURATION",
                   help=f"how long it lasts: 30m, 2h, 1h30m (max {MAX_NOTE_MINUTES // 60} h)")
    p.add_argument("--pin", action="store_true", help="pinned note: replaces the previous pinned note")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("pin", help="pinned note (same as add --pin)")
    p.add_argument("text", nargs="+")
    p.add_argument("--for", dest="minutes", type=parse_minutes, default=None, metavar="DURATION")
    p.set_defaults(func=cmd_add, pin=True)

    sub.add_parser("list", help="notes, board state and fleet state").set_defaults(func=cmd_list)
    p = sub.add_parser("rm", help="remove note N of 'list'")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_rm)
    sub.add_parser("clear", help="remove every captain's note").set_defaults(func=cmd_clear)
    sub.add_parser("off", help="panic: the overlay draws nothing").set_defaults(func=cmd_off)
    sub.add_parser("on", help="switch the board back on").set_defaults(func=cmd_on)
    p = sub.add_parser("serve", help="serve the overlay and its two JSON files (127.0.0.1 by default)")
    p.add_argument("--host", default=None, help="address to listen on (default: serve.host)")
    p.add_argument("--port", type=int, default=None, help="port (default: serve.port, 8765)")
    p.set_defaults(func=cmd_serve)

    fleet = sub.add_parser("fleet", help="the optional Firstmate feed").add_subparsers(
        dest="fleet_command", required=True, metavar="ACTION")
    p = fleet.add_parser("allow", help="allow a project under a public name")
    p.add_argument("project", help="the project's folder name (e.g. lighthouse)")
    p.add_argument("name", nargs="+", help=f"public name (up to {PROJECT_MAX} characters)")
    p.set_defaults(func=cmd_allow)
    p = fleet.add_parser("deny", help="remove a project from the list")
    p.add_argument("project")
    p.set_defaults(func=cmd_deny)
    p = fleet.add_parser("label", help="what a task does, in public (else its cleaned title)")
    p.add_argument("task", help="task id")
    p.add_argument("label", nargs="+", help=f"label (up to {LABEL_MAX} characters)")
    p.set_defaults(func=cmd_label)
    p = fleet.add_parser("unlabel", help="remove a task's label")
    p.add_argument("task")
    p.set_defaults(func=cmd_unlabel)
    p = fleet.add_parser("plan", help="what would be published now, writing nothing")
    p.add_argument("--ids", action="store_true",
                   help="add the task ids (private; refused while FLEETNOTES_ON_AIR=1)")
    p.set_defaults(func=cmd_plan)
    p = fleet.add_parser("watch", help="refresh fleet.json every fleet.refresh_seconds until stopped")
    p.add_argument("--once", action="store_true", help="refresh once and exit (keeps the file)")
    p.add_argument("--interval", type=float, default=None, metavar="SECONDS",
                   help="override fleet.refresh_seconds (5-90)")
    p.set_defaults(func=cmd_watch)
    return parser


def main(argv: Sequence[str] | None = None, *, on_air: Callable[[], bool] | None = None,
         should_stop: Callable[[], bool] | None = None) -> int:
    """Run the CLI. A toolkit that knows when the stream is live can pass
    ``on_air`` (refuses ``fleet plan --ids``) and ``should_stop`` (ends
    ``fleet watch`` when the stream ends)."""
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        fail(tilde(str(e)))
        return 2
    for line in cfg.warnings:
        warn(f"config: {tilde(line)}")
    store = NoteStore.from_config(cfg, rules_path=args.rules)
    if args.func is cmd_plan:
        return cmd_plan(store, args, cfg, on_air=on_air or env_on_air)
    if args.func is cmd_watch:
        return cmd_watch(store, args, cfg, should_stop=should_stop)
    return args.func(store, args, cfg)


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
