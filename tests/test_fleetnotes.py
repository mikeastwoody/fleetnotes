"""Tests for the fleet feed's privacy filter, task titles made public,
secondmate homes and remote machines, the rules file, the captain's notes and
the read of Firstmate's view (fleetnotes.notes, fleetnotes.fleet).

The fleet snapshot is a fixture shaped like fm-fleet-snapshot.v1 (the keys a
real `fm-fleet-snapshot.sh --json` prints), full of private words: ids,
notes, status text and titles made only of what the cleaning must drop, none
of which may reach the public feed. The fruit words (kumquat, pomelo, ...) are
canaries: they only exist in private fields. Firstmate's view and ssh are
fakes; no Firstmate, network or second machine needed; every file goes to a
temp dir.

Run from the repository root:
    python -m unittest discover -s tests -t . -v
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleetnotes.fleet
from fleetnotes.config import Remote
from fleetnotes.fleet import (
    BLOCKED_LABEL, BLOCKED_PROJECT, DONE_OLD, FEED_KEYS, KINDS, LABEL_FROM, NO_CACHE_DIR, NOTE_KEYS,
    OTHER_KIND, PRIVATE_PROJECT, PUBLISHED, STATE_ORDER, STEPS, SUMMARY_KEYS, UNKNOWN_STATE, WHO_MAX,
    FleetRead, SnapshotError, Source, SourceStatus, apply_rules_now, build_feed, header_text, note_text,
    outcome_line, public_state, public_title, read_fleet, refilter_feed, refresh_feed,
    remote_command, run_remote_snapshot, run_snapshot, secondmate_homes, validate_feed,
)
from fleetnotes.notes import (
    FLEET_SCHEMA, LABEL_MAX, MANUAL_SCHEMA, NOTE_MAX, NoteStore, PublicTextError, Rules,
    iso_utc, parse_iso, parse_rules, task_key, text_width, visible_manual,
)

GENERATED = "2026-11-04T13:00:00Z"
GEN_TS = parse_iso(GENERATED)
NOW = GEN_TS + 5
SALT = bytes(range(32))
RULES_PATH = "/srv/example/fleetnotes/rules.conf"

# Words that only exist in private fields of the fixture.
SECRET_WORDS = ("kumquat", "zanahoria", "berenjena", "pomelo", "lucuma", "chirimoya",
                "maracuya", "guanabana", "acme", "tamarindo")
PRIVATE_NOTE = "captain's decision on guanabana: «do not say it on stream»"
PRIVATE_EVENT = "working: redoing tamarindo with acme's invoice, see /Users/example/secret"


def private_title(task_id):
    """A backlog title made only of what public_title drops: nothing is left."""
    return (f"Lighthouse: {task_id} [pomelo] (lucuma with maracuya) /Users/example/acme/zanahoria "
            f"berenjena@acme.example https://acme.example/kumquat password")


def make_task(task_id, project, state="working", *, kind="ship", source="pane",
              detail="harness busy (claude-hook)", spawned_ago=1800, event_age=600,
              pending=False, blocked=False, freshness="fresh"):
    """One tasks[] row shaped like fm-fleet-snapshot.v1."""
    return {
        "id": task_id,
        "kind": kind,
        "harness": "claude",
        "mode": "direct-PR",
        "yolo": True,
        "project": f"/srv/example/firstmate/projects/{project}",
        "spawn_gen": f"s{int(GEN_TS - spawned_ago)}.4242.17",
        "backend": "herdr",
        "paths": {
            "meta": {"path": f"/srv/example/firstmate/state/{task_id}.meta", "present": True},
            "status_log": {
                "path": f"/srv/example/firstmate/state/{task_id}.status", "present": True,
                "kind": "status",
                "last_event": {"state": "working", "note": PRIVATE_NOTE,
                               "raw": f"working [at=1]: {PRIVATE_NOTE}", "age_seconds": event_age},
            },
            "worktree": {"path": f"/srv/example/worktrees/{task_id}", "present": True},
            "home": {"path": None, "present": False},
            "report": {"path": f"/srv/example/firstmate/data/{task_id}/report.md"},
        },
        "current_state": {"state": state, "source": source, "detail": detail,
                          "raw": f"state: {state} · source: {source} · {detail}",
                          "observed_at": GENERATED, "freshness": freshness},
        "endpoint": {"target": f"default:w3:{task_id}", "exists": True, "agent_alive": "not_checked",
                     "status": "present", "observed_at": GENERATED, "freshness": "fresh"},
        "pr": {"url": f"https://github.com/example/{task_id}/pull/9", "source": "status-log"},
        "hints": {"pending_decision": pending, "blocked_event": blocked,
                  "open_decisions": ([{"key": f"{task_id}-chirimoya", "verb": "needs-decision",
                                       "summary": PRIVATE_NOTE}] if pending else []),
                  "last_event_text": PRIVATE_EVENT},
        "actions": {"send": f"fm-send.sh {task_id} 'kumquat'", "watch": f"fm-peek.sh {task_id}",
                    "return_channel_note": PRIVATE_NOTE},
    }


def make_snapshot(tasks, titles=None, home="/srv/example/firstmate"):
    titles = titles or {}
    backlog = [{"id": t["id"], "state": "in_flight", "kind": t["kind"],
                "title": titles.get(t["id"], private_title(t["id"])),
                "body_excerpt": f"Private notes on pomelo and lucuma about {t['id']}",
                "body_lines": [PRIVATE_NOTE], "hold_reason": "waiting for maracuya",
                "repo": t["project"].rsplit("/", 1)[-1], "since": "2026-11-04",
                "raw": f"- [ ] {t['id']} — Redo it for acme"} for t in tasks]
    return {"schema": "fm-fleet-snapshot.v1", "generated": GENERATED,
            "fm_home": home,
            "roots": {"state": "/srv/example/firstmate/state"},
            "backlog": {"path": "/srv/example/firstmate/data/backlog.md", "present": True,
                        "records": backlog},
            "tasks": tasks, "scout_reports": [], "secondmate_current": {"records": []}}


FLEET = [
    make_task("lh-kumquat-overlay", "lighthouse"),                                   # labelled
    make_task("lh-pomelo-tests", "lighthouse", source="run-step",
              detail="ci running · run: 01KUMQUAT"),                                 # validating
    make_task("lh-acme-waiting", "lighthouse", "parked", source="status-log",
              detail=PRIVATE_NOTE, pending=True),                                    # waits on the captain
    make_task("sh-zanahoria-sync", "seashell"),                                      # private project
    make_task("ks-berenjena-render", "kelp-studio", "blocked", source="status-log",
              detail=PRIVATE_EVENT),                                                 # private, problem
    make_task("lh-lucuma-done", "lighthouse", "done", source="status-log",
              detail="checks green", event_age=120),                                 # recently done
    make_task("lh-chirimoya-old", "lighthouse", "done", source="status-log", event_age=3600),
    make_task("sc-tamarindo-gone", "lighthouse", "unknown", source="none",
              detail="backend target gone: default:w3:pKS"),
    make_task("dw-maracuya-home", "firstmate", "parked", kind="secondmate", source="status-log",
              detail="captain hold dw-maracuya-home: " + PRIVATE_NOTE),
    make_task("sc-guanabana-research", "lighthouse", kind="scout", spawned_ago=300),  # unlabelled scout
]

RULES_TEXT = textwrap.dedent("""\
    # test rules
    project lighthouse "Lighthouse"
    label lh-kumquat-overlay "notes that move"
    label lh-pomelo-tests "fleet panel"
