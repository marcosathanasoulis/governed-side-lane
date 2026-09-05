"""Native OAuth-backed Codex adapter with canonical governance injection."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

from side_lane.governance import lane_system_prompt
from side_lane.hosts import with_support_dir
from side_lane.results import LaneResult


NATIVE_PROVIDER = "openai"
NATIVE_GATEWAY = "native-codex"
NATIVE_PROTOCOLS = frozenset({"native-codex", "native-codex-readonly"})

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


def _validate_selection(
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
) -> str:
    if provider != NATIVE_PROVIDER:
        raise CodexAdapterError("native Codex lanes require provider 'openai'")
    if provider_config.get("gateway") != NATIVE_GATEWAY:
        raise CodexAdapterError("native Codex lanes require gateway 'native-codex'")
    if provider_config.get("auth_method") != "oauth" or provider_config.get("billable") is not False:
        raise CodexAdapterError("native Codex lanes require non-billable OAuth")
    runtime_model = _required_string(model_config, "runtime_model", "runtime_model")
    if runtime_model != model:
        raise CodexAdapterError("runtime_model must exactly match the selected model")
    protocol = _required_string(model_config, "protocol", "protocol")
    expected = "native-codex-readonly" if mode == "review" else "native-codex"
    if protocol != expected or protocol not in NATIVE_PROTOCOLS:
        raise CodexAdapterError(f"native Codex {mode} protocol is not verified")
    return runtime_model


def _validate_worktree(path_value: Path | str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise CodexAdapterError(f"path is not a Git worktree: {path}")
    return path


def build_child_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """Preserve the OAuth session while removing every API-key fallback."""

    return {
        name: value
        for name, value in inherited.items()
        if name not in PROVIDER_CREDENTIAL_ENV_NAMES
        and not name.startswith(PROVIDER_CREDENTIAL_ENV_PREFIXES)
    }


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
    runner: Runner = subprocess.run,
) -> LaneResult:
    repo_path = _validate_worktree(repo)
    worktree_path = _validate_worktree(worktree)
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
    child_env = with_support_dir(build_child_env(os.environ if env is None else env), support_dir)
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
        gateway=NATIVE_GATEWAY,
        model=model,
        auth_method="oauth",
        billable=False,
        stdout=getattr(completed, "stdout", "") or "",
        stderr=getattr(completed, "stderr", "") or "",
        capabilities=tuple(sorted(set(capabilities))),
    )
