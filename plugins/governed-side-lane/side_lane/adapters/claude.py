"""Claude Code adapter for native OAuth and explicit billable GLM routes."""

from __future__ import annotations

import os
import json
from pathlib import Path
import re
import signal
import subprocess
from typing import Any, Callable, Mapping, Union
from urllib.parse import urlparse

from side_lane.governance import known_capabilities, lane_system_prompt, tool_policy
from side_lane.results import LaneResult


MAX_PROMPT_CHARS = 100_000
NATIVE_PROVIDER = "claude"
NATIVE_GATEWAY = "native-claude"
BILLABLE_PROVIDERS = frozenset({"glm", "openrouter", "deepseek", "kimi", "minimax"})
# Qualification harness membership only. Runtime endpoint acceptance is driven
# by the configured per-model identity and qualification contract below.
FIRST_WAVE_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/anthropic",
    "kimi": "https://api.kimi.com/coding/",
    "minimax": "https://api.minimax.io/anthropic",
}

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
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    }
)
SCRUB_PREFIXES = (
    "ANTHROPIC_", "OPENROUTER_", "ZAI_", "ZHIPUAI_", "GLM_",
    "DEEPSEEK_", "KIMI_", "MOONSHOT_", "MINIMAX_",
)
EXACT_MODEL_ENV_NAMES = (
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
)
GLM_QUOTA_PAUSE = re.compile(
    r"\b(?:quota|usage limit|rate limit)\b.*\b(?:exceed(?:ed)?|reached|reset|temporar(?:y|ily)|hours?)\b",
    re.IGNORECASE | re.DOTALL,
)
SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
MCP_READINESS_TIMEOUT_SECONDS = 20
READINESS_REQUIRED_CAPABILITIES = frozenset({"playwright"})
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


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


Capabilities = Union[tuple[str, ...], list[str], frozenset[str]]
Runner = Callable[..., Any]


