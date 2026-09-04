"""Secret-free authentication preflight for native developer lanes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shlex
import shutil
import subprocess
from typing import Any, Callable, Sequence


Runner = Callable[..., Any]


class AuthError(RuntimeError):
    """A native OAuth session is unavailable or uses the wrong method."""


@dataclass(frozen=True)
class AuthStatus:
    host: str
    state: str
    method: str
    refresh_command: str

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self.method == "oauth"

    def as_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "state": self.state,
            "method": self.method,
            "ready": self.ready,
            "refresh_command": self.refresh_command,
        }


def _run_status(command: Sequence[str], runner: Runner) -> Any:
    try:
        return runner(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return None


def refresh_command(
    host: str,
    executable: str | None = None,
    *,
    which: Callable[[str], "str | None"] = shutil.which,
) -> str:
    """The sign-in command a human can actually run for ``host``.

    The resolver always hands back an absolute path, so "came from PATH" is
    detected by comparing it with ``which(host)``: when they agree the bare,
    conventional hint is kept. When they differ (an explicit override, or the
    CLI inside a desktop-app bundle that is not on ``PATH``), a bare
    ``codex login`` would fail in exactly the environment that needed the
    resolution, so the resolved path is shell-quoted into the hint instead.
    """

    program = host
    if executable and executable != host:
        on_path = which(host)
        if not on_path or os.path.abspath(on_path) != os.path.abspath(executable):
            program = shlex.quote(executable)
    return f"{program} login" if host == "codex" else f"{program} auth login"


def auth_status(
    host: str, *, executable: str | None = None, runner: Runner = subprocess.run
) -> AuthStatus:
    """Return method/state metadata without reading a token, key, or auth file."""

    refresh = refresh_command(host, executable)
    if host == "codex":
        command = [executable or "codex", "login", "status"]
        completed = _run_status(command, runner)
        if completed is None:
            return AuthStatus("codex", "unavailable", "none", refresh)
        output = f"{getattr(completed, 'stdout', '')}\n{getattr(completed, 'stderr', '')}".lower()
        if completed.returncode != 0:
            return AuthStatus("codex", "signed-out", "none", refresh)
        if "api key" in output:
            return AuthStatus("codex", "wrong-method", "api-key", refresh)
        if "chatgpt" in output or "oauth" in output or "logged in" in output:
            return AuthStatus("codex", "ready", "oauth", refresh)
        return AuthStatus("codex", "unknown", "unknown", refresh)

    if host == "claude":
        command = [executable or "claude", "auth", "status", "--json"]
        completed = _run_status(command, runner)
        if completed is None:
            return AuthStatus("claude", "unavailable", "none", refresh)
        if completed.returncode != 0:
            return AuthStatus("claude", "signed-out", "none", refresh)
        try:
            payload = json.loads(getattr(completed, "stdout", "") or "{}")
        except json.JSONDecodeError:
            return AuthStatus("claude", "unknown", "unknown", refresh)
        if not isinstance(payload, dict) or payload.get("loggedIn") is not True:
            return AuthStatus("claude", "signed-out", "none", refresh)
        raw_method = str(payload.get("authMethod") or payload.get("subscriptionType") or "").lower()
        if "api" in raw_method or "key" in raw_method:
            return AuthStatus("claude", "wrong-method", "api-key", refresh)
        if any(marker in raw_method for marker in ("oauth", "subscription", "claude.ai", "max", "pro", "team", "enterprise")):
            return AuthStatus("claude", "ready", "oauth", refresh)
        return AuthStatus("claude", "unknown", "unknown", refresh)

    raise AuthError(f"unsupported native auth host: {host}")


def require_native_oauth(
    host: str, *, executable: str | None = None, runner: Runner = subprocess.run
) -> AuthStatus:
    status = auth_status(host, executable=executable, runner=runner)
    if not status.ready:
        raise AuthError(
            f"native {host} OAuth is {status.state}; run `{status.refresh_command}` "
            "or explicitly approve a configured billable route for this one run"
        )
    return status
