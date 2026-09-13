"""Codex adapter with canonical governance injection.

Two gateways are accepted, both selected purely from the provider config:

- ``native-codex``: the developer's signed-in Codex OAuth session, never
  billable, review or execute. No API key may reach the child.
- ``codex-api-key``: an explicit, billable, execute-only provider-key route
  for hosts with no OAuth session (e.g. a cloud worker). The launcher-read
  secret is handed to the Codex CLI as ``OPENAI_API_KEY`` (plus
  ``OPENAI_BASE_URL`` when the provider config carries ``base_url``); every
  other inherited provider credential is still scrubbed, and the secret is
  redacted from captured output.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from side_lane.credentials import scrub_backend_environment
from side_lane.governance import lane_system_prompt
from side_lane.hosts import with_support_dir
from side_lane.results import LaneResult


NATIVE_PROVIDER = "openai"
NATIVE_GATEWAY = "native-codex"
API_KEY_GATEWAY = "codex-api-key"
SUPPORTED_GATEWAYS = frozenset({NATIVE_GATEWAY, API_KEY_GATEWAY})
NATIVE_PROTOCOLS = frozenset({"native-codex", "native-codex-readonly"})
REDACTED_SECRET = "[REDACTED_PROVIDER_KEY]"

PROVIDER_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "GLM_API_KEY",
        "AZURE_OPENAI_API_KEY",
    }
)
PROVIDER_CREDENTIAL_ENV_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "OPENROUTER_",
    "ZAI_",
    "ZHIPUAI_",
    "GLM_",
    "AZURE_OPENAI_",
)


class CodexAdapterError(ValueError):
    """Raised before a native Codex process can start."""


Runner = Callable[..., Any]


def _required_string(config: Mapping[str, Any], key: str, label: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise CodexAdapterError(f"{label} must be a non-empty string")
    return value


def _route_metadata(
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
) -> tuple[str, str, str, bool]:
    """Return (runtime_model, gateway, auth_method, billable) or fail closed."""

    gateway = provider_config.get("gateway")
    auth_method = provider_config.get("auth_method")
    billable = provider_config.get("billable")
    if gateway == NATIVE_GATEWAY:
        if provider != NATIVE_PROVIDER:
            raise CodexAdapterError("native Codex lanes require provider 'openai'")
        if auth_method != "oauth" or billable is not False:
            raise CodexAdapterError("native Codex lanes require non-billable OAuth")
    elif gateway == API_KEY_GATEWAY:
        if auth_method != "provider-key" or billable is not True:
            raise CodexAdapterError(
                "Codex API-key lanes require auth_method 'provider-key' and billable true"
            )
        if mode != "execute":
            raise CodexAdapterError(
                "Codex API-key lanes are execute-only; review mode forbids secret access"
            )
        _required_string(provider_config, "credential_service", "credential_service")
        base_url = provider_config.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url):
            raise CodexAdapterError("base_url must be a non-empty string when set")
    else:
        raise CodexAdapterError(
            f"Codex lanes require gateway {NATIVE_GATEWAY!r} or {API_KEY_GATEWAY!r}"
        )
    runtime_model = _required_string(model_config, "runtime_model", "runtime_model")
    if runtime_model != model:
        raise CodexAdapterError("runtime_model must exactly match the selected model")
    protocol = _required_string(model_config, "protocol", "protocol")
    expected = "native-codex-readonly" if mode == "review" else "native-codex"
    if protocol != expected or protocol not in NATIVE_PROTOCOLS:
        raise CodexAdapterError(f"native Codex {mode} protocol is not verified")
    return runtime_model, str(gateway), str(auth_method), bool(billable)


def _validate_selection(
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
) -> str:
    return _route_metadata(provider, model, provider_config, model_config, mode)[0]


def _validate_worktree(path_value: Path | str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise CodexAdapterError(f"path is not a Git worktree: {path}")
    return path


def build_child_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """Preserve the OAuth session while removing every API-key fallback."""

    return scrub_backend_environment({
        name: value
        for name, value in inherited.items()
        if name not in PROVIDER_CREDENTIAL_ENV_NAMES
        and not name.startswith(PROVIDER_CREDENTIAL_ENV_PREFIXES)
    })


def build_transport_environment(
    inherited: Mapping[str, str],
    *,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
    secret: str | None = None,
) -> dict[str, str]:
    """Scrubbed child environment, plus the provider key on API-key routes only."""

    _runtime_model, gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    child = build_child_env(inherited)
    if auth_method == "oauth":
        if secret is not None:
            raise CodexAdapterError("native OAuth routes must not receive an API key")
        return child
    if gateway != API_KEY_GATEWAY or not billable:
        raise CodexAdapterError("provider-key Codex routes must use the codex-api-key gateway")
    if not isinstance(secret, str) or not secret:
        raise CodexAdapterError("explicit billable route credential is absent")
    child["OPENAI_API_KEY"] = secret
    base_url = provider_config.get("base_url")
    if isinstance(base_url, str) and base_url:
        child["OPENAI_BASE_URL"] = base_url.rstrip("/")
    return child


def _redact(value: object, secret: str | None) -> str:
    text = str(value or "")
    return text.replace(secret, REDACTED_SECRET) if secret else text


def build_codex_command(
    executable: str,
    repo: Path | str,
    worktree: Path | str,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    prompt: str,
    *,
    mode: str = "execute",
) -> tuple[str, ...]:
    if not isinstance(executable, str) or not executable:
        raise CodexAdapterError("Codex executable is required")
    if mode not in {"review", "execute"}:
        raise CodexAdapterError("mode must be review or execute")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CodexAdapterError("task prompt must be non-empty")
    repo_path = _validate_worktree(repo)
    worktree_path = _validate_worktree(worktree)
    if repo_path == worktree_path:
        raise CodexAdapterError(f"{mode} lane requires a dedicated worktree")
    runtime_model = _validate_selection(
        provider, model, provider_config, model_config, mode
    )
    task = lane_system_prompt(mode, repo_path) + "\n\n# Approved task\n\n" + prompt
    command = [
        executable,
        "exec",
        "--ephemeral",
        "-C",
        str(worktree_path),
        "-s",
        "read-only" if mode == "review" else "danger-full-access",
        "-m",
        runtime_model,
    ]
    if mode == "review":
        command.extend(("-c", "mcp_servers={}"))
    command.append(task)
    return tuple(command)


def run_codex(
    *,
    executable: str,
    repo: Path | str,
    worktree: Path | str,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    prompt: str,
    mode: str = "execute",
    capabilities: "tuple[str, ...] | list[str]" = (),
    env: Mapping[str, str] | None = None,
    support_dir: str | None = None,
    secret: str | None = None,
    runner: Runner = subprocess.run,
) -> LaneResult:
    repo_path = _validate_worktree(repo)
    worktree_path = _validate_worktree(worktree)
    _runtime_model, gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    argv = build_codex_command(
        executable,
        repo_path,
        worktree_path,
        provider,
        model,
        provider_config,
        model_config,
        prompt,
        mode=mode,
    )
    child_env = with_support_dir(
        build_transport_environment(
            os.environ if env is None else env,
            provider=provider,
            model=model,
            provider_config=provider_config,
            model_config=model_config,
            mode=mode,
            secret=secret,
        ),
        support_dir,
    )
    try:
        completed = runner(
            argv,
            cwd=worktree_path,
            env=child_env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CodexAdapterError(f"could not start Codex executable: {exc}") from exc
    return LaneResult(
        argv=argv,
        returncode=int(completed.returncode),
        cwd=worktree_path,
        host="codex",
        provider=provider,
        gateway=gateway,
        model=model,
        auth_method=auth_method,
        billable=billable,
        stdout=_redact(getattr(completed, "stdout", ""), secret),
        stderr=_redact(getattr(completed, "stderr", ""), secret),
        capabilities=tuple(sorted(set(capabilities))),
        requested_model=model,
        resolved_model=None,
    )
