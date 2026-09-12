"""Native Devin CLI adapter with exact model selection and ATIF results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

from side_lane import devin_command_policy
from side_lane.credentials import scrub_backend_environment
from side_lane.governance import known_capabilities, lane_system_prompt, tool_policy
from side_lane.results import LaneResult


class DevinAdapterError(RuntimeError):
    """A Devin route cannot be launched under the configured contract."""


PopenFactory = Callable[..., Any]
DEFAULT_TIMEOUT_SECONDS = 600
SCRUB_EXACT = frozenset({
    "DEVIN_API_KEY", "DEVIN_BASE_URL", "DEVIN_MODEL", "DEVIN_PERMISSION_MODE",
    "DEVIN_SANDBOX", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
})
SCRUB_PREFIXES = (
    "ANTHROPIC_", "OPENAI_", "OPENROUTER_", "ZAI_", "ZHIPUAI_", "GLM_",
    "DEEPSEEK_", "KIMI_", "MOONSHOT_", "MINIMAX_", "AZURE_OPENAI_",
    "GEMINI_",
)
SAFE_USER_CONFIG_KEYS = frozenset({
    "version", "theme_mode", "show_path", "unicode_mode", "show_hints",
    "include_gitignored_files", "respect_gitignore", "attribution", "keymap",
    "notify", "read_config_from", "hooks", "shell",
})


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DevinAdapterError(f"{label} must be a non-empty string")
    return value.strip()


def _worktree(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise DevinAdapterError(f"path is not a Git worktree: {path}")
    return path


def _validate_route(
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    mode: str,
) -> int:
    if mode != "execute":
        raise DevinAdapterError("native Devin supports execute mode only")
    if provider_config.get("gateway") != "native-devin":
        raise DevinAdapterError("native Devin routes require gateway 'native-devin'")
    if provider_config.get("auth_method") != "oauth":
        raise DevinAdapterError("native Devin routes require its authenticated host session")
    if not isinstance(provider_config.get("billable"), bool):
        raise DevinAdapterError("native Devin routes require an explicit boolean billable setting")
    if model_config.get("runtime_model") != model or model_config.get("protocol") != "native-devin":
        raise DevinAdapterError("native Devin route would substitute the selected model")
    identity = model_config.get("identity_contract")
    if not isinstance(identity, Mapping):
        raise DevinAdapterError("native Devin route lacks an exact model identity contract")
    if identity.get("requested_model") != model or identity.get("resolved_model") != model:
        raise DevinAdapterError("native Devin route has unresolved model identity")
    if identity.get("settings_precedence") != "verified":
        raise DevinAdapterError("native Devin settings precedence is unverified")
    qualification = model_config.get("qualification")
    if not isinstance(qualification, Mapping) or qualification.get("verified") is not True:
        raise DevinAdapterError("native Devin model transport is unqualified")
    for key in ("verified_on", "source"):
        _nonempty(qualification.get(key), f"qualification.{key}")
    timeout = model_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise DevinAdapterError("timeout_seconds must be a positive integer")
    return timeout


def build_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Keep the Devin host session and MCP setup while removing transport overrides."""

    return scrub_backend_environment({
        name: value for name, value in inherited.items()
        if name not in SCRUB_EXACT and not name.startswith(SCRUB_PREFIXES)
    })


def build_command(
    *, executable: str, repo: str | Path, worktree: str | Path, provider: str,
    model: str, provider_config: Mapping[str, Any], model_config: Mapping[str, Any],
    prompt: str, export_path: str | Path, config_path: str | Path,
    mode: str = "execute", capabilities: Sequence[str] = (),
) -> tuple[str, ...]:
    program = _nonempty(executable, "Devin executable")
    repo_path = _worktree(repo)
    worktree_path = _worktree(worktree)
    if repo_path == worktree_path:
        raise DevinAdapterError("execute lane requires a dedicated worktree")
    _validate_route(provider, model, provider_config, model_config, mode)
    unknown = sorted(set(capabilities) - known_capabilities())
    if unknown:
        raise DevinAdapterError(f"unknown capability: {', '.join(unknown)}")
    task = _nonempty(prompt, "prompt")
    governed = lane_system_prompt(mode, repo_path) + "\n\n# Approved task\n\n" + task
    # No sandbox flag is passed. Devin runs locally with its normal user-host
    # MCP configuration and the governance contract in the task itself.
    return (program, "--config", str(config_path), "--permission-mode", "accept-edits",
            "--respect-workspace-trust", "false", "--model", model,
            "--export", str(export_path), "-p", governed)


