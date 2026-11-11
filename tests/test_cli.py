"""Tests for the ``fleetnotes`` command (fleetnotes.cli) and the fleet watcher
(fleetnotes.fleet.watch).

The CLI runs as a real process (``python -m fleetnotes``) with a minimal
environment: HOME is an empty temp folder, no XDG_* or FLEETNOTES_* variable,
no config file. That is the first run of a new user: the captain's notes must
work with the defaults, and nothing printed may show the home folder's real
path (the terminal may be on stream). The watcher and ``fleet plan`` run in
process with the fleet read faked.

Run from the repository root:
    python -m unittest discover -s tests -t . -v
"""

import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PACKAGE_ROOT)

import fleetnotes.cli
from fleetnotes import __version__
from fleetnotes.config import FleetConfig, Remote, defaults
from fleetnotes.fleet import FleetRead, SnapshotError, validate_feed, watch
from fleetnotes.notes import MANUAL_SCHEMA, NoteStore, iso_utc
from tests.test_fleetnotes import FLEET, RULES_TEXT, make_snapshot  # fixtures only, no TestCase


class CliProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "PATH": os.environ.get("PATH", os.defpath),
                    "PYTHONPATH": PACKAGE_ROOT, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        self.public = os.path.join(self.home, ".local", "share", "fleetnotes", "public")
        self.state = os.path.join(self.home, ".local", "state", "fleetnotes")
        self.notes_path = os.path.join(self.public, "notes.json")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, **env):
        proc = subprocess.run([sys.executable, "-m", "fleetnotes", *args], capture_output=True, text=True,
                              encoding="utf-8", env={**self.env, **env}, cwd=self.tmp.name, timeout=60)
        # The terminal may be on stream: the home folder is always shown as ~.
        self.assertNotIn(self.home, proc.stdout + proc.stderr)
        return proc

    def notes(self):
        with open(self.notes_path, encoding="utf-8") as f:
            return json.load(f)

    def test_add_then_list_with_no_config(self):
        added = self.cli("add", "Chat: what is a crewmate?")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn('Temporary note published: "Chat: what is a crewmate?" (expires in 30 min)', added.stdout)
        data = self.notes()
        self.assertEqual(data["schema"], MANUAL_SCHEMA)
        self.assertEqual([(n["text"], n["pinned"]) for n in data["notes"]], [("Chat: what is a crewmate?", False)])
        self.assertEqual(stat.S_IMODE(os.stat(self.notes_path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode) & 0o077, 0)   # 0700 or stricter
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "fleetnotes.lock")).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))     # no config was needed

        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stderr, "")
        self.assertIn("Captain's notes — ~/.local/share/fleetnotes/public/notes.json", listed.stdout)
        self.assertIn("1. [temporary] Chat: what is a crewmate?  · expires in 30 min", listed.stdout)
        self.assertIn("Fleet rules — ~/.config/fleetnotes/rules.conf: none (nothing of the fleet is published)",
                      listed.stdout)
        self.assertIn("Fleet feed — ~/.local/share/fleetnotes/public/fleet.json: none", listed.stdout)

    def test_pin_rm_and_clear(self):
        pinned = self.cli("pin", "Today: notes that update themselves")
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertIn('Pinned note published: "Today: notes that update themselves" (no expiry)', pinned.stdout)
        later = self.cli("add", "Back", "in", "5", "--for", "1h30m")
        self.assertIn('Temporary note published: "Back in 5" (expires in 1 h 30)', later.stdout)
        listed = self.cli("list").stdout
        self.assertIn("1. [pinned] Today: notes that update themselves  · no expiry", listed)
        self.assertIn("2. [temporary] Back in 5", listed)

        removed = self.cli("rm", "1")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn('Removed: "Today: notes that update themselves"', removed.stdout)
        missing = self.cli("rm", "5")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("There is no note 5", missing.stderr)
        cleared = self.cli("clear")
        self.assertIn("Removed 1 captain's note(s).", cleared.stdout)
        self.assertEqual(self.notes()["notes"], [])
        self.assertIn("There were no captain's notes.", self.cli("clear").stdout)

    def test_off_and_on(self):
        self.cli("add", "Back in 5")
        off = self.cli("off")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertTrue(self.notes()["off"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "off")).st_mode), 0o600)
        self.assertIn("[OFF: fleetnotes off]", self.cli("list").stdout)
        while_off = self.cli("add", "Still here")
        self.assertEqual(while_off.returncode, 0)
        self.assertIn("The board is off", while_off.stderr)
        on = self.cli("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertFalse(self.notes()["off"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "off")))
        self.assertEqual([n["text"] for n in self.notes()["notes"]], ["Back in 5", "Still here"])

    def test_a_refused_note_exits_1_and_writes_nothing(self):
        blocked = self.cli("add", "my password is hunter2")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("Note refused: matches the built-in rule 'credentials'", blocked.stderr)
        self.assertNotIn("hunter2", blocked.stdout + blocked.stderr)   # the message names the rule only
        too_long = self.cli("add", "x" * 91)
        self.assertEqual(too_long.returncode, 1)
        self.assertIn("has 91 characters and 90 fit", too_long.stderr)
        self.assertFalse(os.path.exists(self.notes_path))
        self.assertFalse(os.path.exists(self.public))

    def test_version_and_usage(self):
        version = self.cli("--version")
        self.assertEqual((version.returncode, version.stdout), (0, f"fleetnotes {__version__}\n"))
        self.assertEqual(self.cli().returncode, 2)           # a command is required
        self.assertEqual(self.cli("add", "x", "--for", "soon").returncode, 2)

    def test_the_fleet_needs_a_config(self):
        watched = self.cli("fleet", "watch", "--once")
        self.assertEqual(watched.returncode, 2)
        self.assertIn("The fleet feed is off: set fleet.firstmate_home in the config", watched.stdout)
        planned = self.cli("fleet", "plan")
        self.assertEqual(planned.returncode, 2)
        self.assertIn("The fleet feed is off", planned.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.public, "fleet.json")))

    def test_plan_ids_refused_on_air(self):
        refused = self.cli("fleet", "plan", "--ids", FLEETNOTES_ON_AIR="1")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("On air: task ids are not printed", refused.stderr)

    def test_a_broken_or_missing_config_exits_2(self):
        with open(os.path.join(self.home, "broken.toml"), "w", encoding="utf-8") as f:
            f.write("output_dir = [\n")
        broken = self.cli("--config", "~/broken.toml", "list")
        self.assertEqual(broken.returncode, 2)
        self.assertIn("cannot read ~/broken.toml: TOMLDecodeError", broken.stderr)
        missing = self.cli("--config", "~/missing.toml", "list")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("config file not found: ~/missing.toml", missing.stderr)
        by_env = self.cli("list", FLEETNOTES_CONFIG=os.path.join(self.home, "missing.toml"))
        self.assertEqual(by_env.returncode, 2)
        self.assertIn("config file not found: ~/missing.toml", by_env.stderr)
        default = os.path.join(self.home, ".config", "fleetnotes", "config.toml")
        os.makedirs(os.path.dirname(default))
        with open(default, "w", encoding="utf-8") as f:
            f.write("[fleet\n")
        self.assertEqual(self.cli("list").returncode, 2)       # a broken default file too
        self.assertFalse(os.path.exists(self.public))

    def test_config_values_and_warnings(self):
        default = os.path.join(self.home, ".config", "fleetnotes", "config.toml")
        os.makedirs(os.path.dirname(default))
        with open(default, "w", encoding="utf-8") as f:
            f.write('output_dir = "out"\nnope = 1\n[serve]\nport = 0\n')
        added = self.cli("add", "Back in 5")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("warn  config: unknown key 'nope' ignored", added.stderr)
        self.assertIn("warn  config: [serve] port must be an integer from 1 to 65535", added.stderr)
        listed = self.cli("list")
        self.assertIn("Captain's notes — ~/.config/fleetnotes/out/notes.json", listed.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config", "fleetnotes", "out", "notes.json")))


def fresh_fleet():
    snapshot = make_snapshot(FLEET)
    snapshot["generated"] = iso_utc(time.time())
    return snapshot


class WatchTest(unittest.TestCase):
    """fleetnotes.fleet.watch with the fleet read faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.store = NoteStore(os.path.join(root, "public"), os.path.join(root, "config", "rules.conf"),
                               os.path.join(root, "state"))
        os.makedirs(os.path.dirname(self.store.rules_path))
        with open(self.store.rules_path, "w", encoding="utf-8") as f:
            f.write(RULES_TEXT)
        self.fleet = FleetConfig(firstmate_home="/opt/fm/main",
                                 remotes=[Remote("Studio Mac", "studio", "/opt/fm/studio")])
        self.snapshot = fresh_fleet()
        self.reads = []
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, home, timeout, should_stop, remotes=()):
        self.reads.append((home, timeout, list(remotes)))
        return FleetRead(self.snapshot)

    def log(self, tag, line):
        self.logs.append((tag, line))

    def write_old_feed(self):
        os.makedirs(self.store.output_dir, exist_ok=True)
        with open(self.store.feed_path, "w", encoding="utf-8") as f:
            f.write('{"schema": "fleetnotes-feed.v1", "notes": []}')

    def test_once_publishes_and_keeps_the_feed(self):
        code = watch(self.store, self.fleet, once=True, read=self.read, log=self.log)
        self.assertEqual(code, 0)
        self.assertEqual(self.reads, [("/opt/fm/main", 45.0, self.fleet.remotes)])
        with open(self.store.feed_path, encoding="utf-8") as f:
            feed = json.load(f)
        validate_feed(feed)
        self.assertEqual(len(feed["notes"]), 5)
        self.assertEqual([tag for tag, _ in self.logs], ["OK", "PUB", "OK"])
        self.assertEqual(self.logs[0][1], "fleet watch: every 60s · main home and its secondmates · "
                                          "remotes: Studio Mac")
        self.assertTrue(self.logs[1][1].startswith("Published: 5 notes · "), self.logs[1][1])
        self.assertEqual(self.logs[2], ("OK", "Stopped"))
        for tag, line in self.logs:  # counts and rule names only
            for task in FLEET:
                self.assertNotIn(task["id"], line)

    def test_a_stop_ends_the_loop_and_removes_the_feed(self):
        self.write_old_feed()
        published = []

        def log(tag, line):
            self.log(tag, line)
            if tag == "PUB":
                published.append(os.path.exists(self.store.feed_path))

        start = time.monotonic()
        code = watch(self.store, self.fleet, should_stop=lambda: bool(published), read=self.read, log=log)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - start, 3)     # no waiting out the interval once stopped
        self.assertEqual(published, [True])
        self.assertEqual(len(self.reads), 1)
        self.assertFalse(os.path.exists(self.store.feed_path))
        self.assertEqual([line for _, line in self.logs if not line.startswith(("fleet watch", "Published"))],
                         ["Removed the feed of the previous session", "Feed removed", "Stopped"])

    def test_a_stop_during_the_read_publishes_no_outcome(self):
        def read(*args, **kwargs):
            self.reads.append(args)
            return FleetRead(self.snapshot)

        code = watch(self.store, self.fleet, should_stop=lambda: bool(self.reads), read=read, log=self.log)
        self.assertEqual(code, 0)
        self.assertNotIn("PUB", [tag for tag, _ in self.logs])
        self.assertFalse(os.path.exists(self.store.feed_path))
        self.assertEqual(self.logs[-2:], [("OK", "Feed removed"), ("OK", "Stopped")])

    def test_a_disabled_feed_returns_2_without_reading(self):
        code = watch(self.store, FleetConfig(), read=self.read, log=self.log)
        self.assertEqual(code, 2)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.logs, [("WRN", "The fleet feed is off: set fleet.firstmate_home in the config")])

    def test_failed_reads_are_logged_as_warnings_without_their_text(self):
        def broken(*args, **kwargs):
            raise SnapshotError("the fleet view exited with code 1")

        def crashing(*args, **kwargs):
            raise RuntimeError("kumquat in a traceback")

        self.assertEqual(watch(self.store, self.fleet, once=True, read=broken, log=self.log), 0)
        self.assertEqual(self.logs[1], ("WRN", "Could not read the fleet: the fleet view exited with code 1. "
                                               "Keeping the last feed; the overlay hides it after 180 s"))
        self.logs.clear()
        self.assertEqual(watch(self.store, self.fleet, once=True, read=crashing, log=self.log), 0)
        self.assertEqual(self.logs[1], ("WRN", "Could not refresh: RuntimeError"))
        self.assertNotIn("kumquat", " ".join(line for _, line in self.logs))

    def test_the_interval_is_kept_between_5_and_90_seconds(self):
        for interval, shown in ((1, "5"), (30, "30"), (500, "90")):
            with self.subTest(interval=interval):
                self.logs.clear()
                watch(self.store, self.fleet, once=True, interval=interval, read=self.read, log=self.log)
                self.assertTrue(self.logs[0][1].startswith(f"fleet watch: every {shown}s "), self.logs[0][1])


class PlanTest(unittest.TestCase):
    """``fleetnotes fleet plan`` in process, with the fleet read faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.cfg = defaults(os.path.join(root, "config", "config.toml"), {"HOME": os.path.join(root, "home")})
        self.cfg.fleet = FleetConfig(firstmate_home="/opt/fm/main")
        self.store = NoteStore(os.path.join(root, "public"), os.path.join(root, "config", "rules.conf"),
                               os.path.join(root, "state"))
        os.makedirs(os.path.dirname(self.store.rules_path))
        with open(self.store.rules_path, "w", encoding="utf-8") as f:
            f.write(RULES_TEXT)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, ids, on_air, read):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fleetnotes.cli, "read_fleet", read), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fleetnotes.cli.cmd_plan(self.store, argparse.Namespace(ids=ids), self.cfg, on_air=on_air)
        return code, out.getvalue(), err.getvalue()

    def test_plan_ids_refused_on_air(self):
        def must_not_read(*args, **kwargs):
            self.fail("the fleet must not be read")

        code, out, err = self.plan(True, lambda: True, must_not_read)
        self.assertEqual(code, 1)
        self.assertIn("On air: task ids are not printed", err)
        self.assertEqual(out, "")

    def test_plan_prints_the_feed_without_ids(self):
        code, out, err = self.plan(False, lambda: True, lambda *a, **k: FleetRead(fresh_fleet()))
        self.assertEqual(code, 0, err)
        self.assertIn("Would publish to", out)
        self.assertIn("FIRSTMATE · 6 crewmates · 1 waiting · 1 stuck", out)
        self.assertIn("Lighthouse · notes that move  — running  [label]", out)
        for task in FLEET:
            self.assertNotIn(task["id"], out + err)
        self.assertFalse(os.path.exists(self.store.feed_path))   # a preview publishes nothing

    def test_plan_ids_off_air_names_the_tasks(self):
        code, out, err = self.plan(True, lambda: False, lambda *a, **k: FleetRead(fresh_fleet()))
        self.assertEqual(code, 0, err)
        self.assertIn("Private list with task ids", err)
        for task in FLEET:
            self.assertIn(task["id"], out)

    def test_a_failed_read_exits_1(self):
        def broken(*args, **kwargs):
            raise SnapshotError("the fleet view exited with code 1")

        code, out, err = self.plan(False, lambda: False, broken)
        self.assertEqual(code, 1)
        self.assertIn("Could not read the fleet: the fleet view exited with code 1", err)


if __name__ == "__main__":
    unittest.main()
