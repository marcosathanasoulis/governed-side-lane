#!/usr/bin/env python3
"""Dead-easy guided setup for Governed Side Lane + Prompt it.

Run with no arguments for an interactive wizard (every question has a safe
default; press Enter to keep it): detects the Claude Code and/or Codex CLIs
and their sign-in state, offers to link the installed skills for whichever
hosts are present (``scripts/install.sh``/``install.ps1``), offers to set the
Prompt it approval mode per present host, offers an optional OpenRouter
route, and lets a developer optionally declare per-host plan usage (no
detection is performed; this public package never inspects a live account).

Non-interactive / scripted use::

    python3 scripts/setup.py --non-interactive --hosts claude,codex \\
        --prompt-it-mode ask-first --openrouter skip \\
        --usage claude=extra-usage --usage codex=included-oauth

    python3 scripts/setup.py --check
    python3 scripts/setup.py openrouter-key   # the key step alone

Nothing here ever prints, logs, or writes a secret value anywhere but the OS
credential store: the interactive OpenRouter key is read with a hidden
prompt (``getpass``) and handed straight to
``side_lane.credentials.store_credential``, which pipes it to the platform
vault over stdin — never argv, an environment variable, or a file. A chat
assistant driving this setup is told to run ``openrouter-key`` itself, in the
user's own terminal, rather than ever asking for the key in chat.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
from typing import Callable, Mapping, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from side_lane import credentials, hosts as host_discovery, preferences  # noqa: E402
from side_lane.auth import AuthStatus, auth_status  # noqa: E402

OPENROUTER_SERVICE = "governed-side-lane-openrouter"
OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"
OPENROUTER_KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"
PROMPT_IT_MODES = ("ask-first", "just-go")
HOSTS = ("claude", "codex")

Which = Callable[[str], "str | None"]
Runner = Callable[..., subprocess.CompletedProcess]
InputFn = Callable[[str], str]
GetpassFn = Callable[[str], str]
Opener = Callable[[str, "bytes | None", "Mapping[str, str] | None"], object]


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def detect_host(host: str, *, which: Which = shutil.which) -> dict[str, object]:
    """One host's presence and sign-in state, never reading a secret."""

    executable = host_discovery.resolve_host_executable(host, which=which)
    if not executable:
        return {"present": False, "executable": None, "auth": None}
    status = auth_status(host, executable=executable)
    return {"present": True, "executable": executable, "auth": status}


def detect_hosts(*, which: Which = shutil.which) -> dict[str, dict[str, object]]:
    return {host: detect_host(host, which=which) for host in HOSTS}


