"""Tests for fleetnotes.config: defaults, XDG folders, the config file and every bad value.

Nothing here reads the real home folder: every call gets its own ``env``
mapping (HOME included) and files go to temp dirs. A bad value never stops
anything: it keeps its default and adds a line to ``Config.warnings``.

Run from the repository root:
    python -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleetnotes.config import (
    DEFAULT_PORT, FLEET_MAX_SECONDS, MAX_REMOTES, Config, ConfigError, FleetConfig, Remote, ServeConfig,
    default_config_path, defaults, from_dict, load_config,
)

HOME = "/srv/example/home"
ENV = {"HOME": HOME}
CONFIG = "/srv/example/conf/fleetnotes/config.toml"   # never read: from_dict only uses its folder


def parse(data, path=CONFIG, env=ENV) -> Config:
    return from_dict(data, path, env)


class DefaultsTest(unittest.TestCase):
    def test_without_xdg(self):
        cfg = defaults(env=ENV)
        self.assertEqual(cfg.output_dir, f"{HOME}/.local/share/fleetnotes/public")
        self.assertEqual(cfg.state_dir, f"{HOME}/.local/state/fleetnotes")
        self.assertEqual(cfg.rules_file, f"{HOME}/.config/fleetnotes/rules.conf")
        self.assertEqual(cfg.blocked_phrases, [])
        self.assertEqual(cfg.serve, ServeConfig("127.0.0.1", DEFAULT_PORT))
        self.assertEqual(DEFAULT_PORT, 8765)
        self.assertEqual(cfg.fleet, FleetConfig())
        self.assertEqual(cfg.fleet.firstmate_home, "")
        self.assertFalse(cfg.fleet.enabled)               # the fleet feed is off by default
        self.assertEqual(cfg.fleet.remotes, [])
        self.assertEqual((cfg.path, cfg.warnings), ("", []))
        self.assertEqual(default_config_path(ENV), f"{HOME}/.config/fleetnotes/config.toml")

    def test_with_xdg(self):
        env = dict(ENV, XDG_CONFIG_HOME="/srv/example/xdg/config", XDG_DATA_HOME="/srv/example/xdg/data",
                   XDG_STATE_HOME="/srv/example/xdg/state")
        cfg = defaults(env=env)
        self.assertEqual(cfg.output_dir, "/srv/example/xdg/data/fleetnotes/public")
        self.assertEqual(cfg.state_dir, "/srv/example/xdg/state/fleetnotes")
        self.assertEqual(cfg.rules_file, "/srv/example/xdg/config/fleetnotes/rules.conf")
        self.assertEqual(default_config_path(env), "/srv/example/xdg/config/fleetnotes/config.toml")

    def test_relative_or_empty_xdg_values_are_ignored(self):
        env = dict(ENV, XDG_CONFIG_HOME="relative/config", XDG_DATA_HOME="", XDG_STATE_HOME="state")
        self.assertEqual(defaults(env=env), defaults(env=ENV))
        self.assertEqual(default_config_path(env), default_config_path(ENV))

    def test_fleetnotes_config_names_the_file(self):
        env = dict(ENV, FLEETNOTES_CONFIG="/srv/example/elsewhere/fn.toml",
                   XDG_CONFIG_HOME="/srv/example/xdg/config")
        self.assertEqual(default_config_path(env), "/srv/example/elsewhere/fn.toml")
        self.assertEqual(defaults(env=env).rules_file, "/srv/example/elsewhere/rules.conf")
        self.assertEqual(default_config_path(dict(ENV, FLEETNOTES_CONFIG="~/fn.toml")), f"{HOME}/fn.toml")
        self.assertEqual(default_config_path(dict(ENV, FLEETNOTES_CONFIG="conf/fn.toml")),
                         os.path.join(os.getcwd(), "conf", "fn.toml"))

    def test_an_explicit_path_puts_the_rules_next_to_it(self):
        self.assertEqual(defaults("/srv/example/x/c.toml", ENV).rules_file, "/srv/example/x/rules.conf")

    def test_without_home_in_env_the_process_home_is_used(self):
        cfg = defaults("/srv/example/x/c.toml", {})
        self.assertEqual(cfg.output_dir, os.path.join(os.path.expanduser("~"), ".local", "share",
                                                      "fleetnotes", "public"))


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_a_missing_default_file_gives_the_defaults(self):
        cfg = load_config(None, self.env)
        self.assertEqual(cfg, defaults(None, self.env))
        self.assertEqual((cfg.path, cfg.warnings), ("", []))
        self.assertEqual(os.listdir(self.home), [])  # nothing created

    def test_a_named_missing_file_is_an_error(self):
        missing = os.path.join(self.tmp.name, "nope.toml")
        with self.assertRaises(ConfigError) as ctx:
            load_config(missing, self.env)
        self.assertEqual(str(ctx.exception), f"config file not found: {missing}")
        with self.assertRaises(ConfigError):
            load_config(None, dict(self.env, FLEETNOTES_CONFIG=missing))
        with self.assertRaises(ConfigError):
            load_config("~/nope.toml", self.env)

    def test_a_broken_or_unreadable_file_is_an_error(self):
        broken = self.write(os.path.join(self.tmp.name, "broken.toml"), "output_dir = [\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken, self.env)
        self.assertEqual(str(ctx.exception), f"cannot read {broken}: TOMLDecodeError")
        with self.assertRaises(ConfigError):
            load_config(self.tmp.name, self.env)  # a folder
        # Only a missing default file is fine: a broken one is still an error.
        self.write(default_config_path(self.env), "fleet = \n")
        with self.assertRaises(ConfigError):
            load_config(None, self.env)

    def test_reads_a_file(self):
        path = self.write(os.path.join(self.home, ".config", "fleetnotes", "config.toml"), (
            'output_dir = "public"\n'
            'blocked_phrases = ["Billing"]\n'
            "[serve]\nport = 9001\n"
            '[fleet]\nfirstmate_home = "~/firstmate"\nrefresh_seconds = 30\n'
            '[[fleet.remotes]]\nname = "Studio Mac"\nhost = "studio"\nhome = "/opt/fm/studio"\n'))
        cfg = load_config(None, self.env)
        self.assertEqual(cfg.path, path)
        self.assertEqual(cfg.output_dir, os.path.join(self.home, ".config", "fleetnotes", "public"))
        self.assertEqual(cfg.blocked_phrases, ["Billing"])
        self.assertEqual(cfg.serve.port, 9001)
        self.assertEqual(cfg.fleet.firstmate_home, os.path.join(self.home, "firstmate"))
        self.assertTrue(cfg.fleet.enabled)
        self.assertEqual(cfg.fleet.refresh_seconds, 30.0)
        self.assertEqual(cfg.fleet.remotes, [Remote("Studio Mac", "studio", "/opt/fm/studio")])
        self.assertEqual(cfg.warnings, [])

    def test_fleetnotes_config_and_a_tilde_path_are_read(self):
        path = self.write(os.path.join(self.home, "fn.toml"), "state_dir = \"st\"\n")
        for cfg in (load_config(None, dict(self.env, FLEETNOTES_CONFIG=path)),
                    load_config("~/fn.toml", self.env)):
            self.assertEqual(cfg.path, path)
            self.assertEqual(cfg.state_dir, os.path.join(self.home, "st"))
            self.assertEqual(cfg.rules_file, os.path.join(self.home, "rules.conf"))


class FromDictTest(unittest.TestCase):
    def test_happy_path(self):
        cfg = parse({
            "output_dir": "/srv/example/out", "state_dir": "/srv/example/state",
            "rules_file": "/srv/example/private/rules.conf",
            "blocked_phrases": ["Billing", "  Admin console  ", ""],
            "serve": {"host": "127.0.0.1", "port": 9000},
            "fleet": {"firstmate_home": "/opt/fm/main", "refresh_seconds": 30, "snapshot_timeout": 20.5,
                      "remotes": [{"name": "Studio Mac", "host": "studio", "home": "/opt/fm/studio"},
                                  {"name": "Build box", "host": "alex@buildbox.example", "home": "/opt/fm/build"}]},
        })
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.path, CONFIG)
        self.assertEqual((cfg.output_dir, cfg.state_dir, cfg.rules_file),
                         ("/srv/example/out", "/srv/example/state", "/srv/example/private/rules.conf"))
        self.assertEqual(cfg.blocked_phrases, ["Billing", "Admin console"])
        self.assertEqual(cfg.serve, ServeConfig("127.0.0.1", 9000))
        self.assertEqual(cfg.fleet, FleetConfig(
            "/opt/fm/main", 30.0, 20.5,
            [Remote("Studio Mac", "studio", "/opt/fm/studio"),
             Remote("Build box", "alex@buildbox.example", "/opt/fm/build")]))
        self.assertIsInstance(cfg.fleet.refresh_seconds, float)

    def test_relative_paths_follow_the_file_and_tilde_the_home(self):
        cfg = parse({"output_dir": "public", "state_dir": "~/state", "rules_file": "../rules/rules.conf",
                     "fleet": {"firstmate_home": "fm"}})
        self.assertEqual(cfg.output_dir, "/srv/example/conf/fleetnotes/public")
        self.assertEqual(cfg.state_dir, f"{HOME}/state")
        self.assertEqual(cfg.rules_file, "/srv/example/conf/rules/rules.conf")
        self.assertEqual(cfg.fleet.firstmate_home, "/srv/example/conf/fleetnotes/fm")
        self.assertEqual(parse({"output_dir": "~"}).output_dir, HOME)
        self.assertEqual(parse({"fleet": {"firstmate_home": "~/fm/"}}).fleet.firstmate_home, f"{HOME}/fm")
        empty = parse({"fleet": {"firstmate_home": "  "}})
        self.assertEqual((empty.fleet.firstmate_home, empty.fleet.enabled, empty.warnings), ("", False, []))

    def test_bad_paths_keep_their_defaults(self):
        base = defaults(CONFIG, ENV)
        for key in ("output_dir", "state_dir", "rules_file"):
            for value in (5, "", "   ", "a\nb", ["x"], None):
                with self.subTest(key=key, value=value):
                    cfg = parse({key: value})
                    self.assertEqual(getattr(cfg, key), getattr(base, key))
                    self.assertEqual(len(cfg.warnings), 1)
                    self.assertIn(key, cfg.warnings[0])

    def test_bad_blocked_phrases(self):
        for value in ("Billing", ["Billing", 3], {"a": "b"}):
            with self.subTest(value=value):
                cfg = parse({"blocked_phrases": value})
                self.assertEqual(cfg.blocked_phrases, [])
                self.assertEqual(cfg.warnings, ["blocked_phrases must be a list of strings — none used"])

    def test_serve(self):
        for port in (1, 8765, 65535):
            self.assertEqual(parse({"serve": {"port": port}}).serve.port, port)
        for port in (0, -1, 65536, "8765", True, 8765.0):
            with self.subTest(port=port):
                cfg = parse({"serve": {"port": port}})
                self.assertEqual(cfg.serve.port, DEFAULT_PORT)
                self.assertEqual(cfg.warnings, ["[serve] port must be an integer from 1 to 65535 — using 8765"])
        for host in ("127.0.0.1", "127.0.0.2", "localhost", "::1", " 127.0.0.1 "):
            with self.subTest(host=host):
                cfg = parse({"serve": {"host": host}})
                self.assertEqual((cfg.serve.host, cfg.warnings), (host.strip(), []))
        for host in ("0.0.0.0", "192.168.1.20", "::", "studio"):
            with self.subTest(host=host):
                cfg = parse({"serve": {"host": host}})
                self.assertEqual(cfg.serve.host, host)          # allowed, but said out loud
                self.assertEqual(len(cfg.warnings), 1)
                self.assertIn("is not a loopback address", cfg.warnings[0])
        for host in ("", "  ", 5):
            with self.subTest(host=host):
                cfg = parse({"serve": {"host": host}})
                self.assertEqual(cfg.serve.host, "127.0.0.1")
                self.assertEqual(len(cfg.warnings), 1)
        self.assertEqual(parse({"serve": "127.0.0.1"}).warnings, ["[serve] must be a table — using defaults"])
        self.assertEqual(parse({"serve": {"bind": "x"}}).warnings, ["[serve] unknown key 'bind' ignored"])

    def test_fleet_values_and_caps(self):
        cfg = parse({"fleet": {"firstmate_home": "/opt/fm", "refresh_seconds": 30,
                               "snapshot_timeout": 1, "nope": 1}, "rules_file": ""})
        self.assertEqual(cfg.fleet.firstmate_home, "/opt/fm")
        self.assertEqual(cfg.fleet.refresh_seconds, 30.0)
        self.assertEqual(cfg.fleet.snapshot_timeout, 45.0)   # below 5: default kept
        self.assertEqual(cfg.rules_file, defaults(CONFIG, ENV).rules_file)
        self.assertEqual(len(cfg.warnings), 3)
        # Refresh + read above the overlay's 180 s staleness would blank the notes.
        capped = parse({"fleet": {"refresh_seconds": 91, "snapshot_timeout": 61}})
        self.assertEqual((capped.fleet.refresh_seconds, capped.fleet.snapshot_timeout), (60.0, 45.0))
        self.assertEqual(capped.warnings, ["[fleet] refresh_seconds must be a number from 5 to 90 — using 60",
                                           "[fleet] snapshot_timeout must be a number from 5 to 60 — using 45"])
        top = parse({"fleet": {"refresh_seconds": 90, "snapshot_timeout": 60}}).fleet
        self.assertEqual((top.refresh_seconds, top.snapshot_timeout), (90.0, 60.0))
        self.assertLess(top.refresh_seconds + top.snapshot_timeout, 180)
        self.assertEqual(FLEET_MAX_SECONDS, {"refresh_seconds": 90, "snapshot_timeout": 60})
        bottom = parse({"fleet": {"refresh_seconds": 5, "snapshot_timeout": 5.0}})
        self.assertEqual((bottom.fleet.refresh_seconds, bottom.fleet.snapshot_timeout, bottom.warnings),
                         (5.0, 5.0, []))
        for value in (4.9, True, "60", math.nan, math.inf, None):
            with self.subTest(value=value):
                cfg = parse({"fleet": {"refresh_seconds": value, "snapshot_timeout": value}})
                self.assertEqual((cfg.fleet.refresh_seconds, cfg.fleet.snapshot_timeout), (60.0, 45.0))
                self.assertEqual(len(cfg.warnings), 2)

    def test_bad_fleet_tables(self):
        self.assertEqual(parse({"fleet": "on"}).warnings, ["[fleet] must be a table — using defaults"])
        cfg = parse({"fleet": {"firstmate_home": 5}})
        self.assertEqual((cfg.fleet.firstmate_home, cfg.fleet.enabled), ("", False))
        self.assertEqual(cfg.warnings,
                         ["[fleet] firstmate_home must be a path (empty turns the feed off) — feed off"])
        self.assertEqual(parse({"fleet": {"firstmate_home": "/opt/fm\n"}}).fleet.firstmate_home, "")

    def test_remotes(self):
        good = {"name": "Studio Mac", "host": "studio", "home": "/opt/fm/studio/"}
        cfg = parse({"fleet": {"remotes": [
            good,
            {"name": "Bad", "host": "-oProxyCommand=evil", "home": "/x"},  # an ssh option, not a host
            {"name": "Bad", "host": "studio;reboot", "home": "/x"},        # not a host either
            {"name": "Bad", "host": "studio", "home": "fm/studio"},        # relative
            {"name": "Bad", "host": "studio", "home": "/opt/fm\n"},        # not printable
            {"name": "", "host": "studio", "home": "/x"},
            {"name": "  ", "host": "studio", "home": "/x"},
            {"name": "Bad", "host": "studio", "home": "/x", "user": "root"},  # unknown key
            {"name": "Bad", "host": "studio"},                                # missing key
            "studio",                                                         # not a table
        ]}})
        self.assertEqual(cfg.fleet.remotes, [Remote("Studio Mac", "studio", "/opt/fm/studio")])
        self.assertEqual(len(cfg.warnings), 9)
        for warning in cfg.warnings:
            self.assertTrue(warning.startswith("[[fleet.remotes]]"), warning)
            self.assertTrue(warning.endswith("entry ignored"), warning)
        self.assertNotIn("evil", " ".join(cfg.warnings))   # warnings name the rule, not the value
        root = parse({"fleet": {"remotes": [{"name": " Build box ", "host": "alex@buildbox.example",
                                             "home": "/"}]}})
        self.assertEqual(root.fleet.remotes, [Remote("Build box", "alex@buildbox.example", "/")])
        not_a_list = parse({"fleet": {"remotes": "studio"}})
        self.assertEqual(not_a_list.fleet.remotes, [])
        self.assertEqual(len(not_a_list.warnings), 1)
        self.assertEqual(defaults(CONFIG, ENV).fleet.remotes, [])

    def test_at_most_four_remotes(self):
        many = [{"name": f"Mac {n}", "host": f"mac{n}", "home": "/opt/fm"} for n in range(MAX_REMOTES + 2)]
        cfg = parse({"fleet": {"remotes": many}})
        self.assertEqual(MAX_REMOTES, 4)
        self.assertEqual([r.host for r in cfg.fleet.remotes], ["mac0", "mac1", "mac2", "mac3"])
        self.assertEqual(cfg.warnings, ["[fleet] at most 4 remotes — the rest are ignored"])

    def test_unknown_keys_are_warned_and_ignored(self):
        cfg = parse({"sticky": {"refresh_seconds": 30}, "firstmate_home": "/opt/fm"})
        self.assertEqual(cfg.warnings, ["unknown key 'sticky' ignored", "unknown key 'firstmate_home' ignored"])
        self.assertEqual(cfg.fleet, FleetConfig())
        self.assertEqual(parse({"fleet": {"rules_file": "x"}}).warnings, ["[fleet] unknown key 'rules_file' ignored"])

    def test_warnings_are_never_printed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cfg = parse({"nope": 1, "serve": {"port": 0}, "fleet": {"refresh_seconds": 1000,
                                                                    "remotes": [{"name": "x"}]}})
        self.assertEqual(len(cfg.warnings), 4)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


if __name__ == "__main__":
    unittest.main()
