"""Per-user declared host usage preference, for public (no-detection) installs.

The public package never detects plan usage; a developer may optionally
*declare*, per host, whether they are on included/subscription usage, paying
extra, or unknown (the conservative default). The declaration is stored in a
small per-user JSON file outside any repository and outside Git, so it
survives across tasks and repositories for the same OS account.

Nothing here is a credential: the file never carries a secret value, and it
is world-unreadable (0600) only as an ordinary privacy courtesy for a local
preference file, not as a security boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import tempfile
from typing import Mapping

SUPPORTED_HOSTS = frozenset({"claude", "codex"})
SUPPORTED_STATES = frozenset({"included-oauth", "extra-usage", "unknown"})
PREFERENCES_FILENAME = "preferences.json"
PREFERENCES_DIR_NAME = "governed-side-lane"
SCHEMA_VERSION = 1


class PreferencesError(Exception):
    pass


def preferences_dir(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None,
    system: str | None = None,
) -> Path:
    """The per-user config directory for this package, on any supported OS.

    POSIX hosts (including macOS) follow the XDG base-directory convention:
    ``$XDG_CONFIG_HOME`` when set, otherwise ``~/.config``. Windows uses
    ``%APPDATA%`` (Roaming), falling back to the conventional location when
    the variable is unset.
    """

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    selected = system or platform.system()
    if selected == "Windows":
        appdata = environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    else:
        xdg = environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg) if xdg else home / ".config"
    return base / PREFERENCES_DIR_NAME


def preferences_path(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None,
    system: str | None = None,
) -> Path:
    return preferences_dir(environ=environ, home=home, system=system) / PREFERENCES_FILENAME


def load_preferences(path: Path | None = None) -> dict[str, str]:
    """Return the saved ``{host: state}`` usage map, or ``{}`` if absent/unreadable.

    A missing file is the ordinary first-run case and returns an empty map,
    never an error. A present-but-malformed file (not JSON, not an object, a
    bad schema version) is also read as empty rather than raising: a
    preference is advisory and optional, so a corrupt file must not block
    `list`/`recommend`; only explicit `prefs set-usage` writes are expected to
    repair it.
    """

    target = path or preferences_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {}
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        str(host): str(state)
        for host, state in usage.items()
        if host in SUPPORTED_HOSTS and state in SUPPORTED_STATES
    }


def save_usage(host: str, state: str, path: Path | None = None) -> dict[str, str]:
    """Persist one host's declared usage state; returns the full saved map."""

    if host not in SUPPORTED_HOSTS:
        raise PreferencesError(
            f"unsupported host: {host!r}; expected one of {', '.join(sorted(SUPPORTED_HOSTS))}"
        )
    if state not in SUPPORTED_STATES:
        raise PreferencesError(
            f"unsupported usage state: {state!r}; expected one of "
            + ", ".join(sorted(SUPPORTED_STATES))
        )
    target = path or preferences_path()
    usage = load_preferences(target)
    usage[host] = state
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": SCHEMA_VERSION, "usage": usage}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return usage


def merge_host_cost_state(
    explicit: Mapping[str, object], *, path: Path | None = None
) -> dict[str, str]:
    """Saved per-host usage, overridden by any host the caller set explicitly.

    ``explicit`` is a recommendation profile's own ``host_cost_state`` map
    (which may be empty or omit some hosts); an explicit value for a host
    always wins over a saved declaration for that same host.
    """

    merged = dict(load_preferences(path))
    for host, state in explicit.items():
        if isinstance(host, str) and isinstance(state, str):
            merged[host] = state
    return merged