""")


def rules_from(text=RULES_TEXT, blocked_phrases=()):
    return parse_rules(text, RULES_PATH, blocked_phrases)


def private_fragments(snapshot):
    """Every token of every free-text field in the fixture (ids, titles, notes, paths…)."""
    texts = []

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key not in ("schema", "generated", "observed_at", "state",
                                                    "kind", "harness", "backend", "mode", "freshness",
                                                    "source", "since", "status", "verb"):
            texts.append(value)

    walk(snapshot)
    tokens = set()
    for text in texts:
        tokens.add(text.casefold())
        tokens.update(t.casefold() for t in re.findall(r"[\w-]{4,}", text))
    return tokens


def temp_store(root):
    """A NoteStore whose every folder is under ``root`` (never the real home)."""
    return NoteStore(os.path.join(root, "public"), os.path.join(root, "config", "rules.conf"),
                     os.path.join(root, "state"))


class PublicFeedTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = make_snapshot(FLEET)
        self.result = build_feed(self.snapshot, rules_from(), SALT, now=NOW)
        self.feed = self.result.feed
        self.notes = {n["label"] or n["state"]: n for n in self.feed["notes"]}

    def test_feed_holds_only_contract_keys(self):
        self.assertEqual(set(self.feed), FEED_KEYS)
        self.assertEqual(self.feed["schema"], FLEET_SCHEMA)
        self.assertEqual(FLEET_SCHEMA, "fleetnotes-feed.v1")
        self.assertEqual(set(self.feed["summary"]), set(SUMMARY_KEYS))
        for note in self.feed["notes"]:
            self.assertLessEqual(set(note), NOTE_KEYS)
        validate_feed(self.feed)

    def test_feed_has_no_task_id_title_or_private_text(self):
        published = json.dumps(self.feed, ensure_ascii=False).casefold()
        # The contract's own words (keys, closed vocabulary, schema) and the
        # approved public texts may appear; any other private token is a leak.
        public_words = [w.casefold() for w in (*FEED_KEYS, *NOTE_KEYS, *SUMMARY_KEYS, *KINDS, *STATE_ORDER,
                                               *STEPS, *LABEL_FROM, FLEET_SCHEMA, "Lighthouse",
                                               "notes that move", "fleet panel")]
        leaks = sorted(t for t in private_fragments(self.snapshot)
                       if t in published and not any(t in w for w in public_words))
        self.assertEqual(leaks, [])
        for word in SECRET_WORDS:
            self.assertNotIn(word, published)
        for task in FLEET:
            self.assertNotIn(task["id"].casefold(), published)
        for needle in ("/srv/", "/users/", "http", "@", "seashell", "kelp-studio", "firstmate/"):
            self.assertNotIn(needle, published)

    def test_unlabelled_task_with_nothing_public_in_its_title_gets_null_label(self):
        scout = [n for n in self.feed["notes"] if n["kind"] == "scout"]
        self.assertEqual(len(scout), 1)
        self.assertIsNone(scout[0]["label"])
        self.assertIsNone(scout[0]["label_from"])
        self.assertEqual((scout[0]["project"], scout[0]["who"]), ("Lighthouse", "Lighthouse"))
        self.assertEqual(note_text(scout[0]), ("Lighthouse · a crewmate at work", "running"))

    def test_private_projects_only_count_anonymously(self):
        reasons = {d.task_id: d.reason for d in self.result.decisions}
        self.assertEqual(reasons["sh-zanahoria-sync"], PRIVATE_PROJECT)
        self.assertEqual(reasons["ks-berenjena-render"], PRIVATE_PROJECT)
        self.assertEqual({n["project"] for n in self.feed["notes"]}, {"Lighthouse"})
        summary = self.feed["summary"]
        self.assertEqual(summary, {"running": 3, "validating": 1, "needs_captain": 1, "problem": 1,
                                   "paused": 0, "done_recent": 1})
        self.assertEqual(header_text(summary), "FIRSTMATE · 6 crewmates · 1 waiting · 1 stuck")
        self.assertEqual(header_text(dict(summary, running=0, validating=1, needs_captain=0, problem=0)),
                         "FIRSTMATE · 1 crewmate")
        self.assertIsNone(header_text(dict.fromkeys(SUMMARY_KEYS, 0)))

    def test_states_and_order(self):
        states = [n["state"] for n in self.feed["notes"]]
        self.assertEqual(states, ["needs_captain", "done", "validating", "running", "running"])
        validating = self.notes["fleet panel"]
        self.assertEqual((validating["state"], validating.get("step")), ("validating", "ci"))
        self.assertEqual(note_text(self.notes["needs_captain"])[1], "waiting on the captain")
        self.assertEqual(note_text(self.notes["done"])[1], "done!")

    def test_done_is_shown_ten_minutes_then_dropped(self):
        reasons = {d.task_id: d.reason for d in self.result.decisions}
        self.assertEqual(reasons["lh-lucuma-done"], PUBLISHED)
        self.assertEqual(reasons["lh-chirimoya-old"], DONE_OLD)

    def test_unknown_state_and_secondmates_neither_show_nor_count(self):
        reasons = {d.task_id: d.reason for d in self.result.decisions}
        self.assertEqual(reasons["sc-tamarindo-gone"], UNKNOWN_STATE)
        self.assertEqual(reasons["dw-maracuya-home"], OTHER_KIND)
        stale = make_task("lh-stale", "lighthouse", freshness="unknown")
        self.assertEqual(public_state(stale), (None, None))
        unsure = make_task("lh-unsure", "lighthouse")
        del unsure["current_state"]["freshness"]
        self.assertEqual(public_state(unsure), (None, None))

    def test_times_come_from_spawn_and_last_event(self):
        note = self.notes["notes that move"]
        self.assertEqual(note["started"], iso_utc(GEN_TS - 1800))
        self.assertEqual(note["changed"], iso_utc(GEN_TS - 600))
        odd = make_task("lh-odd", "lighthouse")
        odd["spawn_gen"] = "g-no-epoch"
        odd["paths"]["status_log"]["last_event"]["age_seconds"] = None
        rules = rules_from(RULES_TEXT + 'label lh-odd "odd one"\n')
        feed = build_feed(make_snapshot([odd]), rules, SALT, now=NOW).feed
        self.assertEqual((feed["notes"][0]["started"], feed["notes"][0]["changed"]), (None, None))

    def test_keys_are_opaque_stable_and_salted(self):
        note = self.notes["notes that move"]
        self.assertRegex(note["key"], r"^[0-9a-f]{10}$")
        self.assertEqual(note["key"], task_key(SALT, "lh-kumquat-overlay"))
        self.assertNotEqual(task_key(b"x" * 32, "lh-kumquat-overlay"), note["key"])

    def test_foreign_schema_or_no_reading_time_raises(self):
        with self.assertRaises(SnapshotError):
            build_feed({"schema": "something-else", "tasks": []}, rules_from(), SALT, now=NOW)
        for generated in (None, "yesterday", iso_utc(NOW + 3600)):
            snapshot = dict(make_snapshot(FLEET), generated=generated)
            with self.assertRaises(SnapshotError):
                build_feed(snapshot, rules_from(), SALT, now=NOW)

    def test_validate_feed_rejects_anything_outside_the_contract(self):
        for mutate in (lambda f: f.update(extra=1),
                       lambda f: f["notes"][0].update(title="Redo kumquat"),
                       lambda f: f["notes"][0].update(project="x" * 40),
                       lambda f: f["notes"][0].update(state="secret"),
                       lambda f: f["notes"][0].update(who="Another project"),     # not about its project
                       lambda f: f["notes"][0].update(who="x" * (WHO_MAX + 1) + "Lighthouse"),
                       lambda f: f["notes"][0].update(label="x" * (LABEL_MAX + 1)),
                       lambda f: f["notes"][0].update(label_from="kumquat"),
                       lambda f: next(n for n in f["notes"] if n["label"]).update(label=None),  # label_from left
                       lambda f: f["summary"].update(total=9)):
            feed = json.loads(json.dumps(self.feed))
            mutate(feed)
            with self.assertRaises(ValueError):
                validate_feed(feed)


class BlocklistTest(unittest.TestCase):
    def test_blocked_label_suppresses_the_note(self):
        rules = rules_from(RULES_TEXT.replace("notes that move", "new kick token"))
        result = build_feed(make_snapshot(FLEET), rules, SALT, now=NOW)
        decision = next(d for d in result.decisions if d.task_id == "lh-kumquat-overlay")
        self.assertEqual(decision.reason, BLOCKED_LABEL)
        self.assertIn("built-in rule 'credentials'", decision.detail)
        self.assertNotIn("token", decision.detail)
        self.assertNotIn(task_key(SALT, "lh-kumquat-overlay"), [n["key"] for n in result.feed["notes"]])
        self.assertNotIn("token", json.dumps(result.feed))
        self.assertEqual(result.feed["summary"]["running"], 3)  # still counted

    def test_local_block_line_and_blocked_phrases(self):
        rules = rules_from(RULES_TEXT + "block move|projectX\n", blocked_phrases=["RevenueCat"])
        result = build_feed(make_snapshot(FLEET), rules, SALT, now=NOW)
        decision = next(d for d in result.decisions if d.task_id == "lh-kumquat-overlay")
        self.assertEqual(decision.reason, BLOCKED_LABEL)
        self.assertIn("line 5 of fleetnotes/rules.conf", decision.detail)
        phrase = rules.blocklist.match("Testing revenuecat")
        self.assertIsNotNone(phrase)
        self.assertEqual(phrase.describe(rules.name), "blocked_phrases entry 1")
        self.assertEqual(rules.blocklist.match("my password").describe(rules.name), "built-in rule 'credentials'")

    def test_blocked_public_project_name_only_counts(self):
        rules = rules_from('project lighthouse "Client Acme"\n')
        result = build_feed(make_snapshot(FLEET), rules, SALT, now=NOW)
        self.assertEqual(result.feed["notes"], [])
        self.assertIn(BLOCKED_PROJECT, result.counts())

    def test_builtin_rules(self):
        # English and Spanish: the built-in list covers a bilingual stream.
        rules = rules_from("")
        for text in ("mi contraseña", "my password", "new API_KEY", "open .env", "costs 20 €", "$5 a month",
                     "factura de marzo", "invoice for March", "clientes VIP", "VIP customers", "Apple Ads",
                     "~/projects/x", "/Users/example", "/home/example/x", "someone@example.com",
                     "https://example.app", "www.example", "pass​word"):
            self.assertIsNotNone(rules.blocklist.match(text), text)
        for text in ("notes that move", "apps overlay", "24/7 live", "iOS/macOS"):
            self.assertIsNone(rules.blocklist.match(text), text)


class RulesFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = temp_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_rules_file_publishes_nothing(self):
        rules = self.store.load_rules()
        self.assertFalse(rules.exists)
        self.assertFalse(rules.publishing)
        calls = []
        line = refresh_feed(self.store, "/opt/fm/none", read=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])  # the fleet is not even read
        self.assertEqual(line, "No allowed projects in config/rules.conf: publishing nothing")
        self.assertFalse(os.path.exists(self.store.feed_path))

    def test_errors_keep_publishing_off(self):
        rules = parse_rules('project lighthouse "Lighthouse"\nblock (\nlabels x "y"\nblock a*\n',
                            "/srv/example/rules.conf")
        self.assertFalse(rules.publishing)
        self.assertEqual(rules.errors, ["line 2: the 'block' expression is not valid",
                                        "line 3: not understood (expected project, label or block)",
                                        "line 4: the 'block' expression matches any text"])

    def test_inline_comments_are_errors_not_part_of_the_rule(self):
        rules = parse_rules(textwrap.dedent("""\
            project lighthouse "Lighthouse"   # public name
            label lh-x new task #wip
            block acme|jordan   # client names
        """), "/srv/example/rules.conf")
        self.assertEqual([e.split(":")[0] for e in rules.errors], ["line 1", "line 2", "line 3"])
        self.assertFalse(rules.publishing)
        with self.assertRaises(PublicTextError):  # and nothing manual is published either
            store = NoteStore(self.store.output_dir, self.store.rules_path, self.store.state_dir)
            os.makedirs(os.path.dirname(store.rules_path), exist_ok=True)
            with open(store.rules_path, "w", encoding="utf-8") as f:
                f.write("block acme|jordan   # client names\n")
            store.add_note("hello jordan")

    def test_labels_never_carry_a_task_id(self):
        with self.assertRaises(PublicTextError):
            self.store.set_label("lh-kumquat-overlay", "LH-Kumquat-Overlay new")
        rules = rules_from(RULES_TEXT + 'label lh-lucuma-done "see lh-kumquat-overlay"\n'
                           + 'label sc-guanabana-research "sc-guanabana-research"\n')
        result = build_feed(make_snapshot(FLEET), rules, SALT, now=NOW)
        reasons = {d.task_id: (d.reason, d.detail) for d in result.decisions}
        self.assertEqual(reasons["lh-lucuma-done"][0], BLOCKED_LABEL)        # another task's id
        self.assertEqual(reasons["sc-guanabana-research"][0], BLOCKED_LABEL)  # its own id
        published = json.dumps(result.feed).casefold()
        for task in FLEET:
            self.assertNotIn(task["id"], published)

    def test_cli_writers_keep_comments_replace_duplicates_and_mode_600(self):
        self.store.allow_project("lighthouse", "Lighthouse")
        self.store.set_label("lh-kumquat-overlay", "notes that move")
        with open(self.store.rules_path, "a", encoding="utf-8") as f:
            f.write('# mine\nproject lighthouse "Other"\n')
        self.store.allow_project("lighthouse", "Light House")
        with open(self.store.rules_path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("# fleetnotes"))  # the header explains the file
        self.assertEqual(text.count("project lighthouse"), 1)
        self.assertIn('project lighthouse "Light House"', text)
        self.assertIn("# mine", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.rules_path).st_mode), 0o600)
        self.assertTrue(self.store.remove_label("lh-kumquat-overlay"))
        self.assertFalse(self.store.remove_label("lh-kumquat-overlay"))
        self.assertTrue(self.store.deny_project("lighthouse"))
        self.assertFalse(self.store.load_rules().publishing)

    def test_writers_refuse_blocked_long_or_odd_text(self):
        for name in ("Clientes VIP", "VIP customers", "A name that is too long"):
            with self.subTest(name=name):
                with self.assertRaises(PublicTextError):
                    self.store.allow_project("lighthouse", name)
        with self.assertRaises(PublicTextError):
            self.store.allow_project("../etc", "Something")
        with self.assertRaises(PublicTextError):
            self.store.set_label("lh-x", "x" * (LABEL_MAX + 1))
        with self.assertRaises(PublicTextError):
            self.store.set_label("lh-x", 'with "quotes"')
        self.assertFalse(os.path.exists(self.store.rules_path))


class ManualNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = temp_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with open(self.store.manual_path, encoding="utf-8") as f:
            return json.load(f)

    def test_temporary_and_pinned_notes(self):
        t0 = 1_800_000_000
        self.store.add_note("Chat: what is a crewmate?", minutes=30, now=t0)
        self.store.add_note("Today: notes that update themselves", minutes=None, pinned=True, now=t0)
        self.store.add_note("New pinned note", minutes=None, pinned=True, now=t0 + 1)
        data = self.read()
        self.assertEqual(data["schema"], MANUAL_SCHEMA)
        self.assertEqual(MANUAL_SCHEMA, "fleetnotes-notes.v1")
        self.assertFalse(data["off"])
        self.assertEqual([(n["text"], n["pinned"]) for n in data["notes"]],
                         [("Chat: what is a crewmate?", False), ("New pinned note", True)])
        self.assertEqual(data["notes"][0]["expires"], iso_utc(t0 + 1800))
        for note in data["notes"]:
            self.assertEqual(set(note), {"key", "text", "pinned", "expires"})
        later = t0 + 1801
        self.assertEqual([n.text for n in visible_manual(self.store.list_notes(later), later)], ["New pinned note"])

    def test_refused_texts(self):
        for text in ("my password is hunter2", "x" * (NOTE_MAX + 1), "\U0001F389" * (NOTE_MAX // 2 + 1),
                     "", "two\x00parts", "pass​word"):
            with self.subTest(text=text):
                with self.assertRaises(PublicTextError):
                    self.store.add_note(text)
        with_phrases = NoteStore(self.store.output_dir, self.store.rules_path, self.store.state_dir,
                                 blocked_phrases=["Stripe"])
        with self.assertRaises(PublicTextError) as ctx:
            with_phrases.add_note("Stripe is up to date")
        self.assertEqual(str(ctx.exception), "matches the blocked_phrases entry 1")
        self.assertFalse(os.path.exists(self.store.manual_path))

    def test_broken_rules_file_refuses_notes(self):
        os.makedirs(os.path.dirname(self.store.rules_path))
        with open(self.store.rules_path, "w", encoding="utf-8") as f:
            f.write("block (\n")
        with self.assertRaises(PublicTextError):
            self.store.add_note("hello")

    def test_rm_clear_off_on(self):
        self.store.add_note("one")
        self.store.add_note("two")
        self.assertEqual(self.store.remove_note(1).text, "one")
        with self.assertRaises(IndexError):
            self.store.remove_note(5)
        self.store.set_off(True)
        self.assertTrue(self.read()["off"])
        self.assertEqual([n["text"] for n in self.read()["notes"]], ["two"])
        self.store.set_off(False)
        self.assertEqual(self.store.clear_notes(), 1)
        self.assertEqual(self.read()["notes"], [])

    def test_state_files_are_private_and_the_notes_public(self):
        self.store.add_note("one")
        self.store.salt()
        self.store.set_off(True)
        state = self.store.state_dir
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode) & 0o077, 0)
        self.assertEqual(sorted(os.listdir(state)), ["fleetnotes.lock", "off", "salt"])
        for name in ("fleetnotes.lock", "off", "salt"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state, name)).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.manual_path).st_mode), 0o644)
        self.assertEqual(os.path.basename(self.store.manual_path), "notes.json")
        self.assertEqual(os.path.basename(self.store.feed_path), "fleet.json")
        salt = self.store.salt()
        self.assertGreaterEqual(len(salt), 16)
        self.assertEqual(self.store.salt(), salt)  # created once, then stable


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = temp_store(self.tmp.name)
        os.makedirs(os.path.dirname(self.store.rules_path))
        with open(self.store.rules_path, "w", encoding="utf-8") as f:
            f.write(RULES_TEXT)
        snapshot = make_snapshot(FLEET)
        snapshot["generated"] = iso_utc(time.time())
        self.fleet = lambda *a, **k: FleetRead(snapshot)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self):
        with open(self.store.feed_path, encoding="utf-8") as f:
            return json.load(f)

    def test_refresh_writes_only_public_fields(self):
        line = refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.assertEqual(line, "Published: 5 notes · 2 counted only · 3 neither shown nor counted · "
                               "sources: main · header: FIRSTMATE · 6 crewmates · 1 waiting · 1 stuck")
        for word in SECRET_WORDS:
            self.assertNotIn(word, line)
        feed = self.feed()
        validate_feed(feed)
        published = json.dumps(feed).casefold()
        for task in FLEET:
            self.assertNotIn(task["id"], published)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.feed_path).st_mode), 0o644)

    def test_off_and_deny_remove_the_feed(self):
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.store.set_off(True)
        self.assertFalse(os.path.exists(self.store.feed_path))
        self.assertEqual(refresh_feed(self.store, "/opt/fm/main", read=self.fleet),
                         "Switched off with 'fleetnotes off': publishing nothing")
        self.assertFalse(os.path.exists(self.store.feed_path))
        self.store.set_off(False)
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.store.deny_project("lighthouse")
        self.assertTrue(apply_rules_now(self.store))
        self.assertFalse(os.path.exists(self.store.feed_path))

    def test_unlabel_applies_at_once_and_a_new_label_waits_for_the_fleet_check(self):
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        key = task_key(self.store.salt(), "lh-kumquat-overlay")
        note = lambda: next(n for n in self.feed()["notes"] if n["key"] == key)
        self.store.remove_label("lh-kumquat-overlay")
        self.assertTrue(apply_rules_now(self.store))
        self.assertIsNone(note()["label"])
        # A new label never appears without build_feed's check against every task id.
        self.store.set_label("lh-kumquat-overlay", "see lh-pomelo-tests")
        apply_rules_now(self.store)
        self.assertIsNone(note()["label"])
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.assertNotIn(key, [n["key"] for n in self.feed()["notes"]])  # carries another task's id
        self.store.set_label("lh-kumquat-overlay", "another label")
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.assertEqual(note()["label"], "another label")

    def test_off_survives_a_failed_write_and_a_broken_file(self):
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)

        def full_disk(*a, **k):
            raise OSError(28, "No space left on device")
        self.store._write_manual = full_disk
        with self.assertRaises(OSError):
            self.store.set_off(True)
        self.assertFalse(os.path.exists(self.store.feed_path))  # removed before the write
        self.assertIn("fleetnotes off", refresh_feed(self.store, "/opt/fm/main", read=self.fleet))
        del self.store._write_manual
        self.store.set_off(False)
        self.assertTrue(refresh_feed(self.store, "/opt/fm/main", read=self.fleet).startswith("Published"))
        with open(self.store.manual_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertTrue(self.store.read_manual()[0])
        self.assertIn("fleetnotes off", refresh_feed(self.store, "/opt/fm/main", read=self.fleet))
        self.assertFalse(os.path.exists(self.store.feed_path))

    def test_failed_read_keeps_last_feed(self):
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        before = self.feed()

        def broken(*a, **k):
            raise SnapshotError("the fleet view exited with code 1")
        line = refresh_feed(self.store, "/opt/fm/main", read=broken)
        self.assertEqual(line, "Could not read the fleet: the fleet view exited with code 1. "
                               "Keeping the last feed; the overlay hides it after 180 s")
        self.assertEqual(self.feed(), before)

    def test_refilter_needs_publishing_rules(self):
        refresh_feed(self.store, "/opt/fm/main", read=self.fleet)
        self.assertIsNone(refilter_feed(self.feed(), Rules(path="/srv/example/rules.conf"), SALT))


class ViewTestClock:
    """A test clock for reading a view: time passes only by the waits the reader makes in full.

    A wait that runs out its seconds moves the clock by those seconds, however long the machine
    really took; a wait that ends because the view ended costs nothing. Until ``started()`` (the
    view really runs) the clock stands still, so a slow start of the shell does not count either.
    """

    def __init__(self, started=lambda: True):
        self.now = 0.0
        self.started = started
        self.give_up = time.monotonic() + 60  # only turns a view that never starts into a failure

    def monotonic(self):
        return self.now

    def wait(self, proc, seconds):
        try:
            return proc.communicate(timeout=seconds)[0]  # a real wait: the view runs meanwhile
        except subprocess.TimeoutExpired:
            if self.started():
                self.now += seconds
            elif time.monotonic() > self.give_up:
                raise AssertionError("the view never started") from None
            raise


class SnapshotRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "bin"))
        self.view = os.path.join(self.home, "bin", "fm-fleet-snapshot.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def write_view(self, body):
        with open(self.view, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(self.view, 0o755)

    def test_reads_the_view_with_a_clean_environment(self):
        self.write_view('printf \'{"schema":"fm-fleet-snapshot.v1","arg":"%s","home":"%s","extra":"%s",'
                        '"forge":"%s","tasks":[]}\' "$1" "$FM_HOME" "${FM_STATE_OVERRIDE:-}" '
                        '"$FM_CREW_STATE_NO_FORGE"\n')
        os.environ["FM_STATE_OVERRIDE"] = "/srv/elsewhere"
        try:
            data = run_snapshot(self.home, timeout=10)
        finally:
            del os.environ["FM_STATE_OVERRIDE"]
        self.assertEqual((data["arg"], data["home"], data["extra"], data["forge"]),
                         ("--json", self.home, "", "1"))

    def test_errors_carry_no_fleet_text(self):
        with self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home)  # no view
        self.assertEqual(str(ctx.exception), "bin/fm-fleet-snapshot.sh not found in fleet.firstmate_home")
        self.write_view("echo kumquat-secret >&2; echo kumquat; exit 3\n")
        with self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home, timeout=10)
        self.assertEqual(str(ctx.exception), "the fleet view exited with code 3")
        self.write_view("echo kumquat\n")
        with self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home, timeout=10)
        self.assertEqual(str(ctx.exception), "the fleet view did not return JSON")
        self.write_view('echo \'{"schema":"other"}\'\n')
        with self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home, timeout=10)
        self.assertEqual(str(ctx.exception), "the fleet view does not have the expected schema")

    def test_timeout_and_stop_kill_the_view(self):
        marker = os.path.join(self.home, "child.pid")
        self.write_view(f"sleep 30 & echo $! > {marker}; wait\n")

        def started():  # the view's shell has written its child's pid
            try:
                with open(marker) as f:
                    return f.read().endswith("\n")
            except FileNotFoundError:
                return False
        clock = ViewTestClock(started)
        start = clock.monotonic()
        with mock.patch.object(fleetnotes.fleet, "_clock", clock), self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home, timeout=30, should_stop=lambda: clock.monotonic() - start > 1)
        self.assertEqual(str(ctx.exception), "read interrupted")
        self.assertLess(clock.monotonic() - start, 5)
        with open(marker) as f:
            child = int(f.read())
        for _ in range(40):  # the killed, orphaned sleep is reaped by the system shortly after
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.write_view("sleep 30\n")
        clock = ViewTestClock()
        start = clock.monotonic()
        with mock.patch.object(fleetnotes.fleet, "_clock", clock), self.assertRaises(SnapshotError) as ctx:
            run_snapshot(self.home, timeout=1)
        self.assertEqual(str(ctx.exception), "the view took longer than 1 s")
        self.assertLess(clock.monotonic() - start, 5)


class TitleTest(unittest.TestCase):
    """public_title: what a task does, from its backlog title, fit to publish."""

    def setUp(self):
        self.rules = rules_from(RULES_TEXT + "block clientx\n", blocked_phrases=["App Store Connect"])
        self.ids = [t["id"] for t in FLEET]

    def title(self, text, ids=None):
        return public_title(text, self.rules, self.ids if ids is None else ids)

    def test_prefix_and_asides_go(self):
        self.assertEqual(self.title("Lighthouse: the chat bot answers questions with the local model (option A)"),
                         "the chat bot answers questions with the local model")
        self.assertEqual(self.title("[2.3.0] Tidepool redesign E1: Statistics like Seashell on black (1S·1M)"),
                         "Statistics like Seashell on black")
        self.assertEqual(self.title("Slack integration"), "Slack integration")  # no prefix: as is

    def test_ids_paths_links_and_emails_go(self):
        cases = {
            "Driftwood: Slack integration for lh-kumquat-overlay (ABC-123)": "Slack integration",
            "Driftwood: Slack integration, see ABC-123 and #12": "Slack integration, see",
            "Lighthouse: check /Users/example/x/config.toml and the startup": "check and the startup",
            "Lighthouse: read ~/projects/fleet before the startup": "read before the startup",
            "Website: upload the new landing https://example.app/en today": "upload the new landing today",
            "Website: tell someone@example.com about the change": "tell about the change",
            "Lighthouse: fix fleet_notes.py and rules_conf in the startup 1a2b3c4d": "fix and in the startup",
            "Website: www.example.app with the new logo": "with the new logo",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                out = self.title(raw)
                self.assertEqual(out, expected)
                for needle in ("/", "@", "http", "www", "ABC-123", "#12", "_", "lh-kumquat"):
                    self.assertNotIn(needle, out)

    def test_a_blocked_word_drops_the_whole_title(self):
        # Like a label: whole or nothing. Removing only "client" or "price"
        # would publish the client's name or the price next to it.
        for raw in ("Seashell: raise prices of the yearly plan",
                    "Seashell: subir precios del plan anual",                    # the Spanish list too
                    "Seashell: raise the price of Seashell Pro to 4.99 a month",
                    "Integration with the client Globex Corp for Seashell",
                    "Driftwood: Slack integration for ClientX",                  # local block line
                    "Driftwood: tell the secretary about the change",            # "secret" inside a word
                    "Harbor: screenshots for App Store Connect and the listing", # blocked_phrases
                    "Lighthouse: rotate the Kick bot token",
                    "Lighthouse: the stream password"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.title(raw))
        self.assertIsNone(self.title(private_title("lh-kumquat-overlay")))

    def test_ids_written_with_spaces_and_private_names_drop_the_title(self):
        self.assertIsNone(self.title("Notes: lh kumquat overlay with who and what"))
        self.assertEqual(self.title("Notes: LH_Kumquat.Overlay with who and what"), "with who and what")  # token gone
        private = ["quarry", "buildbox"]
        self.assertIsNone(public_title("Port the Quarry login to Lighthouse", self.rules, self.ids,
                                       private=private))
        self.assertIsNone(public_title("Test the view on the new buildbox", self.rules, self.ids,
                                       private=private))
        self.assertEqual(public_title("Test the view of the quarrymen cottage", self.rules, self.ids,
                                      private=private), "Test the view of the quarrymen cottage")  # whole words

    def test_more_shapes_that_go(self):
        cases = {
            "Move logic to lighthouse/fleet and scripts/notes": "Move logic",
            "Publish on example.app and staging.quarry.internal today": "Publish on and today",
            "Fix AppDelegate.m and Package.resolved in Seashell": "Fix and in Seashell",
            "Test with 192.168.1.20 and port 4455": "Test with and port 4455",
            "SCOUT investigate the camera freeze watchdog": "investigate the camera freeze watchdog",
            "Redesign (for (client) Globex) of the overlay": "Redesign of the overlay",
            "Redesign the camera overlay (waiting on Globex": "Redesign the camera overlay",
            "Harbor: build 1.5.0 for the store": "build 1.5.0 for the store",           # versions stay
            "Lighthouse: Kick/Twitch chat on iOS/macOS": "Kick/Twitch chat on iOS/macOS",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.title(raw), expected)

    def test_the_cut_counts_wide_characters_double(self):
        out = self.title("Lighthouse: " + " ".join(["\U0001F389\U0001F389 party"] * 12))
        self.assertIsNotNone(out)
        self.assertLessEqual(text_width(out), LABEL_MAX)
        self.assertGreater(len(out) * 2, LABEL_MAX)  # far fewer characters than a plain-text cut keeps
        self.assertTrue(out.endswith("…"))

    def test_cut_at_a_word_boundary(self):
        raw = ("[2.2.0] Tidepool: build 23 of the test version with the per-app counter, the fix for "
               "the phantom shield and the redesign of the Live Activity and the island")
        out = self.title(raw)
        self.assertEqual(out, "build 23 of the test version with the per-app counter…")
        self.assertLessEqual(len(out), LABEL_MAX)
        self.assertTrue(out.endswith("…"))
        full = "build 23 of the test version with the per-app counter, the fix for the phantom shield"
        self.assertTrue(full.startswith(out[:-1]) and full[len(out) - 1] in " ,", out)
        self.assertFalse(out[:-1].endswith((" of", " the", " with", " for", ",")))
        quoted = self.title("Tidepool: in the rule detail screen, the footer covers the «This week» tile "
                            "when opening")
        self.assertEqual(quoted.count("«"), quoted.count("»"))  # never an open quote
        self.assertEqual(quoted, "in the rule detail screen, the footer covers…")
        self.assertLessEqual(len(quoted), LABEL_MAX)

    def test_odd_input(self):
        for raw in (None, 42, "", "   ", "Lighthouse:", "(all aside)", "a b"):
            self.assertIsNone(self.title(raw), raw)
        self.assertEqual(self.title("Seashell:​ dai​ly\tstreaks\x00"), "daily streaks")


class LabelOrTitleTest(unittest.TestCase):
    TITLES = {
        "lh-kumquat-overlay": "Lighthouse: notes with concrete sentences for each job",
        "sc-guanabana-research": "Lighthouse: research the voice chat (option B)",
    }

    def setUp(self):
        self.result = build_feed(make_snapshot(FLEET, self.TITLES), rules_from(), SALT, now=NOW)
        self.by_key = {n["key"]: n for n in self.result.feed["notes"]}

    def note(self, task_id):
        return self.by_key[task_key(SALT, task_id)]

    def test_an_approved_label_wins_over_the_title(self):
        note = self.note("lh-kumquat-overlay")
        self.assertEqual((note["label"], note["label_from"]), ("notes that move", "label"))
        self.assertEqual(note_text(note)[0], "Lighthouse · notes that move")

    def test_without_label_the_cleaned_title_is_published(self):
        note = self.note("sc-guanabana-research")
        self.assertEqual((note["label"], note["label_from"]), ("research the voice chat", "title"))
        self.assertEqual(note_text(note)[0], "Lighthouse · research the voice chat")

    def test_labels_take_sixty_characters(self):
        text = "x" * LABEL_MAX
        rules = rules_from(f'project lighthouse "Lighthouse"\nlabel lh-kumquat-overlay "{text}"\n')
        result = build_feed(make_snapshot(FLEET), rules, SALT, now=NOW)
        labels = [n["label"] for n in result.feed["notes"]]
        self.assertIn(text, labels)
        rules = rules_from(f'project lighthouse "Lighthouse"\nlabel lh-kumquat-overlay "{text}x"\n')
        decision = next(d for d in build_feed(make_snapshot(FLEET), rules, SALT, now=NOW).decisions
                        if d.task_id == "lh-kumquat-overlay")
        self.assertEqual(decision.reason, BLOCKED_LABEL)

    def test_a_title_naming_a_private_project_or_the_ssh_host_is_not_published(self):
        titles = {"sc-guanabana-research": "Lighthouse: port the Seashell panel to the stream",
                  "lh-kumquat-overlay": "Lighthouse: test the view on the new buildbox"}
        rules = rules_from('project lighthouse "Lighthouse"\n')
        remote = [Source("remote", remote_snapshot(), "alex@buildbox.example", "Studio Mac")]
        result = build_feed(make_snapshot(FLEET, titles), rules, SALT, now=NOW, extra=remote)
        labels = {n["key"]: n["label"] for n in result.feed["notes"]}
        self.assertIsNone(labels[task_key(SALT, "sc-guanabana-research")])  # "Seashell" is not allowed
        self.assertIsNone(labels[task_key(SALT, "lh-kumquat-overlay")])     # the remote's ssh host
        published = json.dumps(result.feed, ensure_ascii=False).casefold()
        self.assertNotIn("seashell", published)
        self.assertNotIn("buildbox", published)
        self.assertNotIn("alex", published)
        # The host's domain is not a private word: "example" is fine in a title.
        fine = build_feed(make_snapshot(FLEET, {"lh-kumquat-overlay":
                                                "Lighthouse: the example chat bot runs a local model"}),
                          rules, SALT, now=NOW, extra=remote)
        self.assertIn("the example chat bot runs a local model", json.dumps(fine.feed, ensure_ascii=False))

    def test_titles_of_private_projects_never_leave(self):
        titles = {"sh-zanahoria-sync": "Seashell: new streaks with widgets",
                  "ks-berenjena-render": "Kelp Studio: render of the kumquat ad"}
        result = build_feed(make_snapshot(FLEET, titles), rules_from(), SALT, now=NOW)
        published = json.dumps(result.feed, ensure_ascii=False).casefold()
        for word in ("streaks", "widgets", "render", "seashell", "kelp"):
            self.assertNotIn(word, published)
        reasons = {d.task_id: d.reason for d in result.decisions}
        self.assertEqual(reasons["sh-zanahoria-sync"], PRIVATE_PROJECT)


def remote_snapshot():
    """The other machine's view: one Harbor task, one Tidepool task, one private."""
    tasks = [make_task("hb-store-screenshots", "harbor", spawned_ago=900),
             make_task("tp-phantom-shield", "tidepool-app", "paused", source="status-log"),
             make_task("sh-pomelo-private", "seashell")]
    titles = {"hb-store-screenshots": "[1.5.0] Harbor: new store screenshots (en and es)",
              "tp-phantom-shield": "[2.2.0] Tidepool: window guard on the re-lock after expiry",
              "sh-pomelo-private": "Seashell: pomelo secret"}
    snap = make_snapshot(tasks, titles, home="/srv/example/studio/firstmate")
    snap["generated"] = iso_utc(GEN_TS - 3)
    return snap