def _bounded_process(command: list[str], *, timeout: int, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one Claude worker and stop its whole process group on timeout/cancel."""

    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs.pop("check", None)
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        except ProcessLookupError:
            stdout, stderr = process.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        return subprocess.CompletedProcess(command, 124, stdout,
            (stderr or "") + "\nworker timed out; process group stopped")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
        if provider != "glm" and mode != "execute":
            raise ClaudeAdapterError("new direct providers are unqualified for review lanes")
        expected = "anthropic-compatible-readonly" if mode == "review" else "anthropic-compatible"
        if protocol != expected:
            raise ClaudeAdapterError(f"billable Claude {mode} protocol is not verified")
        endpoint = _nonempty(provider_config.get("base_url"), "base_url")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ClaudeAdapterError("billable gateway must be a clean HTTPS endpoint")
        # GLM predates the identity contract. Every newly configured direct
        # provider must prove that Claude Code settings cannot substitute an
        # alias, fast model, or subagent model behind the requested selector.
        if provider != "glm":
            identity = model_config.get("identity_contract")
            if not isinstance(identity, Mapping):
                raise ClaudeAdapterError("external route lacks an exact model identity contract")
            if identity.get("requested_model") != model or identity.get("resolved_model") != model:
                raise ClaudeAdapterError("external route has unresolved model identity")
            if identity.get("settings_precedence") != "verified":
                raise ClaudeAdapterError("external route has unverified Claude settings precedence")
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
    for name in EXACT_MODEL_ENV_NAMES:
        child[name] = runtime_model
    return child


def _required_mcp_servers(capabilities: Capabilities) -> tuple[str, ...]:
    return tuple(sorted(set(capabilities) & READINESS_REQUIRED_CAPABILITIES))


def _per_launch_settings(
    provider: str,
    model: str,
    mode: str,
    base_url: str | None = None,
    capabilities: Capabilities = (),
) -> str:
    """Return nonsecret settings that outrank user/project/local settings.

    Execute lanes intentionally run in the user workspace under the user
    identity.  The setting is process-local and cannot change a user's saved
    Claude settings.  Direct compatible transports also pin every documented
    model selector here because Kimi's settings environment overrides shell
    environment variables.
    """

    settings: dict[str, Any] = {"sandbox": {"enabled": False}} if mode == "execute" else {}
    required_servers = _required_mcp_servers(capabilities)
    if required_servers:
        # A capability grant is also explicit approval for the matching
        # project-scoped MCP server for this one process. Claude otherwise
        # leaves a fresh worktree's .mcp.json entry pending and omits its tools.
        settings["enabledMcpjsonServers"] = list(required_servers)
    if provider != NATIVE_PROVIDER:
        settings["env"] = {name: model for name in EXACT_MODEL_ENV_NAMES}
        if base_url is not None:
            settings["env"]["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
        settings["alwaysThinkingEnabled"] = True
    return json.dumps(settings, separators=(",", ":"), sort_keys=True)


def _optional_effort(model_config: Mapping[str, Any]) -> str | None:
    effort = model_config.get("reasoning_effort")
    if effort is None:
        return None
    if effort not in SUPPORTED_EFFORTS:
        raise ClaudeAdapterError("reasoning_effort is not supported by the Claude host")
    return str(effort)


def _optional_budget(model_config: Mapping[str, Any]) -> str | None:
    value = model_config.get("max_budget_usd")
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ClaudeAdapterError("max_budget_usd must be a positive finite number") from exc
    if not parsed > 0 or parsed == float("inf"):
        raise ClaudeAdapterError("max_budget_usd must be a positive finite number")
    return str(parsed)


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
                "stream-json",
                "--verbose",
                "--settings",
                _per_launch_settings(
                    provider, runtime_model, mode, provider_config.get("base_url"), capabilities
                ),
            )
        )
        effort = _optional_effort(model_config)
        if effort is not None:
            command.extend(("--effort", effort))
        budget = _optional_budget(model_config)
        if budget is not None:
            command.extend(("--max-budget-usd", budget))
        for tool in allowed_tools(mode, capabilities):
            command.extend(("--allowedTools", tool))
        for tool in disallowed_tools(mode, capabilities):
            command.extend(("--disallowedTools", tool))
    command.extend(("--append-system-prompt", lane_system_prompt(mode, repo_path)))
    return command


def _require_mcp_readiness(
    *, executable: str, cwd: Path, capabilities: Capabilities,
    env: Mapping[str, str], runner: Runner,
) -> None:
    """Health-check capability-required MCP servers before starting a model."""

    for server in _required_mcp_servers(capabilities):
        settings = json.dumps(
            {"enabledMcpjsonServers": [server]}, separators=(",", ":"), sort_keys=True
        )
        command = [executable, "--settings", settings, "mcp", "get", server]
        try:
            completed = runner(
                command,
                timeout=MCP_READINESS_TIMEOUT_SECONDS,
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ClaudeAdapterError(f"could not check {server} MCP readiness: {exc}") from exc
        stdout = str(getattr(completed, "stdout", "") or "")
        connected = any(
            re.search(r"^\s*Status:\s*[^\w]*Connected\s*$", ANSI_ESCAPE.sub("", line), re.I)
            for line in stdout.splitlines()
        )
        if int(completed.returncode) != 0 or not connected:
            status = next(
                (ANSI_ESCAPE.sub("", line).strip() for line in stdout.splitlines()
                 if "status:" in line.lower()),
                "status unavailable",
            )
            raise ClaudeAdapterError(
                f"required MCP server {server!r} is not ready before worker launch ({status})"
            )


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
    runner: Runner = None,
    readiness_runner: Runner = None,
) -> LaneResult:
    if provider != NATIVE_PROVIDER and provider != "glm":
        qualification = model_config.get("qualification")
        if not isinstance(qualification, Mapping) or qualification.get("verified") is not True:
            raise ClaudeAdapterError("external route lacks verified model transport qualification")
        _nonempty(qualification.get("verified_on"), "qualification.verified_on")
        _nonempty(qualification.get("source"), "qualification.source")
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
    timeout = model_config.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ClaudeAdapterError("timeout_seconds must be a positive integer")
    active_runner = _bounded_process if runner is None else runner
    active_readiness_runner = _bounded_process if readiness_runner is None else readiness_runner
    _require_mcp_readiness(
        executable=executable, cwd=worktree_path, capabilities=granted,
        env=child_env, runner=active_readiness_runner,
    )
    try:
        completed = active_runner(
            command,
            timeout=timeout,
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
    resolved_model, usage, attested_models = _stream_metadata(stdout)
    identity = model_config.get("identity_contract")
    if int(completed.returncode) == 0 and provider != NATIVE_PROVIDER and isinstance(identity, Mapping):
        if not attested_models:
            completed = subprocess.CompletedProcess(command, 65, stdout, stderr)
            stderr = (stderr + "\nidentity-unverified: response did not attest a model").lstrip()
        elif len(attested_models) != 1:
            completed = subprocess.CompletedProcess(command, 65, stdout, stderr)
            stderr = (stderr + "\nidentity-unverified: response attested multiple models").lstrip()
        elif resolved_model != model:
            completed = subprocess.CompletedProcess(command, 65, stdout, stderr)
            stderr = (stderr + f"\nidentity-mismatch: requested {model!r}, attested {resolved_model!r}").lstrip()
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
        requested_model=model,
        # The transport requests this selector; it cannot attest to the
        # provider's response weight/version without a verified response field.
        resolved_model=resolved_model,
        reasoning_effort=(
            model_config.get("reasoning_effort")
            if isinstance(model_config.get("reasoning_effort"), str) else None
        ),
        usage=usage,
    )


def _stream_metadata(stdout: str) -> tuple[str | None, dict[str, Any] | None, frozenset[str]]:
    """Read attested model and usage from Claude stream-json without estimating cost."""

    models: set[str] = set()
    final_usage: dict[str, Any] | None = None
    message_usage: dict[str, dict[str, int | float]] = {}
    for line_number, line in enumerate(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        if isinstance(event.get("model"), str) and event["model"]:
            models.add(event["model"])
        message = event.get("message")
        if isinstance(message, Mapping):
            if isinstance(message.get("model"), str) and message["model"]:
                models.add(message["model"])
            if isinstance(message.get("usage"), Mapping):
                message_id = message.get("id")
                identity = message_id if isinstance(message_id, str) and message_id else f"line:{line_number}"
                observed = message_usage.setdefault(identity, {})
                for key, value in message["usage"].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        observed[key] = max(observed.get(key, value), value)
        if event.get("type") == "result" and isinstance(event.get("usage"), Mapping):
            final_usage = dict(event["usage"])
    usage: dict[str, Any] | None = final_usage
    if usage is None and message_usage:
        usage = {"observation": "partial-stream"}
        for observed in message_usage.values():
            for key, value in observed.items():
                usage[key] = usage.get(key, 0) + value
    resolved = next(iter(models)) if len(models) == 1 else None
    return resolved, usage, frozenset(models)
