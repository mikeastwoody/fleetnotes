"""fleetnotes configuration: one optional TOML file, every key optional.

The file is ``$FLEETNOTES_CONFIG``, else ``$XDG_CONFIG_HOME/fleetnotes/config.toml``
(``~/.config/fleetnotes/config.toml``). Without it the captain's notes work
with the defaults below and the fleet feed is off::

    output_dir = "~/.local/share/fleetnotes/public"  # the overlay's two JSON files
    state_dir  = "~/.local/state/fleetnotes"         # lock, salt, private copy of `off`
    rules_file = "rules.conf"                        # private fleet rules (next to this file)
    blocked_phrases = ["Billing", "Admin console"]   # extra phrases no public text may hold

    [serve]                    # `fleetnotes serve`: the overlay for your streaming software
    host = "127.0.0.1"
    port = 8765

    [fleet]                    # optional: what your Firstmate crew is doing
    firstmate_home = ""        # empty: the fleet feed is off
    refresh_seconds = 60       # 5-90
    snapshot_timeout = 45      # 5-60

    [[fleet.remotes]]          # up to 4 other machines, read over ssh
    name = "Studio Mac"        # public: the overlay shows "Studio Mac: <project>"
    host = "studio"            # an ssh destination or ~/.ssh/config alias
    home = "/Users/you/firstmate"

``output_dir`` and ``state_dir`` default to ``$XDG_DATA_HOME`` and
``$XDG_STATE_HOME`` when those are set. A relative path in the file is
relative to the file's folder; ``~`` is the home folder. A bad value keeps
its default and adds a line to ``Config.warnings``; nothing here exits.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Mapping

APP = "fleetnotes"
DEFAULT_PORT = 8765
FLEET_MAX_SECONDS = {"refresh_seconds": 90, "snapshot_timeout": 60}
"""The overlay hides a fleet feed older than 180 s: a refresh starts every
``refresh_seconds`` and a read gives up after ``snapshot_timeout`` (plus a few
seconds to stop the view), so a feed is replaced before 90 + 60 + ~6 s."""
MAX_REMOTES = 4
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,252}")


class ConfigError(Exception):
    """The config file named explicitly cannot be read."""


@dataclass(frozen=True)
class Remote:
    """One Firstmate home on another machine, read over ssh ([[fleet.remotes]]).

    ``name`` is public (the overlay shows "Studio Mac: <project> · ...") and
    passes the blocklist; ``host`` is an ssh destination (a ~/.ssh/config
    alias works) and ``home`` the absolute path of that machine's Firstmate home.
    """
    name: str
    host: str
    home: str


@dataclass
class FleetConfig:
    """[fleet]: the optional Firstmate feed.

    ``fleetnotes fleet watch`` reads the fleet only through Firstmate's
    read-only view ``<firstmate_home>/bin/fm-fleet-snapshot.sh`` (also for
    each local secondmate home and each ``remotes`` entry, over ssh) every
    ``refresh_seconds``, giving up on the whole read after ``snapshot_timeout``.
    """
    firstmate_home: str = ""
    refresh_seconds: float = 60.0
    snapshot_timeout: float = 45.0
    remotes: list[Remote] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.firstmate_home)


@dataclass
class ServeConfig:
    """[serve]: where ``fleetnotes serve`` listens. Loopback by default."""
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT


@dataclass
class Config:
    output_dir: str
    state_dir: str
    rules_file: str
    blocked_phrases: list[str] = field(default_factory=list)
    serve: ServeConfig = field(default_factory=ServeConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    path: str = ""                                     # the file it came from; "" when none
    warnings: list[str] = field(default_factory=list)  # bad values, each kept at its default


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _home(env: Mapping[str, str]) -> str:
    return env.get("HOME") or os.path.expanduser("~")


def _expand(path: str, env: Mapping[str, str], base: str) -> str:
    """``~`` is the home folder; a relative path is relative to ``base``."""
    if path == "~" or path.startswith("~/"):
        path = _home(env) + path[1:]
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


def _xdg(env: Mapping[str, str], var: str, fallback: str) -> str:
    value = env.get(var, "")
    return value if os.path.isabs(value) else os.path.join(_home(env), fallback)


def default_config_path(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    explicit = env.get("FLEETNOTES_CONFIG", "")
    if explicit:
        return _expand(explicit, env, os.getcwd())
    return os.path.join(_xdg(env, "XDG_CONFIG_HOME", ".config"), APP, "config.toml")


def defaults(config_path: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """The configuration without a file (or before the file's values)."""
    env = os.environ if env is None else env
    config_path = config_path or default_config_path(env)
    return Config(
        output_dir=os.path.join(_xdg(env, "XDG_DATA_HOME", os.path.join(".local", "share")), APP, "public"),
        state_dir=os.path.join(_xdg(env, "XDG_STATE_HOME", os.path.join(".local", "state")), APP),
        rules_file=os.path.join(os.path.dirname(config_path), "rules.conf"),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Read ``path`` (default: ``default_config_path()``) over the defaults.

    A missing default file is fine (defaults); a missing or unreadable file
    named with ``path`` or FLEETNOTES_CONFIG raises ConfigError.
    """
    env = os.environ if env is None else env
    explicit = bool(path) or bool(env.get("FLEETNOTES_CONFIG"))
    path = _expand(path, env, os.getcwd()) if path else default_config_path(env)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {path}") from None
        return defaults(path, env)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e.__class__.__name__}") from None
    return from_dict(data, path, env)


def from_dict(data: dict, path: str, env: Mapping[str, str] | None = None) -> Config:
    """The configuration for the parsed TOML ``data`` of the file at ``path``."""
    env = os.environ if env is None else env
    cfg = defaults(path, env)
    cfg.path = path
    base = os.path.dirname(path)
    warn = cfg.warnings.append
    for key, value in data.items():
        if key in ("output_dir", "state_dir", "rules_file"):
            if isinstance(value, str) and value.strip() and value.isprintable():
                setattr(cfg, key, _expand(value.strip(), env, base))
            else:
                warn(f"{key} must be a non-empty path — using {getattr(cfg, key)}")
        elif key == "blocked_phrases":
            if isinstance(value, list) and all(isinstance(p, str) for p in value):
                cfg.blocked_phrases = [p.strip() for p in value if p.strip()]
            else:
                warn("blocked_phrases must be a list of strings — none used")
        elif key == "serve":
            _merge_serve(cfg.serve, value, warn)
        elif key == "fleet":
            _merge_fleet(cfg.fleet, value, warn, env, base)
        else:
            warn(f"unknown key '{key}' ignored")
    return cfg


def _merge_serve(target: ServeConfig, data: object, warn) -> None:
    if not isinstance(data, dict):
        warn("[serve] must be a table — using defaults")
        return
    for key, value in data.items():
        if key == "host":
            if isinstance(value, str) and value.strip():
                target.host = value.strip()
                if not _is_loopback(target.host):
                    warn(f"[serve] host {target.host} is not a loopback address: "
                         "anyone who can reach it can read your notes")
            else:
                warn(f"[serve] host must be a non-empty string — using {target.host}")
        elif key == "port":
            if type(value) is int and 1 <= value <= 65535:
                target.port = value
            else:
                warn(f"[serve] port must be an integer from 1 to 65535 — using {target.port}")
        else:
            warn(f"[serve] unknown key '{key}' ignored")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _merge_fleet(target: FleetConfig, data: object, warn, env: Mapping[str, str], base: str) -> None:
    """Merge [fleet] over defaults, keeping the default for any bad value."""
    if not isinstance(data, dict):
        warn("[fleet] must be a table — using defaults")
        return
    for key, value in data.items():
        if key == "firstmate_home":
            if isinstance(value, str) and value.isprintable():
                target.firstmate_home = _expand(value.strip(), env, base) if value.strip() else ""
            else:
                warn("[fleet] firstmate_home must be a path (empty turns the feed off) — feed off")
        elif key in FLEET_MAX_SECONDS:
            top = FLEET_MAX_SECONDS[key]
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 5 <= value <= top):
                setattr(target, key, float(value))
            else:
                warn(f"[fleet] {key} must be a number from 5 to {top} — using {getattr(target, key):g}")
        elif key == "remotes":
            if not isinstance(value, list):
                warn("[fleet] remotes must be an array of tables ([[fleet.remotes]]) — none read")
                continue
            remotes = [r for r in (_remote(entry, warn) for entry in value) if r is not None]
            if len(remotes) > MAX_REMOTES:
                warn(f"[fleet] at most {MAX_REMOTES} remotes — the rest are ignored")
            target.remotes = remotes[:MAX_REMOTES]
        else:
            warn(f"[fleet] unknown key '{key}' ignored")


def _remote(entry: object, warn) -> Remote | None:
    """A valid [[fleet.remotes]] table, or None (with a warning)."""
    if not isinstance(entry, dict) or set(entry) != {"name", "host", "home"}:
        warn("[[fleet.remotes]] needs exactly name, host and home — entry ignored")
        return None
    name, host, home = entry["name"], entry["host"], entry["home"]
    if not (isinstance(name, str) and name.strip()):
        warn("[[fleet.remotes]] name must be a non-empty string — entry ignored")
        return None
    if not (isinstance(host, str) and _SSH_HOST_RE.fullmatch(host)):
        warn("[[fleet.remotes]] host must be an ssh host or alias — entry ignored")
        return None
    if not (isinstance(home, str) and home.startswith("/") and home.isprintable()):
        warn("[[fleet.remotes]] home must be an absolute path — entry ignored")
        return None
    return Remote(name.strip(), host, home.rstrip("/") or "/")