def secondmate_snapshot():
    tasks = [make_task("dw-slack-integration", "driftwood", source="run-step", detail="ci running"),
             make_task("dw-done-recently", "driftwood", "done", source="status-log", event_age=60)]
    titles = {"dw-slack-integration": "Driftwood: Slack integration",
              "dw-done-recently": "Driftwood: Starter plan up to 3 projects and a notice to the admin "
                                  "when the widget passes the limit"}
    return make_snapshot(tasks, titles, home="/srv/example/worktrees/fm/1/firstmate")


SOURCES_RULES = RULES_TEXT + textwrap.dedent("""\
    project driftwood "Driftwood"
    project harbor "Harbor"
    project tidepool-app "Tidepool"
""")


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.extra = [Source("secondmate", secondmate_snapshot(), "driftwood-mate"),
                      Source("remote", remote_snapshot(), "studio-ssh-alias", "Studio Mac")]
        self.result = build_feed(make_snapshot(FLEET), rules_from(SOURCES_RULES), SALT, now=NOW,
                                 extra=self.extra)
        self.sentences = [note_text(n)[0] for n in self.result.feed["notes"]]

    def test_secondmate_work_is_attributed_to_the_secondmate(self):
        self.assertIn("Secondmate of Driftwood · Slack integration", self.sentences)
        done = next(n for n in self.result.feed["notes"] if n["state"] == "done" and n["project"] == "Driftwood")
        self.assertEqual(done["who"], "Secondmate of Driftwood")
        self.assertEqual(done["label"], "Starter plan up to 3 projects and a notice to the admin…")
        self.assertLessEqual(len(done["label"]), LABEL_MAX)

    def test_remote_work_names_the_machine(self):
        self.assertIn("Studio Mac: Harbor · new store screenshots", self.sentences)
        self.assertIn("Studio Mac: Tidepool · window guard on the re-lock after expiry", self.sentences)
        self.assertNotIn("Seashell", json.dumps(self.result.feed))  # private there too

    def test_counts_cover_every_home_and_no_private_tag_leaks(self):
        summary = self.result.feed["summary"]
        self.assertEqual(summary["validating"], 2)    # main + secondmate
        self.assertEqual(summary["running"], 3 + 2)   # main + remote (Harbor, private Seashell)
        self.assertEqual(summary["paused"], 1)
        self.assertEqual(summary["done_recent"], 2)
        published = json.dumps(self.result.feed, ensure_ascii=False).casefold()
        for needle in ("driftwood-mate", "studio-ssh-alias", "/srv/", "/users/", "dw-slack", "hb-store"):
            self.assertNotIn(needle, published)
        self.assertEqual(self.result.read, Counter({"main": 1, "secondmate": 1, "remote": 1}))
        validate_feed(self.result.feed)

    def test_plan_ids_name_the_source(self):
        sources = {d.task_id: d.source for d in self.result.decisions}
        self.assertEqual(sources["dw-slack-integration"], "secondmate driftwood-mate")
        self.assertEqual(sources["hb-store-screenshots"], "remote studio-ssh-alias")
        self.assertEqual(sources["lh-kumquat-overlay"], "")

    def test_a_blocked_machine_name_only_counts(self):
        extra = [Source("remote", remote_snapshot(), "studio", "Acme Mac"), self.extra[0]]
        result = build_feed(make_snapshot(FLEET), rules_from(SOURCES_RULES + "block acme\n"), SALT,
                            now=NOW, extra=extra)
        self.assertNotIn("Acme", json.dumps(result.feed))
        reasons = {d.task_id: (d.reason, d.detail) for d in result.decisions}
        self.assertEqual(reasons["hb-store-screenshots"][0], BLOCKED_PROJECT)
        self.assertIn("machine name blocked by the line 8 of fleetnotes/rules.conf",
                      reasons["hb-store-screenshots"][1])
        self.assertEqual(result.feed["summary"]["running"], 5)  # still counted

    def test_a_bad_extra_view_is_skipped_and_counted(self):
        stale = dict(remote_snapshot(), generated=iso_utc(NOW + 3600))
        foreign = dict(secondmate_snapshot(), schema="other")
        result = build_feed(make_snapshot(FLEET), rules_from(SOURCES_RULES), SALT, now=NOW,
                            extra=[Source("remote", stale, "studio", "Studio Mac"),
                                   Source("secondmate", foreign, "dw")],
                            unread=Counter({"remote": 1}))
        self.assertEqual(result.unread, Counter({"remote": 2, "secondmate": 1}))
        self.assertEqual({n["project"] for n in result.feed["notes"]}, {"Lighthouse"})
        self.assertIn("sources: main (unread: 1 secondmate, 2 remotes)", outcome_line(result))

    def test_the_same_id_in_two_homes_gets_two_keys(self):
        twin = make_snapshot([make_task("lh-kumquat-overlay", "lighthouse")])
        result = build_feed(make_snapshot(FLEET), rules_from(), SALT, now=NOW,
                            extra=[Source("secondmate", twin, "other-mate")])
        keys = [n["key"] for n in result.feed["notes"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_refilter_keeps_titles_and_drops_what_the_rules_now_forbid(self):
        feed = self.result.feed
        rules = rules_from(SOURCES_RULES)
        same = refilter_feed(feed, rules, SALT)
        self.assertEqual(same, feed)
        denied = refilter_feed(feed, rules_from(SOURCES_RULES.replace('project harbor "Harbor"\n', "")), SALT)
        self.assertNotIn("Harbor", json.dumps(denied))
        blocked = refilter_feed(feed, rules_from(SOURCES_RULES + "block slack\n"), SALT)
        slack = next(n for n in blocked["notes"] if n["key"] == task_key(SALT, "dw-slack-integration"))
        self.assertEqual((slack["label"], slack["label_from"]), (None, None))
        renamed = refilter_feed(feed, rules_from(SOURCES_RULES + "block studio\n"), SALT)
        self.assertNotIn("Studio Mac", json.dumps(renamed))  # its "who" is now blocked


class ReadFleetTest(unittest.TestCase):
    """read_fleet with Firstmate's view and ssh faked: which homes are read, and how."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.main_home = os.path.join(root, "firstmate")
        self.sm_homes = [os.path.join(root, f"sm{n}", "firstmate") for n in (1, 2)]
        for home in (self.main_home, *self.sm_homes):
            os.makedirs(home)
        tasks = [make_task("driftwood-mate", "firstmate", "parked", kind="secondmate"),
                 make_task("harbor-mate", "firstmate", "done", kind="secondmate"),
                 make_task("faraway-mate", "firstmate", "parked", kind="secondmate"),
                 make_task("myself-mate", "firstmate", "parked", kind="secondmate"),
                 make_task("lh-kumquat-overlay", "lighthouse")]
        homes = {"driftwood-mate": self.sm_homes[0], "harbor-mate": self.sm_homes[1],
                 "faraway-mate": "/srv/example/studio/firstmate", "myself-mate": self.main_home}
        for task in tasks:
            task["paths"]["home"] = {"path": homes.get(task["id"]), "present": True}
        self.main = make_snapshot(tasks, home=self.main_home)
        self.main["secondmate_current"] = {"records": [{"id": "faraway-mate", "remote": True, "host": "studio"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_secondmate_homes_are_the_local_registered_ones(self):
        self.assertEqual(secondmate_homes(self.main),
                         [("driftwood-mate", self.sm_homes[0]), ("harbor-mate", self.sm_homes[1])])

    def test_main_then_secondmates_then_remotes(self):
        calls = []

        def local(home, timeout, should_stop, fm_home=None):
            calls.append(("local", home, fm_home))
            if fm_home == self.sm_homes[1]:
                raise SnapshotError("the view took longer than 30 s")
            return self.main if fm_home is None else secondmate_snapshot()

        def remote(r, timeout, should_stop):
            calls.append(("remote", r.host, r.home))
            if r.host == "oldbox":
                raise SnapshotError("the remote machine does not answer over ssh")
            return remote_snapshot()

        remotes = [Remote("Studio Mac", "studio", "/opt/fm/studio"),
                   Remote("Old Mac", "oldbox", "/opt/fm/old")]
        read = read_fleet(self.main_home, 30, None, remotes=remotes, local=local, remote=remote)
        self.assertIn(("local", self.main_home, None), calls)
        self.assertIn(("local", self.main_home, self.sm_homes[0]), calls)  # the main view, that FM_HOME
        self.assertIn(("remote", "studio", "/opt/fm/studio"), calls)
        self.assertEqual([(s.origin, s.tag) for s in read.extra],
                         [("secondmate", "driftwood-mate"), ("remote", "studio")])
        self.assertEqual(read.unread(), Counter({"secondmate": 1, "remote": 1}))
        result = build_feed(read.main, rules_from(SOURCES_RULES), SALT, now=NOW,
                            extra=read.extra, unread=read.unread())
        line = outcome_line(result)
        self.assertIn("sources: main + 1 secondmate + 1 remote (unread: 1 secondmate, 1 remote)", line)
        for private in ("oldbox", "harbor-mate", "studio", "/srv", "/opt"):
            self.assertNotIn(private, line)

    def test_a_failed_main_read_stops_the_remote_reads(self):
        stopped = []

        def local(home, timeout, should_stop, fm_home=None):
            raise SnapshotError("the fleet view exited with code 2")

        def remote(r, timeout, should_stop):
            for _ in range(100):
                if should_stop():
                    stopped.append(r.host)
                    raise SnapshotError("read interrupted")
                time.sleep(0.02)
            return remote_snapshot()

        start = time.monotonic()
        with self.assertRaises(SnapshotError):
            read_fleet(self.main_home, 30, None, remotes=[Remote("Studio Mac", "studio", "/opt/fm/studio")],
                       local=local, remote=remote)
        self.assertEqual(stopped, ["studio"])
        self.assertLess(time.monotonic() - start, 1.5)

    def test_any_failure_after_the_main_read_stops_the_other_reads(self):
        stopped = []

        def local(home, timeout, should_stop, fm_home=None):
            if fm_home is not None:
                raise RuntimeError("bug in a secondmate read")  # not a SnapshotError
            return self.main

        def remote(r, timeout, should_stop):
            for _ in range(200):
                if should_stop():
                    stopped.append(r.host)
                    raise SnapshotError("read interrupted")
                time.sleep(0.02)
            return remote_snapshot()

        start = time.monotonic()
        with self.assertRaises(RuntimeError):
            read_fleet(self.main_home, 30, None, remotes=[Remote("Studio Mac", "studio", "/opt/fm/studio")],
                       local=local, remote=remote)
        self.assertEqual(stopped, ["studio"])
        self.assertLess(time.monotonic() - start, 2)

    def test_a_malformed_main_view_names_no_secondmate_home(self):
        for broken in ({"secondmate_current": {"records": 7}, "tasks": self.main["tasks"]},
                       dict(self.main, fm_home="/x\x00y")):
            with self.subTest(keys=sorted(broken)):
                homes = secondmate_homes(broken)
                self.assertIsInstance(homes, list)

    def test_refresh_publishes_without_the_machine_that_does_not_answer(self):
        store = temp_store(self.tmp.name)
        os.makedirs(os.path.dirname(store.rules_path))
        with open(store.rules_path, "w", encoding="utf-8") as f:
            f.write(SOURCES_RULES)
        main = dict(make_snapshot(FLEET), generated=iso_utc(time.time()))
        unreachable = FleetRead(main, statuses=[])
        unreachable.statuses.append(SourceStatus("remote", "studio", "Studio Mac",
                                                 "the remote machine does not answer over ssh"))
        line = refresh_feed(store, "/opt/fm/main", read=lambda *a, **k: unreachable)
        self.assertTrue(line.startswith("Published: 5 notes"), line)
        self.assertIn("unread: 1 remote", line)
        with open(store.feed_path, encoding="utf-8") as f:
            self.assertEqual({n["project"] for n in json.load(f)["notes"]}, {"Lighthouse"})


class RemoteSnapshotTest(unittest.TestCase):
    """run_remote_snapshot against a fake `ssh` first on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.argv_file = os.path.join(self.tmp.name, "argv")
        self.path = os.environ.get("PATH", os.defpath)
        os.environ["PATH"] = self.bin + os.pathsep + self.path
        self.remote = Remote("Studio Mac", "studio", "/opt/fm/studio")

    def tearDown(self):
        os.environ["PATH"] = self.path
        self.tmp.cleanup()

    def fake_ssh(self, body):
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > {self.argv_file}\n' + body)
        os.chmod(path, 0o755)

    def test_batch_mode_short_timeout_and_a_read_only_command(self):
        self.fake_ssh('printf \'{"schema":"fm-fleet-snapshot.v1","tasks":[]}\'\n')
        data = run_remote_snapshot(self.remote, timeout=10)
        self.assertEqual(data["schema"], "fm-fleet-snapshot.v1")
        with open(self.argv_file) as f:
            argv = f.read().splitlines()
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=5", argv)
        self.assertEqual(argv[argv.index("--") + 1], "studio")
        command = argv[-1]
        self.assertEqual(command, remote_command("/opt/fm/studio", 10))
        outer = shlex.split(command)
        self.assertEqual(outer[:3], ["exec", "/bin/sh", "-c"])      # whatever the login shell is
        inner = outer[3]
        self.assertTrue(inner.startswith("exec env -i "))
        self.assertIn(f"FM_SNAPSHOT_CACHE_DIR={NO_CACHE_DIR}", inner)
        self.assertIn("FM_HOME=/opt/fm/studio", inner)
        self.assertIn("perl -e 'alarm shift; exec @ARGV' 15 ", inner)  # the view ends on that machine too
        self.assertTrue(inner.endswith("/opt/fm/studio/bin/fm-fleet-snapshot.sh --json"))
        self.assertFalse(os.path.isdir(os.path.dirname(NO_CACHE_DIR)))  # the cache can never be created

    def test_unreachable_machine_raises_without_its_text(self):
        self.fake_ssh("echo 'ssh: connect to host studio port 22: Operation timed out' >&2; exit 255\n")
        with self.assertRaises(SnapshotError) as ctx:
            run_remote_snapshot(self.remote, timeout=10)
        self.assertEqual(str(ctx.exception), "the remote machine does not answer over ssh")
        self.fake_ssh("echo kumquat; exit 3\n")
        with self.assertRaises(SnapshotError) as ctx:
            run_remote_snapshot(self.remote, timeout=10)
        self.assertEqual(str(ctx.exception), "the remote machine's fleet view exited with code 3")
        self.assertNotIn("kumquat", str(ctx.exception))

    def test_a_hung_machine_is_given_up_on_time(self):
        self.fake_ssh("sleep 30\n")
        start = time.monotonic()
        with self.assertRaises(SnapshotError):
            run_remote_snapshot(self.remote, timeout=1)
        self.assertLess(time.monotonic() - start, 5)

    def test_home_is_quoted(self):
        inner = shlex.split(remote_command("/opt/fm/x; rm -rf ~"))[3]
        self.assertIn("FM_HOME='/opt/fm/x; rm -rf ~'", inner)

    @unittest.skipUnless(shutil.which("perl"), "the remote command needs perl (it ships with macOS and most Linux)")
    def test_the_remote_command_runs_under_sh(self):
        """A login-like shell runs the command, the real /bin/sh the view: the env is clean."""
        shell = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "bin"))
            view = os.path.join(home, "bin", "fm-fleet-snapshot.sh")
            with open(view, "w") as f:
                f.write('#!/bin/sh\nprintf \'{"schema":"fm-fleet-snapshot.v1","home":"%s","extra":"%s",'
                        '"cache":"%s","arg":"%s"}\' "$FM_HOME" "${FM_STATE_OVERRIDE:-}" '
                        '"$FM_SNAPSHOT_CACHE_DIR" "$1"\n')
            os.chmod(view, 0o755)
            env = dict(os.environ, FM_STATE_OVERRIDE="/srv/elsewhere")
            out = subprocess.run([shell, "-c", remote_command(home, 10)], capture_output=True,
                                 text=True, env=env, timeout=20).stdout
        data = json.loads(out)
        self.assertEqual((data["home"], data["extra"], data["cache"], data["arg"]),
                         (home, "", NO_CACHE_DIR, "--json"))


class SecondmateViewTest(unittest.TestCase):
    def test_a_secondmate_home_is_read_with_the_main_view_and_no_cache(self):
        with tempfile.TemporaryDirectory() as root:
            main, sm = os.path.join(root, "main"), os.path.join(root, "sm")
            os.makedirs(os.path.join(main, "bin"))
            os.makedirs(sm)
            view = os.path.join(main, "bin", "fm-fleet-snapshot.sh")
            with open(view, "w") as f:
                f.write('#!/bin/sh\nprintf \'{"schema":"fm-fleet-snapshot.v1","home":"%s","cache":"%s"}\' '
                        '"$FM_HOME" "${FM_SNAPSHOT_CACHE_DIR:-}"\n')
            os.chmod(view, 0o755)
            self.assertEqual(run_snapshot(main, 10)["cache"], "")
            data = run_snapshot(main, 10, fm_home=sm)
            self.assertEqual((data["home"], data["cache"]), (sm, NO_CACHE_DIR))


if __name__ == "__main__":
    unittest.main()
