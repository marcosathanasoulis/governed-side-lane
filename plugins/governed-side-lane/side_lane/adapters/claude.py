"""Claude Code adapter for native OAuth and explicit billable GLM routes."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from side_lane.governance import lane_system_prompt
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
# ``--allowedTools`` list derived from the capabilities the coordinator granted
# on the command line. Review lanes never receive one: their argv stays
# byte-identical to the strict read-only form.
#
# The lists are deliberately ordinary developer commands. Nothing here matches
# deploy, IAM, credential, cloud, or merge tooling; those remain forbidden by
# ``config/lane-governance.md`` and the CLI's execute-mode prompt filter.
FILE_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep")
SHELL_TOOLS: tuple[str, ...] = (
    "Bash(pnpm *)",
    "Bash(npx *)",
    "Bash(npm *)",
    "Bash(node *)",
    "Bash(./node_modules/.bin/*)",
    "Bash(yarn *)",
    "Bash(uv *)",
    "Bash(uvx *)",
    "Bash(python3 *)",
    "Bash(python3.11 *)",
    "Bash(python3.12 *)",
    "Bash(pytest *)",
    "Bash(bash -n *)",
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git checkout *)",
    "Bash(git switch *)",
    "Bash(git restore *)",
    "Bash(git stash *)",
    "Bash(git worktree list *)",
    "Bash(gh pr view *)",
    "Bash(gh pr list *)",
    "Bash(gh pr diff *)",
    "Bash(gh pr checks *)",
    "Bash(gh run view *)",
    "Bash(gh run list *)",
    "Bash(ln *)",
    "Bash(cp *)",
    "Bash(mv *)",
    "Bash(mkdir *)",
    "Bash(rm *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(grep *)",
    "Bash(find *)",
    "Bash(sed *)",
    "Bash(wc *)",
    "Bash(echo *)",
    "Bash(pwd)",
    "Bash(which *)",
    "Bash(env)",
)
GIT_PUSH_TOOLS: tuple[str, ...] = ("Bash(git push *)",)
GIT_PUSH_DENIED: tuple[str, ...] = (
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git push * --force*)",
)
SHELL_CAPABILITIES = frozenset({"shell", "workspace-write"})
KNOWN_CAPABILITIES = frozenset(
    {
        "workspace-write",
        "shell",
        "git-push",
        "gitnexus",
        "codegraph",
        "gcloud-read",
        "secret-use",
        "database-read",
        "workflow-write",
        "playwright",
    }
)


class ClaudeAdapterError(RuntimeError):
    """A Claude route cannot be safely launched."""


def _capability_set(capabilities: "tuple[str, ...] | list[str] | frozenset[str]") -> frozenset[str]:
    granted = frozenset(capabilities)
    unknown = sorted(granted - KNOWN_CAPABILITIES)
    if unknown:
        raise ClaudeAdapterError(f"unknown capability: {', '.join(unknown)}")
    return granted


def allowed_tools(mode: str, capabilities: "tuple[str, ...] | list[str] | frozenset[str]" = ()) -> tuple[str, ...]:
    """Deterministic ``--allowedTools`` rules for the granted capabilities.

    Review mode returns an empty tuple regardless of input. ``git-push``
    implies the ordinary shell set so a lane can stage and commit what it
    pushes. Unknown capability names fail closed.
    """

    granted = _capability_set(capabilities)
    if mode != "execute":
        return ()
    tools: list[str] = list(FILE_TOOLS)
    if granted & SHELL_CAPABILITIES or "git-push" in granted:
        tools.extend(SHELL_TOOLS)
    if "git-push" in granted:
        tools.extend(GIT_PUSH_TOOLS)
    return tuple(tools)


def disallowed_tools(mode: str, capabilities: "tuple[str, ...] | list[str] | frozenset[str]" = ()) -> tuple[str, ...]:
    """Deny rules that must accompany an allow rule (deny wins in Claude Code)."""

    granted = _capability_set(capabilities)
    if mode != "execute" or "git-push" not in granted:
        return ()
    return GIT_PUSH_DENIED


Runner = Callable[..., Any]


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
    capabilities: "tuple[str, ...] | list[str] | frozenset[str]" = (),
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
    capabilities: "tuple[str, ...] | list[str] | frozenset[str]" = (),
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
