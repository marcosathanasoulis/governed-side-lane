"""Native Devin CLI adapter with exact model selection and ATIF results."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
import re
import signal
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

from side_lane import devin_command_policy
from side_lane.credentials import scrub_backend_environment
from side_lane.governance import known_capabilities, lane_system_prompt, tool_policy
from side_lane.mcp_run_config import (
    McpRunServer,
    devin_local_payload,
    ensure_no_registration_conflicts,
    require_env_references,
    startup_note,
    write_ephemeral,
)
from side_lane.read_roots import read_rule, scope_note
from side_lane.results import LaneResult
from side_lane.worktrees import (
    WorktreeError,
    ensure_devin_local_mcp_exclusion,
    ensure_devin_local_mcp_untracked,
)


class DevinAdapterError(RuntimeError):
    """A Devin route cannot be launched under the configured contract."""


PopenFactory = Callable[..., Any]
DEFAULT_TIMEOUT_SECONDS = 1800
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


#: Characters a shell still acts on inside a double-quoted word. A spelling
#: containing one cannot be granted as an equivalent of the literal path.
_DOUBLE_QUOTE_UNSAFE = frozenset('"\\$`')

#: Basenames of Python interpreters that may carry a benign leading
#: environment assignment in the native permission layer.
_PYTHON_INTERPRETER_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?")

#: The only leading environment assignment that is admitted as a native
#: pregrant.  It has no path or shell-expansion semantics and is paired
#: exactly with an already-granted Python interpreter.
_NATIVE_ENV_PREGRANT = "PYTHONDONTWRITEBYTECODE=1"


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DevinAdapterError(f"{label} must be a non-empty string")
    return value.strip()


def _path_spellings(path: Path) -> tuple[str, ...]:
    """Shell spellings of ``path`` that all denote exactly ``path``.

    Devin matches an ``Exec(...)`` grant against the command as the worker
    spelled it, so the lane worktree written with different (but equivalent)
    quoting is a different command. Every spelling returned here is re-parsed
    with :mod:`shlex` and kept only when it splits back to the exact literal
    path and contains nothing a shell would expand, so granting all of them
    accepts the same authority as the canonical spelling and never a wider one:
    the quoting variants cannot carry a command separator, a substitution, or a
    different target.
    """

    literal = str(path)
    candidates = [shlex.quote(literal)]
    if not any(character in literal for character in _DOUBLE_QUOTE_UNSAFE):
        candidates.append(f'"{literal}"')
    spellings: list[str] = []
    for candidate in candidates:
        if candidate in spellings:
            continue
        try:
            parsed = shlex.split(candidate)
        except ValueError:
            continue
        if parsed == [literal]:
            spellings.append(candidate)
    return tuple(spellings)


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
    read_roots: Sequence[Path] = (),
    run_mcp_servers: "Mapping[str, McpRunServer] | None" = None,
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
    note = scope_note(read_roots)
    if run_mcp_servers:
        # The registrations themselves are delivered through the lane
        # worktree's local-scope MCP file (see `launch`); the task text only
        # tells the worker they exist and that presence is not authentication.
        note = (note or "") + startup_note(run_mcp_servers)
    governed = (lane_system_prompt(mode, repo_path)
                + (f"\n\n{note}" if note else "")
                + "\n\n# Approved task\n\n" + task)
    # No sandbox flag is passed. Devin runs locally with its normal user-host
    # MCP configuration and the governance contract in the task itself. The
    # generated `--config` file is the general CLI config: `devin mcp add
    # --help` documents the MCP registrations as separate user, project and
    # local files that this flag does not replace, so nothing here registers,
    # removes or shadows a connector.
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
        "matcher": devin_command_policy.policy_hook_matcher(),
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }]
    return hooks


def _file_tool_rules(worktree: Path | None,
                     read_roots: Sequence[Path] = ()) -> list[str]:
    """Exact file-tool rules for the lane worktree and granted read roots.

    Reads are limited to the lane's own worktree plus each coordinator-supplied
    read-only root; writes are limited to the lane worktree. The former
    ``Read(**)``/``Write(**)`` pair granted every path on the machine, which is
    both wider than any lane needs and — being a rule that matches everything —
    useless as a statement of what the worker may actually reach.
    ``read_roots.read_rule`` re-validates each root's characters, so a root
    carrying glob or rule-delimiter syntax fails closed here even when this
    helper is called directly instead of through the CLI.
    """

    if worktree is None:
        # Reachable only from direct calls: `launch` always supplies the lane
        # worktree. Emitting no file-tool rule fails closed — Devin prompts,
        # and a prompt ends a non-interactive run — rather than falling back to
        # a wildcard.
        return []
    rules = [read_rule(worktree)]
    for root in sorted({root for root in read_roots if root != worktree}, key=str):
        rule = read_rule(root)
        if rule not in rules:
            rules.append(rule)
    rules.append(f"Write({worktree}/**)")
    return rules


def _runtime_config(model: str, capabilities: Sequence[str],
                    user_config: Mapping[str, Any] | None = None,
                    policy_hook_command: str | None = None,
                    worktree: Path | None = None,
                    read_roots: Sequence[Path] = ()) -> dict[str, Any]:
    """Build Devin's runtime config from the canonical tool policy.

    File tools are scoped to real directories: the lane worktree, plus any
    read-only root the coordinator granted explicitly. Writes never reach a
    read root.

    Devin matches `Exec(<prefix>)` grants as whole-word command prefixes, so a
    command spelled `git -C <lane worktree> status` does not match
    `Exec(git status)` and would prompt, ending a non-interactive run; the
    `-C` spellings are therefore granted explicitly, while the PreToolUse hook
    still normalises `-C` before matching deny rules.
    """
    allow = _file_tool_rules(worktree, read_roots)
    policy = tool_policy()
    for capability in sorted(set(capabilities) & {"shell", "workspace-write", "git-push"}):
        for rule in policy.allowed.get(capability, ()):
            if rule.startswith("Bash("):
                grant = devin_command_policy.devin_exec_rule(rule)
                if grant is not None and grant not in allow:
                    allow.append(grant)
    # Devin's native permission layer prompts for a shell builtin before the
    # PreToolUse hook can validate its lane containment. Grant only the
    # spelling of `cd`; the hook remains authoritative and rejects every
    # target outside the lane (or an ambiguous expansion).
    if "shell" in capabilities and "Exec(cd)" not in allow:
        allow.append("Exec(cd)")
    # Devin's native permission matcher does not treat a lane venv launcher
    # as the canonical ``Exec(python*)`` prefix, even though the policy hook
    # validates the launcher and its resolved interpreter. Admit only the
    # standard lane-contained spellings; the hook still rejects non-lane
    # working directories, planted interpreters, and denied commands.
    if "shell" in capabilities:
        for interpreter in ("python", "python3", "python3.11"):
            grant = f"Exec(.venv/bin/{interpreter})"
            if grant not in allow:
                allow.append(grant)
    # The native permission matcher sees leading environment assignments
    # before the PreToolUse hook strips them.  Derive a narrow native
    # pregrant for the benign exact form ``PYTHONDONTWRITEBYTECODE=1
    # <granted interpreter>`` from the interpreter grants already emitted;
    # the canonical hook still enforces the underlying grant, deny rules,
    # and read/write bounds.
    if "shell" in capabilities:
        if "Exec(env)" not in allow:
            allow.append("Exec(env)")
        extra_pregrants: list[str] = []
        for rule in allow:
            if not (rule.startswith("Exec(") and rule.endswith(")")):
                continue
            command = rule[len("Exec("):-1].strip()
            first = command.split()[0] if command else ""
            if _PYTHON_INTERPRETER_NAME.fullmatch(Path(first).name):
                pregrant = f"Exec({_NATIVE_ENV_PREGRANT} {first})"
                if pregrant not in allow and pregrant not in extra_pregrants:
                    extra_pregrants.append(pregrant)
        allow.extend(extra_pregrants)
    # MCP rules are copied from the canonical policy as exact per-tool IDs and
    # never widened to a server-wide wildcard. Devin's own permissions
    # reference documents these exact `mcp__<server>__<tool>` MCP permission
    # IDs (https://docs.devin.ai/cli/reference/permissions), so the canonical
    # spelling is the documented one and not a Claude-host convention. An
    # observed generic `mcp_call_tool` transcript carrying server_name and
    # tool_name does not imply a different rule syntax for a *registered*
    # server; registration itself is documented across the user, project and
    # local MCP config files (https://docs.devin.ai/cli/extensibility/mcp/
    # configuration). What is still unproven is a live call: a rule that names
    # a server registered nowhere grants nothing, so these entries must not be
    # read as capability evidence — no connector-name registration and no
    # permission-ID documentation substitutes for a successful call. A missing
    # grant makes Devin prompt, and a prompt ends a non-interactive run.
    # ``asana-read``/``drive-read`` share the fixed user-global ``cm-services``
    # registration the coordinator provisions into the worker host's Devin
    # user config; each capability admits only its own exact
    # ``mcp__cm-services__<tool>`` rules from the canonical policy — the shared
    # server never widens one grant into the other's tools.
    for capability in sorted(set(capabilities) & {"playwright", "gitnexus", "codegraph", "slack-read", "aws-read", "asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read", "contentful-read", "contentful-master-read"}):
        for rule in policy.allowed.get(capability, ()):
            if rule.startswith("mcp__") and rule not in allow:
                allow.append(rule)
    # Every `-C` grant follows every canonical grant, and only subcommand-
    # naming rules qualify: `Exec(git)` never yields `Exec(git -C ...)` without
    # a subcommand. Deny rules are never widened — the hook strips `-C` before
    # matching them with `anywhere=True`, so denies already cover the spelling.
    dash_c_grants: list[str] = []
    if worktree is not None:
        # Devin matches the grant against the command as spelled, so one
        # spelling of the lane path does not cover the others. A lane whose
        # worktree had no spaces was granted only the unquoted spelling and a
        # worker that wrote `git -C "<lane>" ...` then matched no grant at all
        # and was prompted, which ends a non-interactive run (reproduced
        # 2026-09-19). Every equivalent spelling of the path is granted for
        # that reason; `_path_spellings` proves each one denotes the same path,
        # so this cannot widen the authority the canonical rule already gives.
        # The `-C .` spelling stays bare because the CLI's working directory is
        # already the lane worktree, so `.` needs no quoting to be typed.
        spellings = _path_spellings(worktree)
        for rule in allow:
            if not (rule.startswith("Exec(git ") and rule.endswith(")")):
                continue
            rest = rule[len("Exec(git "):-1].strip()
            if not rest:
                continue
            for target in (*spellings, "."):
                grant = f"Exec(git -C {target} {rest})"
                if grant not in allow and grant not in dash_c_grants:
                    dash_c_grants.append(grant)
        allow.extend(dash_c_grants)
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


def _merge_local_mcp_config(
    worktree_path: Path, servers: "Mapping[str, McpRunServer]"
) -> tuple[Path, str | None]:
    """Merge per-run servers into Devin's local-scope MCP file.

    `devin mcp add --help` documents the not-committed local project scope
    ``.devin/mcp_config.local.json``; Devin 3000.10.21 resolves it at the git
    worktree root. The merge is additive and shape-preserving: an existing
    file may legitimately be ``{}`` (no servers yet) or carry top-level keys
    beside ``mcpServers`` — both survive the merge, existing entries are
    kept, a name clash fails closed (overwriting a user registration or
    shadowing it would both misstate which server is live), and the whole
    file is restored or removed after the run
    (`_restore_local_mcp_config`) so the lane's delivery check never sees it
    as an uncommitted artifact.

    Returns ``(path, original text or None when the file did not exist)``.
    """

    path = worktree_path / ".devin" / "mcp_config.local.json"
    original: str | None = None
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            original = path.read_text(encoding="utf-8")
            payload = json.loads(original)
        except (OSError, json.JSONDecodeError) as exc:
            raise DevinAdapterError(
                f"cannot read the existing Devin local MCP config: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DevinAdapterError(
                "Devin local MCP config must be a JSON object with an mcpServers object"
            )
    existing_servers = payload.get("mcpServers", {})
    if not isinstance(existing_servers, dict):
        raise DevinAdapterError(
            "Devin local MCP config must be a JSON object with an mcpServers object"
        )
    merged_servers = dict(existing_servers)
    additions = devin_local_payload(servers)["mcpServers"]
    clash = sorted(set(additions) & set(merged_servers))
    if clash:
        raise DevinAdapterError(
            "the lane worktree's Devin local MCP config already registers "
            f"server name(s): {', '.join(clash)}"
        )
    merged_servers.update(additions)
    write_ephemeral(path.parent, path.name, {**payload, "mcpServers": merged_servers})
    return path, original


def _restore_local_mcp_config(path: Path, original: str | None) -> None:
    """Best-effort restoration of the local-scope MCP file after a run."""

    try:
        if original is None:
            path.unlink(missing_ok=True)
            with suppress(OSError):
                path.parent.rmdir()
        else:
            path.write_text(original, encoding="utf-8")
    except OSError:
        # Restoration is best-effort: a leftover file is visible in the lane's
        # delivery check (uncommitted artifact) rather than silently erased.
        pass


def launch(
    *, executable: str, repo: str | Path, worktree: str | Path, provider: str,
    model: str, provider_config: Mapping[str, Any], model_config: Mapping[str, Any],
    prompt: str, mode: str = "execute", capabilities: Sequence[str] = (),
    env: Mapping[str, str] | None = None, popen: PopenFactory = subprocess.Popen,
    user_config_path: Path | None = None, read_roots: Sequence[Path] = (),
    run_mcp_servers: "Mapping[str, McpRunServer] | None" = None,
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
    if mode == "execute":
        # Install the hook whenever an execute lane has a worktree to contain,
        # even with an empty Bash allowlist (e.g. no shell/workspace-write/
        # git-push capability requested): file-tool writes still need
        # containment, and `_evaluate_exec` fails closed on an empty
        # `allowed` list rather than silently permitting commands.
        policy_path = run_dir / "devin-command-policy.json"
        # The worktree anchors write containment and `git -C` normalisation in
        # the hook; without it the hook fails closed on every file-mutating tool.
        policy_path.write_text(json.dumps({"allowed": allowed_rules, "denied": denied_rules,
                                           "worktree": str(worktree_path)},
                                          indent=2) + "\n", encoding="utf-8")
        policy_hook_command = shlex.join((sys.executable, str(Path(devin_command_policy.__file__)),
                                          str(policy_path)))
    config_path.write_text(json.dumps(_runtime_config(
        model, capabilities, _load_user_config(user_config_path), policy_hook_command,
        worktree=worktree_path, read_roots=read_roots
    ), indent=2) + "\n",
                           encoding="utf-8")
    command = build_command(executable=executable, repo=repo_path, worktree=worktree_path,
        provider=provider, model=model, provider_config=provider_config,
        model_config=model_config, prompt=prompt, export_path=export_path,
        config_path=config_path, mode=mode, capabilities=capabilities,
        read_roots=read_roots, run_mcp_servers=run_mcp_servers)
    child_env = build_environment(os.environ if env is None else env)
    # Per-run MCP delivery (execute only — this adapter supports no other
    # mode): validate env references against the environment the worker child
    # actually gets, then merge the local-scope file and restore it after the
    # run. Devin's header `${ENV}` expansion is UNVERIFIED (module docstring
    # in side_lane.mcp_run_config); a non-expanding host fails visibly at the
    # bridge instead of leaking the reference.
    local_mcp: tuple[Path, str | None] | None = None
    if run_mcp_servers:
        require_env_references(run_mcp_servers, child_env)
        # The local-scope merge below fails closed on a local-file clash; this
        # check extends the same rule to Devin's user and project scopes,
        # which the local merge never sees.
        ensure_no_registration_conflicts(
            run_mcp_servers, "devin", worktree_path, env=child_env
        )
        try:
            ensure_devin_local_mcp_untracked(worktree_path)
        except WorktreeError as exc:
            raise DevinAdapterError(str(exc)) from exc
        # Keep an untracked generated local-scope file out of `git status`/
        # `git add -A` for the whole run — the exclusion lands BEFORE the file
        # is written, while restore only runs after the worker exits. Tracked
        # local configs are rejected above because Git excludes cannot protect
        # them from a worker's mid-run add.
        ensure_devin_local_mcp_exclusion(worktree_path)
        local_mcp = _merge_local_mcp_config(worktree_path, run_mcp_servers)
    try:
        returncode, stdout, stderr = _run(command, cwd=worktree_path,
            env=child_env, timeout=timeout, popen=popen)
    finally:
        if local_mcp is not None:
            _restore_local_mcp_config(*local_mcp)
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
