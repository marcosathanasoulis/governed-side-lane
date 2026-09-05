"""Claude Code adapter for native OAuth and explicit billable GLM routes."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from side_lane.governance import known_capabilities, lane_system_prompt, tool_policy
from side_lane.results import LaneResult


MAX_PROMPT_CHARS = 100_000
NATIVE_PROVIDER = "claude"
NATIVE_GATEWAY = "native-claude"
BILLABLE_PROVIDERS = frozenset({"glm", "openrouter"})

SCRUB_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "GLM_API_KEY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)
SCRUB_PREFIXES = ("OPENROUTER_", "ZAI_", "ZHIPUAI_", "GLM_")
GLM_QUOTA_PAUSE = re.compile(
    r"\b(?:quota|usage limit|rate limit)\b.*\b(?:exceed(?:ed)?|reached|reset|temporar(?:y|ily)|hours?)\b",
    re.IGNORECASE | re.DOTALL,
)


# Capability-gated permission rules for headless execute lanes.
#
# ``claude -p`` cannot ask a human for approval, so every Bash command that no
# allow rule covers is denied. Execute lanes therefore receive an explicit
# ``--allowedTools`` list. The rules themselves live in the canonical
# ``config/lane-governance.md`` ("Execute tool allowlist"); this adapter only
# renders that section for the granted capabilities. Review lanes never receive
# one: their argv stays byte-identical to the strict read-only form.
class ClaudeAdapterError(RuntimeError):
    """A Claude route cannot be safely launched."""


Capabilities = "tuple[str, ...] | list[str] | frozenset[str]"


def _capability_set(capabilities: Capabilities) -> frozenset[str]:
    granted = frozenset(capabilities)
    unknown = sorted(granted - known_capabilities())
    if unknown:
        raise ClaudeAdapterError(f"unknown capability: {', '.join(unknown)}")
    return granted


def allowed_tools(mode: str, capabilities: Capabilities = ()) -> tuple[str, ...]:
    """Deterministic ``--allowedTools`` rules rendered from canonical governance.

    Capability names are validated first and unknown names raise in every
    mode; review mode then returns an empty tuple. The rule text comes from the
    ``Execute tool allowlist`` section of ``config/lane-governance.md``.
    """

    granted = _capability_set(capabilities)
    if mode != "execute":
        return ()
    policy = tool_policy()
    tools: list[str] = list(policy.always)
    for name, rules in policy.allowed.items():
        if name in granted:
            for rule in rules:
                if rule not in tools:
                    tools.append(rule)
    return tuple(tools)


def disallowed_tools(mode: str, capabilities: Capabilities = ()) -> tuple[str, ...]:
    """Deny rules that must accompany an allow rule (deny wins in Claude Code)."""

    granted = _capability_set(capabilities)
    if mode != "execute":
        return ()
    policy = tool_policy()
    tools: list[str] = []
    for name, rules in policy.denied.items():
        if name in granted:
            tools.extend(rule for rule in rules if rule not in tools)
    return tuple(tools)


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaudeAdapterError(f"{label} must be a non-empty string")
    return value.strip()


def validate_worktree(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise ClaudeAdapterError(f"path is not a Git worktree: {path}")
    return path


def scrub_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in inherited.items()
        if name not in SCRUB_EXACT and not name.startswith(SCRUB_PREFIXES)
    }


def _route_metadata(
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
) -> tuple[str, str, str, bool]:
    runtime_model = _nonempty(model_config.get("runtime_model"), "runtime_model")
    if runtime_model != model:
        raise ClaudeAdapterError("model configuration would silently substitute a model")
    protocol = _nonempty(model_config.get("protocol"), "protocol")
    gateway = _nonempty(provider_config.get("gateway"), "gateway")
    auth_method = _nonempty(provider_config.get("auth_method"), "auth_method")
    billable = provider_config.get("billable") is True
    expected_native = "native-claude-readonly" if mode == "review" else "native-claude"
    if provider == NATIVE_PROVIDER:
        if gateway != NATIVE_GATEWAY or auth_method != "oauth" or billable:
            raise ClaudeAdapterError("native Claude lanes require non-billable OAuth")
        if protocol != expected_native:
            raise ClaudeAdapterError(f"native Claude {mode} protocol is not verified")
    else:
        if provider not in BILLABLE_PROVIDERS or auth_method != "provider-key" or not billable:
            raise ClaudeAdapterError("external Claude routes must be explicit billable key routes")
        expected = "anthropic-compatible-readonly" if mode == "review" else "anthropic-compatible"
        if protocol != expected:
            raise ClaudeAdapterError(f"billable Claude {mode} protocol is not verified")
        endpoint = _nonempty(provider_config.get("base_url"), "base_url")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ClaudeAdapterError("billable gateway must be a clean HTTPS endpoint")
    return runtime_model, gateway, auth_method, billable


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
    runtime_model, _gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    child = scrub_environment(inherited)
    if auth_method == "oauth":
        if secret is not None:
            raise ClaudeAdapterError("native OAuth routes must not receive an API key")
        return child
    if not billable or not isinstance(secret, str) or not secret:
        raise ClaudeAdapterError("explicit billable route credential is absent")
    child["ANTHROPIC_AUTH_TOKEN"] = secret
    child["ANTHROPIC_BASE_URL"] = _nonempty(provider_config.get("base_url"), "base_url").rstrip("/")
    child["ANTHROPIC_MODEL"] = runtime_model
    child["ANTHROPIC_SMALL_FAST_MODEL"] = runtime_model
    return child


def build_command(
    *,
    executable: str,
    repo: str | Path,
    worktree: str | Path,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    prompt: str,
    mode: str = "execute",
    capabilities: Capabilities = (),
) -> list[str]:
    executable = _nonempty(executable, "Claude executable")
    repo_path = validate_worktree(repo)
    worktree_path = validate_worktree(worktree)
    if repo_path == worktree_path:
        raise ClaudeAdapterError(f"{mode} lane requires a dedicated worktree")
    runtime_model, _gateway, _auth_method, _billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    task = _nonempty(prompt, "prompt")
    if len(task) > MAX_PROMPT_CHARS:
        raise ClaudeAdapterError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    command = [executable, "-p", task]
    if mode == "review":
        command.extend(
            (
                "--safe-mode",
                "--add-dir",
                str(repo_path),
                "--model",
                runtime_model,
                "--permission-mode",
                "plan",
                "--tools",
                "Read,Glob,Grep",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-session-persistence",
                "--output-format",
                "text",
            )
        )
    else:
        command.extend(
            (
                "--model",
                runtime_model,
                "--permission-mode",
                "acceptEdits",
                "--setting-sources",
                "user,project,local",
                "--output-format",
                "text",
            )
        )
        for tool in allowed_tools(mode, capabilities):
            command.extend(("--allowedTools", tool))
        for tool in disallowed_tools(mode, capabilities):
            command.extend(("--disallowedTools", tool))
    command.extend(("--append-system-prompt", lane_system_prompt(mode, repo_path)))
    return command


def _redact(value: object, secret: str | None) -> str:
    text = str(value or "")
    return text.replace(secret, "[REDACTED_PROVIDER_KEY]") if secret else text


def launch(
    *,
    executable: str,
    repo: str | Path,
    worktree: str | Path,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    prompt: str,
    mode: str = "execute",
    capabilities: Capabilities = (),
    env: Mapping[str, str] | None = None,
    secret: str | None = None,
    runner: Runner = subprocess.run,
) -> LaneResult:
    repo_path = validate_worktree(repo)
    worktree_path = validate_worktree(worktree)
    granted = tuple(sorted(_capability_set(capabilities)))
    command = build_command(
        executable=executable,
        repo=repo_path,
        worktree=worktree_path,
        provider=provider,
        model=model,
        provider_config=provider_config,
        model_config=model_config,
        prompt=prompt,
        mode=mode,
        capabilities=granted,
    )
    runtime_model, gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    child_env = build_transport_environment(
        os.environ if env is None else env,
        provider=provider,
        model=model,
        provider_config=provider_config,
        model_config=model_config,
        mode=mode,
        secret=secret,
    )
    try:
        completed = runner(
            command,
            cwd=worktree_path,
            env=child_env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ClaudeAdapterError(f"could not start Claude Code: {exc}") from exc
    stdout = _redact(getattr(completed, "stdout", ""), secret)
    stderr = _redact(getattr(completed, "stderr", ""), secret)
    availability = (
        "temporarily-unavailable"
        if provider == "glm"
        and int(completed.returncode) != 0
        and GLM_QUOTA_PAUSE.search(f"{stdout}\n{stderr}")
        else "completed"
    )
    return LaneResult(
        argv=tuple(command),
        returncode=int(completed.returncode),
        cwd=worktree_path,
        host="claude",
        provider=provider,
        gateway=gateway,
        model=runtime_model,
        auth_method=auth_method,
        billable=billable,
        stdout=stdout,
        stderr=stderr,
        availability=availability,
        capabilities=granted,
        allowed_tools=allowed_tools(mode, granted),
        disallowed_tools=disallowed_tools(mode, granted),
    )
