"""Secret-free authentication preflight for native developer lanes."""

from __future__ import annotations

from dataclasses import dataclass
import json
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


def auth_status(
    host: str, *, executable: str | None = None, runner: Runner = subprocess.run
) -> AuthStatus:
    """Return method/state metadata without reading a token, key, or auth file."""

    if host == "codex":
        command = [executable or "codex", "login", "status"]
        completed = _run_status(command, runner)
        if completed is None:
            return AuthStatus("codex", "unavailable", "none", "codex login")
        output = f"{getattr(completed, 'stdout', '')}\n{getattr(completed, 'stderr', '')}".lower()
        if completed.returncode != 0:
            return AuthStatus("codex", "signed-out", "none", "codex login")
        if "api key" in output:
            return AuthStatus("codex", "wrong-method", "api-key", "codex login")
        if "chatgpt" in output or "oauth" in output or "logged in" in output:
            return AuthStatus("codex", "ready", "oauth", "codex login")
        return AuthStatus("codex", "unknown", "unknown", "codex login")

    if host == "claude":
        command = [executable or "claude", "auth", "status", "--json"]
        completed = _run_status(command, runner)
        if completed is None:
            return AuthStatus("claude", "unavailable", "none", "claude auth login")
        if completed.returncode != 0:
            return AuthStatus("claude", "signed-out", "none", "claude auth login")
        try:
            payload = json.loads(getattr(completed, "stdout", "") or "{}")
        except json.JSONDecodeError:
            return AuthStatus("claude", "unknown", "unknown", "claude auth login")
        if not isinstance(payload, dict) or payload.get("loggedIn") is not True:
            return AuthStatus("claude", "signed-out", "none", "claude auth login")
        raw_method = str(payload.get("authMethod") or payload.get("subscriptionType") or "").lower()
        if "api" in raw_method or "key" in raw_method:
            return AuthStatus("claude", "wrong-method", "api-key", "claude auth login")
        if any(marker in raw_method for marker in ("oauth", "subscription", "claude.ai", "max", "pro", "team", "enterprise")):
            return AuthStatus("claude", "ready", "oauth", "claude auth login")
        return AuthStatus("claude", "unknown", "unknown", "claude auth login")

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