def _atif_metadata(path: str | Path) -> tuple[frozenset[str], dict[str, Any] | None]:
    """Extract only provider-attested model and usage fields from ATIF JSON."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset(), None
    if not isinstance(payload, Mapping):
        return frozenset(), None
    models: set[str] = set()
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            if isinstance(step.get("model_name"), str) and step["model_name"]:
                models.add(step["model_name"])
            extra = step.get("extra")
            if isinstance(extra, Mapping) and isinstance(extra.get("generation_model"), str) and extra["generation_model"]:
                models.add(extra["generation_model"])
    if not models:
        agent = payload.get("agent")
        if isinstance(agent, Mapping) and isinstance(agent.get("model_name"), str) and agent["model_name"]:
            models.add(agent["model_name"])
    metrics = payload.get("final_metrics")
    usage = None
    if isinstance(metrics, Mapping):
        exact = {key: metrics[key] for key in (
            "total_prompt_tokens", "total_completion_tokens", "total_cached_tokens", "total_steps"
        ) if isinstance(metrics.get(key), (int, float)) and not isinstance(metrics.get(key), bool)}
        usage = exact or None
    return frozenset(models), usage


def parse_atif(path: str | Path) -> tuple[str | None, dict[str, Any] | None]:
    models, usage = _atif_metadata(path)
    return (next(iter(models)) if len(models) == 1 else None), usage


def _parse_jsonc(raw: str) -> dict[str, Any]:
    """Parse Devin's documented JSON-with-comments config or fail closed."""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(raw):
        char = raw[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if raw.startswith("//", index):
            newline = raw.find("\n", index + 2)
            index = len(raw) if newline < 0 else newline
            continue
        if raw.startswith("/*", index):
            end = raw.find("*/", index + 2)
            if end < 0:
                raise DevinAdapterError("Devin user config contains an unterminated comment")
            index = end + 2
            continue
        output.append(char)
        index += 1
    without_comments = "".join(output)
    # Remove JSONC trailing commas without altering commas inside strings.
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(without_comments):
        char = without_comments[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        if char == ",":
            lookahead = index + 1
            while lookahead < len(without_comments) and without_comments[lookahead].isspace():
                lookahead += 1
            if lookahead < len(without_comments) and without_comments[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    try:
        payload = json.loads("".join(output))
    except json.JSONDecodeError as exc:
        raise DevinAdapterError(f"cannot parse Devin user config: {exc}") from exc
    if not isinstance(payload, dict):
        raise DevinAdapterError("Devin user config must be a JSON object")
    return payload


def _load_user_config(path: Path | None = None) -> dict[str, Any]:
    source = path or (Path(os.environ["APPDATA"]) / "devin" / "config.json"
                      if os.name == "nt" and os.environ.get("APPDATA")
                      else Path.home() / ".config" / "devin" / "config.json")
    if not source.exists():
        return {}
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise DevinAdapterError(f"cannot read Devin user config: {exc}") from exc
    return _parse_jsonc(raw)


def _merge_policy_hook(inherited: object, command: str | None) -> object:
    if command is None:
        return inherited
    if inherited is None:
        hooks: dict[str, Any] = {}
    elif isinstance(inherited, Mapping):
        hooks = {key: value for key, value in inherited.items()}
    else:
        raise DevinAdapterError("Devin user config hooks must be a JSON object")
    existing = hooks.get("PreToolUse", [])
    if not isinstance(existing, list):
        raise DevinAdapterError("Devin user config hooks.PreToolUse must be an array")
    hooks["PreToolUse"] = [*existing, {
        "matcher": "^exec$",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }]
    return hooks


def _runtime_config(model: str, capabilities: Sequence[str],
                    user_config: Mapping[str, Any] | None = None,
                    policy_hook_command: str | None = None) -> dict[str, Any]:
    allow = ["Read(**)", "Write(**)"]
    policy = tool_policy()
    for capability in sorted(set(capabilities) & {"shell", "workspace-write", "git-push"}):
        for rule in policy.allowed.get(capability, ()):
            if rule.startswith("Bash("):
                grant = devin_command_policy.devin_exec_rule(rule)
                if grant is not None and grant not in allow:
                    allow.append(grant)
    # Devin's permission syntax accepts exact MCP tool IDs directly. Preserve
    # the canonical per-capability list rather than widening it to mcp__server__*.
    for capability in sorted(set(capabilities) & {"playwright", "gitnexus", "codegraph"}):
        for rule in policy.allowed.get(capability, ()):
            if rule.startswith("mcp__") and rule not in allow:
                allow.append(rule)
    inherited = user_config or {}
    config = {key: inherited[key] for key in SAFE_USER_CONFIG_KEYS if key in inherited}
    merged_hooks = _merge_policy_hook(inherited.get("hooks"), policy_hook_command)
    if merged_hooks is not None:
        config["hooks"] = merged_hooks
    agent = inherited.get("agent")
    config["agent"] = {
        **({"show_history_on_continue": agent["show_history_on_continue"]}
           if isinstance(agent, Mapping) and isinstance(agent.get("show_history_on_continue"), bool)
           else {}),
        "model": model,
    }
    permissions = inherited.get("permissions")
    preserved_rules: dict[str, list[str]] = {"deny": [], "ask": []}
    if isinstance(permissions, Mapping):
        for kind in preserved_rules:
            rules = permissions.get(kind, [])
            if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
                raise DevinAdapterError(f"Devin user config permissions.{kind} must be an array of strings")
            preserved_rules[kind] = list(rules)
    for capability in sorted(set(capabilities) & {"git-push"}):
        for rule in policy.denied.get(capability, ()):
            grant = devin_command_policy.devin_exec_deny_rule(rule)
            if grant is not None and grant not in preserved_rules["deny"]:
                preserved_rules["deny"].append(grant)
    config["permissions"] = {
        "allow": allow,
        **preserved_rules,
    }
    config.update({"version": inherited.get("version", 1), "subagents_enabled": False,
                   "auto_update": False})
    return config


def _run(command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: int,
         popen: PopenFactory) -> tuple[int, str, str]:
    try:
        process = popen(command, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        start_new_session=True)
    except OSError as exc:
        raise DevinAdapterError(f"could not start Devin executable: {exc}") from exc
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
        return 124, stdout or "", stderr or ""
    return int(process.returncode), stdout or "", stderr or ""


def launch(
    *, executable: str, repo: str | Path, worktree: str | Path, provider: str,
    model: str, provider_config: Mapping[str, Any], model_config: Mapping[str, Any],
    prompt: str, mode: str = "execute", capabilities: Sequence[str] = (),
    env: Mapping[str, str] | None = None, popen: PopenFactory = subprocess.Popen,
    user_config_path: Path | None = None,
) -> LaneResult:
    repo_path = _worktree(repo)
    worktree_path = _worktree(worktree)
    timeout = _validate_route(provider, model, provider_config, model_config, mode)
    support_root = worktree_path.parent / ".side-lane-runtime"
    support_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{worktree_path.name}-", dir=support_root))
    export_path = run_dir / "devin-atif.json"
    config_path = run_dir / "devin-config.json"
    command_capabilities = sorted(set(capabilities) & {"shell", "workspace-write", "git-push"})
    allowed_rules = list(dict.fromkeys(
        rule for capability in command_capabilities
        for rule in tool_policy().allowed.get(capability, ()) if rule.startswith("Bash(")
    ))
    denied_rules = list(dict.fromkeys(
        rule for capability in command_capabilities
        for rule in tool_policy().denied.get(capability, ()) if rule.startswith("Bash(")
    ))
    policy_hook_command = None
    if allowed_rules:
        policy_path = run_dir / "devin-command-policy.json"
        policy_path.write_text(json.dumps({"allowed": allowed_rules, "denied": denied_rules},
                                          indent=2) + "\n", encoding="utf-8")
        policy_hook_command = shlex.join((sys.executable, str(Path(devin_command_policy.__file__)),
                                          str(policy_path)))
    config_path.write_text(json.dumps(_runtime_config(
        model, capabilities, _load_user_config(user_config_path), policy_hook_command
    ), indent=2) + "\n",
                           encoding="utf-8")
    command = build_command(executable=executable, repo=repo_path, worktree=worktree_path,
        provider=provider, model=model, provider_config=provider_config,
        model_config=model_config, prompt=prompt, export_path=export_path,
        config_path=config_path, mode=mode, capabilities=capabilities)
    returncode, stdout, stderr = _run(command, cwd=worktree_path,
        env=build_environment(os.environ if env is None else env), timeout=timeout, popen=popen)
    models, usage = _atif_metadata(export_path)
    resolved_model = next(iter(models)) if len(models) == 1 else None
    if returncode == 0:
        if not models:
            returncode = 65
            stderr = (stderr + "\nidentity-unverified: ATIF did not attest a model").lstrip()
        elif len(models) != 1:
            returncode = 65
            stderr = (stderr + "\nidentity-unverified: ATIF attested multiple models").lstrip()
        elif resolved_model != model:
            returncode = 65
            stderr = (stderr + f"\nidentity-mismatch: requested {model!r}, attested {resolved_model!r}").lstrip()
    return LaneResult(argv=command, returncode=returncode, cwd=worktree_path,
        host="devin", provider=provider, gateway="native-devin", model=model,
        auth_method="oauth", billable=provider_config["billable"], stdout=stdout, stderr=stderr,
        capabilities=tuple(sorted(set(capabilities))), requested_model=model,
        resolved_model=resolved_model, usage=usage, provider_artifact=str(export_path))