def format_detection(detected: Mapping[str, Mapping[str, object]]) -> str:
    lines = []
    for host in HOSTS:
        info = detected[host]
        if not info["present"]:
            lines.append(f"  {host:7s} not found on this machine")
            continue
        status: AuthStatus = info["auth"]  # type: ignore[assignment]
        state = "signed in" if status.ready else f"not ready ({status.state})"
        lines.append(f"  {host:7s} found at {info['executable']} -- {state}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# install.sh / install.ps1 delegation
# --------------------------------------------------------------------------


def run_install_step(
    mode: str, host: str, *, runner: Runner = subprocess.run
) -> subprocess.CompletedProcess:
    """Delegate one install.sh (or install.ps1 on Windows) step for one host.

    This never reimplements the installer's own symlink/manifest logic; it
    only decides *which* hosts to call it for, so install.sh/install.ps1 stay
    the single source of truth for what "installed" means.
    """

    if os.name == "nt":
        command = [
            "powershell", "-NoProfile", "-NonInteractive", "-File",
            str(PACKAGE_ROOT / "scripts" / "install.ps1"), "-Mode", mode, "-Host", host,
        ]
    else:
        command = ["bash", str(PACKAGE_ROOT / "scripts" / "install.sh"), mode, host]
    return runner(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)


# --------------------------------------------------------------------------
# Prompt it mode
# --------------------------------------------------------------------------


def locate_mode_script() -> "Path | None":
    """Find the public Prompt it mode.py, without assuming any one install layout.

    Checked in order: an explicit override, then the sibling ``public/prompt-it``
    checkout (the layout this very repository uses), then common installed
    skill locations for each host. Returns ``None`` (never raises) when none
    exists, so the wizard can silently skip this step on an install that
    ships Side Lane without Prompt it.
    """

    override = os.environ.get("SIDE_LANE_PROMPT_IT_MODE_SCRIPT")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    sibling = (
        PACKAGE_ROOT.parents[2] / "prompt-it" / "plugins" / "prompt-it"
        / "skills" / "prompt-it" / "scripts" / "mode.py"
    )
    if sibling.is_file():
        return sibling
    for installed in (
        Path.home() / ".claude" / "skills" / "prompt-it" / "scripts" / "mode.py",
        Path.home() / ".codex" / "skills" / "prompt-it" / "scripts" / "mode.py",
    ):
        if installed.is_file():
            return installed
    return None


def set_prompt_it_mode(
    mode_script: Path, host: str, mode: str, *, runner: Runner = subprocess.run
) -> subprocess.CompletedProcess:
    return runner(
        [sys.executable, str(mode_script), "setup", "--host", host, "--mode", mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
    )


# --------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------


def check_openrouter_key_status(
    secret: str, *, opener: "Opener | None" = None
) -> "dict[str, object] | None":
    """Optional, free ``GET /api/v1/key`` call; returns the parsed JSON or None.

    ``opener`` is injectable so tests never perform a real network call; the
    key is held only in this process's memory for the single request and is
    never written to a file, a log, or this function's return value's own
    error path.
    """

    def _default_opener(url: str, data: "bytes | None", headers: "Mapping[str, str] | None") -> object:
        request = urllib.request.Request(url, data=data, headers=dict(headers or {}))
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    call = opener or _default_opener
    try:
        result = call(
            OPENROUTER_KEY_STATUS_URL, None, {"Authorization": f"Bearer {secret}"}
        )
    except Exception:  # noqa: BLE001 - best-effort, never fatal to setup
        return None
    return result if isinstance(result, dict) else None


def store_openrouter_key(
    secret: str, *, system: "str | None" = None
) -> None:
    if not secret or not secret.strip():
        raise SetupError("OpenRouter key must not be empty")
    credentials.store_credential(OPENROUTER_SERVICE, secret.strip(), system)


def remove_openrouter_key(*, system: "str | None" = None) -> None:
    credentials.delete_credential(OPENROUTER_SERVICE, system)


# --------------------------------------------------------------------------
# Usage declaration
# --------------------------------------------------------------------------


def parse_usage_flags(values: Sequence[str]) -> dict[str, str]:
    """Parse repeatable ``--usage host=state`` flags into a validated map."""

    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SetupError(f"--usage must be host=state, got: {value!r}")
        host, _, state = value.partition("=")
        host, state = host.strip(), state.strip()
        if host not in preferences.SUPPORTED_HOSTS:
            raise SetupError(
                f"--usage host must be one of {sorted(preferences.SUPPORTED_HOSTS)}: {host!r}"
            )
        if state not in preferences.SUPPORTED_STATES:
            raise SetupError(
                f"--usage state must be one of {sorted(preferences.SUPPORTED_STATES)}: {state!r}"
            )
        parsed[host] = state
    return parsed


# --------------------------------------------------------------------------
# Finish summary
# --------------------------------------------------------------------------


def usable_routes_summary(
    detected: Mapping[str, Mapping[str, object]], *, openrouter_present: bool
) -> str:
    lines = []
    for host in HOSTS:
        info = detected[host]
        if info["present"] and info["auth"].ready:  # type: ignore[union-attr]
            lines.append(f"  {host}: native route ready")
        elif info["present"]:
            lines.append(f"  {host}: installed but not signed in ({info['auth'].state})")  # type: ignore[union-attr]
        else:
            lines.append(f"  {host}: not installed")
    lines.append(
        "  openrouter: " + ("key present (unqualified until a smoke test)" if openrouter_present else "not configured")
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Interactive wizard
# --------------------------------------------------------------------------


def _ask(input_fn: InputFn, prompt: str, default: str) -> str:
    try:
        answer = input_fn(f"{prompt} ").strip()
    except EOFError:
        return default
    return answer or default


def run_wizard(
    *,
    input_fn: InputFn = input,
    getpass_fn: GetpassFn = getpass.getpass,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
    opener: "Opener | None" = None,
    system: "str | None" = None,
    print_fn: Callable[[str], None] = print,
) -> int:
    print_fn("Governed Side Lane + Prompt it setup\n")
    detected = detect_hosts(which=which)
    print_fn("Detected hosts:")
    print_fn(format_detection(detected))
    present_hosts = [host for host, info in detected.items() if info["present"]]
    if not present_hosts:
        print_fn(
            "\nNeither Claude Code nor Codex was found on PATH. Install one of "
            "them, then rerun this setup."
        )
        return 1

    print_fn("\nLinking installed skills for the hosts found above...")
    for host in present_hosts:
        result = run_install_step("install", host, runner=runner)
        print_fn(result.stdout or "")

    print_fn("\nPrompt it approval mode:")
    print_fn("  [1] Ask first -- Prompt it proposes a plan and waits for your go-ahead (default)")
    print_fn("  [2] Just go   -- Prompt it plans and executes without pausing for approval")
    mode_script = locate_mode_script()
    if mode_script is None:
        print_fn("  (Prompt it is not installed here; skipping)")
    else:
        choice = _ask(input_fn, "Mode [1/2]:", "1")
        mode = "just-go" if choice == "2" else "ask-first"
        for host in present_hosts:
            set_prompt_it_mode(mode_script, host, mode, runner=runner)
        print_fn(f"  saved: {mode}")

    print_fn("\nOpenRouter (optional; lets Claude Code route to OpenRouter models):")
    choice = _ask(
        input_fn,
        "Do you have an OpenRouter key? [s]kip (default) / [y]es / [h]elp me get one:",
        "s",
    )
    if choice == "h":
        print_fn(f"  Get a key at: {OPENROUTER_KEYS_URL}")
        _ask(input_fn, "Press Enter once you have a key (or just Enter to skip):", "")
        choice = _ask(input_fn, "Do you have an OpenRouter key now? [s]kip / [y]es:", "s")
    if choice == "y":
        secret = getpass_fn("OpenRouter API key (input hidden): ")
        try:
            store_openrouter_key(secret, system=system)
        except (credentials.CredentialError, SetupError) as exc:
            print_fn(f"  could not store the key: {exc}")
        else:
            present = credentials.credential_present(OPENROUTER_SERVICE, system)
            print_fn(f"  stored: {'yes' if present else 'no'} (value never displayed)")
            if _ask(input_fn, "Check key status now? (free, optional) [y/N]:", "n") == "y":
                status = check_openrouter_key_status(secret, opener=opener)
                print_fn(f"  key status: {status if status is not None else 'could not reach OpenRouter'}")

    openrouter_present = credentials.credential_present(OPENROUTER_SERVICE, system)

    print_fn("\nUsage (optional; no detection is performed -- you manage your own usage):")
    for host in present_hosts:
        choice = _ask(
            input_fn,
            f"  {host}: [1] included/subscription  [2] paying extra  [3] unknown (default):",
            "3",
        )
        state = {"1": "included-oauth", "2": "extra-usage"}.get(choice, "unknown")
        preferences.save_usage(host, state)
        print_fn(f"    saved: {state}")

    print_fn("\nFinishing up...")
    for host in present_hosts:
        result = run_install_step("check", host, runner=runner)
        print_fn(result.stdout or "")
    print_fn("\nUsable routes:")
    print_fn(usable_routes_summary(detected, openrouter_present=openrouter_present))
    return 0


# --------------------------------------------------------------------------
# Non-interactive entrypoint
# --------------------------------------------------------------------------


def run_non_interactive(
    args: argparse.Namespace,
    *,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
    system: "str | None" = None,
    print_fn: Callable[[str], None] = print,
) -> int:
    detected = detect_hosts(which=which)
    requested_hosts = (
        [item.strip() for item in args.hosts.split(",") if item.strip()]
        if args.hosts else [host for host, info in detected.items() if info["present"]]
    )
    for host in requested_hosts:
        if host not in HOSTS:
            raise SetupError(f"unknown host: {host!r}; expected one of {HOSTS}")
    present_hosts = [host for host in requested_hosts if detected[host]["present"]]
    print_fn(format_detection(detected))

    for host in present_hosts:
        run_install_step("install", host, runner=runner)

    mode_script = locate_mode_script()
    if mode_script is not None and args.prompt_it_mode:
        for host in present_hosts:
            set_prompt_it_mode(mode_script, host, args.prompt_it_mode, runner=runner)

    if args.remove_openrouter_key or args.openrouter == "remove":
        remove_openrouter_key(system=system)
    elif args.openrouter == "keep":
        pass  # explicit no-op: leave whatever key is already stored

    for host, state in parse_usage_flags(args.usage).items():
        preferences.save_usage(host, state)

    for host in present_hosts:
        run_install_step("check", host, runner=runner)
    openrouter_present = credentials.credential_present(OPENROUTER_SERVICE, system)
    print_fn(usable_routes_summary(detected, openrouter_present=openrouter_present))
    return 0


def run_check(
    *, which: Which = shutil.which, system: "str | None" = None,
    print_fn: Callable[[str], None] = print,
) -> int:
    detected = detect_hosts(which=which)
    print_fn(format_detection(detected))
    openrouter_present = credentials.credential_present(OPENROUTER_SERVICE, system)
    print_fn(usable_routes_summary(detected, openrouter_present=openrouter_present))
    return 0 if any(info["present"] for info in detected.values()) else 1


def run_openrouter_key_step(
    *, getpass_fn: GetpassFn = getpass.getpass, system: "str | None" = None,
    print_fn: Callable[[str], None] = print,
) -> int:
    secret = getpass_fn("OpenRouter API key (input hidden): ")
    try:
        store_openrouter_key(secret, system=system)
    except (credentials.CredentialError, SetupError) as exc:
        print_fn(f"could not store the key: {exc}")
        return 1
    present = credentials.credential_present(OPENROUTER_SERVICE, system)
    print_fn(f"stored: {'yes' if present else 'no'} (value never displayed)")
    return 0 if present else 1


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("openrouter-key",), default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--hosts", default=None, help="comma-separated: claude,codex")
    parser.add_argument("--prompt-it-mode", choices=PROMPT_IT_MODES, default=None)
    parser.add_argument("--openrouter", choices=("skip", "keep", "remove"), default="skip")
    parser.add_argument(
        "--usage", action="append", default=[], metavar="host=state",
        help="repeatable, e.g. --usage claude=extra-usage",
    )
    parser.add_argument("--remove-openrouter-key", action="store_true")
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "openrouter-key":
            return run_openrouter_key_step()
        if args.check:
            return run_check()
        if args.non_interactive:
            return run_non_interactive(args)
        if not sys.stdin.isatty():
            print(
                "setup needs a terminal for the interactive wizard; pass "
                "--non-interactive with explicit flags for a scripted run, "
                "see --help",
                file=sys.stderr,
            )
            return 2
        return run_wizard()
    except (SetupError, credentials.CredentialError, preferences.PreferencesError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
