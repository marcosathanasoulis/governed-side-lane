"""Claude Code adapter for native OAuth and explicit billable GLM routes."""

from __future__ import annotations

import os
import json
from contextlib import suppress
from pathlib import Path
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence, Union
from urllib.parse import urlparse

from side_lane import report_stop_hook, routed_read_pagination
from side_lane.capabilities import (
    CAPABILITY_MCP_SERVERS,
    CM_SERVICES_CAPABILITIES,
    RUN_CONFIG_CAPABILITIES,
    USER_SCOPE_MCP_CAPABILITIES,
)
from side_lane.credentials import scrub_backend_environment
from side_lane.governance import known_capabilities, lane_system_prompt, tool_policy
from side_lane.mcp_run_config import (
    McpRunServer,
    build_strict_mcp_bundle,
    claude_payload,
    ensure_no_registration_conflicts,
    require_env_references,
    write_ephemeral,
    write_strict_mcp_bundle,
)
from side_lane.read_roots import scope_note
from side_lane.web_domains import claude_rules, scope_note as web_scope_note
from side_lane.results import LaneResult
from side_lane.redaction import redact_provider_secret


MAX_PROMPT_CHARS = 100_000
NATIVE_PROVIDER = "claude"
NATIVE_GATEWAY = "native-claude"
BILLABLE_PROVIDERS = frozenset({"glm", "openrouter", "deepseek", "kimi", "minimax", "anthropic", "omniroute"})
# Routed providers forward our requested selector to an upstream pool the
# router selects itself (OmniRoute-style). They are validated against an
# explicit routing-policy contract, never the exact-model identity contract.
ROUTED_PROVIDERS = frozenset({"omniroute"})
ROUTED_GATEWAYS = {"omniroute": "omniroute-router"}
ROUTING_POLICY_CONTRACT_KEY = "routing_policy_contract"
# The installed Superpowers plugin adds a coordinator-intake SessionStart
# hook. Routed workers already receive the pinned lane skills and role prompt,
# so only this exact plugin is disabled in the disposable routed settings.
ROUTED_COORDINATOR_PLUGIN = "superpowers@claude-plugins-official"
# Claude Code sends ANTHROPIC_API_KEY as `X-Api-Key` (first-party key auth)
# and ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer` (proxy/OAuth-style); a
# first-party Anthropic key must use the former.
# Source: code.claude.com/docs/en/env-vars.
FIRST_PARTY_API_KEY_GATEWAY = "direct-anthropic"
# Qualification harness membership only. Runtime endpoint acceptance is driven
# by the configured per-model identity and qualification contract below.
FIRST_WAVE_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/anthropic",
    "kimi": "https://api.kimi.com/coding/",
    "minimax": "https://api.minimax.io/anthropic",
    "anthropic": "https://api.anthropic.com",
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
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CONFIG_DIR",
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
# Report-only execute lanes (`--report-only`) get one deterministic Stop hook
# inside the same invocation. The filename and action are fixed here; a worker
# never chooses its own acceptance path, and the hook is armed with the run
# baseline the runner captured before the worker started, so a report the
# worktree already contained at HEAD is not mistaken for this run's.
REPORT_ONLY_REPORT_NAME = "SIDE_LANE_REPORT.md"
REPORT_ONLY_HOOK_TIMEOUT_SECONDS = 10
# Process-local controls for routed runs. Compaction starts below the
# reviewed OmniRoute ceiling and output is bounded per response; these do not
# claim provider capacity or replace the router's fail-closed guard.
ROUTED_AUTOCOMPACT_WINDOW = "100k"
ROUTED_MAX_OUTPUT_TOKENS = "16384"
# The routed lane's auto-compaction window is an operator execution budget, not
# a vendor context-capacity claim: it bounds how much conversation the CLI
# carries before it compacts, and the CLI still owns whatever reserve it takes
# below the requested window — no reserve arithmetic is done here. An explicit
# ``autocompact_window_tokens`` is rendered as the plain decimal integer the
# installed CLI parses; it is neither an enum of one reviewed value nor a
# derived window. Absent or null, the routed default above is unchanged, and
# the option is read on the routed provider only: the native Claude, direct
# provider, and review paths never see it and stay byte-identical.
AUTOCOMPACT_WINDOW_TOKENS_KEY = "autocompact_window_tokens"
AUTOCOMPACT_WINDOW_TOKENS_MIN = 100_000
AUTOCOMPACT_WINDOW_TOKENS_MAX = 1_000_000
# Claude Code's native auto-memory — the per-project memory directory it loads
# and writes under the controlled HOME — is switched off in every lane
# worker's child environment, native or routed, review or execute. The value is
# forced after transport scrubbing, so a caller's inherited false cannot
# survive into the child. This governs that native feature only: it is a
# same-user control, not an operating-system write control, because an execute
# worker's own manual tools can still write the path. See
# docs/lane-context.md and ``HOST_MEMORY_READONLY_NOTE`` below.
AUTO_MEMORY_DISABLE_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
AUTO_MEMORY_DISABLED_VALUE = "1"
# Exact MCP server names a granted capability maps to. ``slack-read`` maps to
# the server registered as ``slack``; the canonical lane governance names the
# registration, not the capability. The graph capabilities use their own exact
# names. ``aws-read`` names the one server its per-run --mcp-config file may
# register (side_lane.mcp_run_config); it never appears in
# enabledMcpjsonServers because its registration is not a project .mcp.json
# entry. Every cm-services-family capability — asana, drive, gcloud,
# database, algolia, contentful, and Gateway — maps to the fixed local stdio
# server registered as ``cm-services`` in the worker host's user-global config
# (the controlled HOME the coordinator provisions); a user-scope registration
# needs no project approval either. Server names must never be wildcarded and
# a grant never approves a server outside this mapping.
# CAPABILITY_MCP_SERVERS, RUN_CONFIG_CAPABILITIES, and USER_SCOPE_MCP_CAPABILITIES
# are imported from side_lane.capabilities to avoid circular imports.

# Capabilities whose MCP server additionally requires the expensive
# pre-launch ``mcp get`` health probe. Approval (above) is a per-process
# settings fact; readiness is a live subprocess check, and the two stay
# separate so a graph/Slack grant never pays for — or gates on — a probe.
# ``cm-services`` is probed once even when several of its capabilities are
# granted.
READINESS_REQUIRED_CAPABILITIES = frozenset(
    {"playwright", "asana-read", "drive-read", "gcloud-read", "database-read", "algolia-read",
     "contentful-read", "contentful-master-read", "gateway-read"}
)


def _effective_mcp_registrations(
    host: str,
    repo: Path,
    worktree: Path,
    home: Path,
    granted_capabilities: "frozenset[str] | set[str] | tuple[str, ...]" = (),
) -> list[tuple[str, str, Path]]:
    """Return (server_name, scope, path, definition) tuples from user/project/worktree configs.

    Only returns registrations for servers whose corresponding capabilities are in
    ``granted_capabilities`` and are ``USER_SCOPE_MCP_CAPABILITIES`` or
    ``RUN_CONFIG_CAPABILITIES``. Scope is "user", "project", or "worktree".
    Used to build the narrow bundle for ``--strict-mcp-config`` in routed
    execute lanes.

    Raises ``ClaudeAdapterError`` if the same server name has different definitions
    across scopes (silent last-write-wins would hide a configuration conflict).
    """
    results: list[tuple[str, str, Path, dict[str, Any]]] = []
    if host != "claude":
        return []

    granted = set(granted_capabilities)
    # Build the set of server names whose corresponding capability is granted.
    # Includes both cm-services family and host-native servers (gitnexus, codegraph,
    # playwright, slack).
    user_scope_names: set[str] = set()
    for cap in USER_SCOPE_MCP_CAPABILITIES | RUN_CONFIG_CAPABILITIES:
        if cap not in granted:
            continue
        server = CAPABILITY_MCP_SERVERS.get(cap)
        if server:
            user_scope_names.add(server)

    # Track definitions seen so far to detect same-name conflicts.
    # key: server name, value: (scope, path, definition)
    seen: dict[str, tuple[str, Path, dict[str, Any]]] = {}

    def _check_and_record(
        name: str, scope: str, path: Path, definition: dict[str, Any],
    ) -> None:
        """Record a definition, raising on conflict with a prior one."""
        if name in seen:
            prior_scope, prior_path, prior_def = seen[name]
            if prior_def != definition:
                raise ClaudeAdapterError(
                    f"MCP server {name!r} has conflicting definitions: "
                    f"{prior_scope} scope ({prior_path}) defines it differently from "
                    f"{scope} scope ({path}). Identical definitions coalesce; "
                    f"different definitions must be reconciled before routing."
                )
            # Identical — skip duplicate entry
            return
        seen[name] = (scope, path, definition)
        results.append((name, scope, path, definition))

    # Scan user-global config (~/.claude.json)
    user_config = home / ".claude.json"
    try:
        os.stat(user_config)
    except FileNotFoundError:
        user_config_exists = False
    except OSError as exc:
        raise ClaudeAdapterError(
            f"user MCP config {user_config} could not be read: {exc}"
        ) from exc
    else:
        user_config_exists = True
    if user_config_exists:
        try:
            cfg = json.loads(user_config.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ClaudeAdapterError(
                f"user MCP config {user_config} is not valid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise ClaudeAdapterError(
                f"user MCP config {user_config} could not be read: {exc}"
            ) from exc
        # Top-level mcpServers
        top_servers = cfg.get("mcpServers", {})
        if isinstance(top_servers, dict):
            for name, definition in top_servers.items():
                if name in user_scope_names and isinstance(definition, dict):
                    _check_and_record(name, "user", user_config, definition)
        # Project entries
        projects = cfg.get("projects", {})
        if isinstance(projects, dict):
            proj_entry = projects.get(str(repo))
            if isinstance(proj_entry, dict):
                proj_servers = proj_entry.get("mcpServers", {})
                if isinstance(proj_servers, dict):
                    for name, definition in proj_servers.items():
                        if name in user_scope_names and isinstance(definition, dict):
                            _check_and_record(name, "project", user_config, definition)

    # Lane worktree .mcp.json — server names are top-level keys (no mcpServers wrapper)
    worktree_mcp = worktree / ".mcp.json"
    if worktree_mcp.is_file():
        try:
            cfg = json.loads(worktree_mcp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ClaudeAdapterError(
                f"worktree MCP config {worktree_mcp} is not valid JSON: {exc}"
            ) from exc
        except OSError as exc:
            raise ClaudeAdapterError(
                f"worktree MCP config {worktree_mcp} could not be read: {exc}"
            ) from exc
        servers = cfg.get("mcpServers", {})
        if not isinstance(servers, dict) or not servers:
            # .mcp.json uses server names as top-level keys directly
            servers = cfg if isinstance(cfg, dict) else {}
        if isinstance(servers, dict):
            for name, definition in servers.items():
                if name in user_scope_names and isinstance(definition, dict):
                    _check_and_record(name, "worktree", worktree_mcp, definition)

    return results


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# Per-executable cache: does this executable accept --strict-mcp-config?
# Populated once per executable identity on first probe.  The key is the
# executable string as passed to the adapter; the value is True if the
# installed CLI supports the flag.
_strict_mcp_executable_cache: dict[str, bool] = {}


def _check_strict_mcp_support(
    executable: str, runner: Runner,
    *, cwd: Path, env: "Mapping[str, str]",
) -> bool:
    """Return True if the installed CLI accepts ``--strict-mcp-config``.

    Probed in the actual worker working directory and environment rather
    than the coordinator's ``Path.cwd()`` or ``os.environ``.  Cached per
    executable identity after the first probe.  A missing or broken
    executable is cached as False so the check is not retried.
    """

    if executable in _strict_mcp_executable_cache:
        return _strict_mcp_executable_cache[executable]

    result = False
    try:
        completed = runner(
            [executable, "--help"],
            timeout=10,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )
        result = completed.returncode == 0 and "--strict-mcp-config" in completed.stdout
    except (OSError, subprocess.SubprocessError):
        result = False

    _strict_mcp_executable_cache[executable] = result
    return result
PLAYWRIGHT_STARTUP_INSTRUCTION = """\


# Claude Code Playwright startup

This task requires the Playwright MCP server. Before assessing browser-tool
availability or starting browser work, check whether the Playwright tools have
already appeared. If they have not, call `WaitForMcpServers` with
`servers: ["playwright"]`. Continue only when the tools are present or the wait
reports `ready: true` and adds them. If the wait reports that Playwright failed,
needs authentication, is disabled, or remains pending, stop and report that
exact state instead of claiming the tools were never configured.
"""

SLACK_READ_STARTUP_INSTRUCTION = """\


# Claude Code Slack read startup

This task grants the execute-only Slack read capability: exactly the tools
`slack_read_thread` and `slack_read_channel` on the MCP server registered as
`slack`. The server may still be loading when the session starts: before
declaring the tools absent, check that those exact tool names are available,
and if they are not, call `WaitForMcpServers` with `servers: ["slack"]`. A
successful wait — like any `mcp` metadata — is presence evidence only, never
proof of authentication or of a read; only a successful permitted read call
is. If after the wait the tools are absent, named differently, or the server
needs authentication, stop and report that exact state — do not substitute
another tool, widen the grant, or claim a read happened. The capability is
not task authority: read only the channel or thread the coordinator's task
names.
"""
HOST_MEMORY_READONLY_NOTE = """\
## Host memory is read-only

Never create, update, or delete host memory, including the user or project
memory stores maintained by Claude Code. This does not prohibit task-authorized
edits to repository configuration files that are not memory stores. This is a same-user instruction and control, not an
operating-system sandbox: it exists so no lane worker silently changes shared
host state, and your own manual tools are not technically barred from that
path. A durable fact this task produces belongs in a reviewed repository
artifact in your lane worktree — the report, findings, or run artefact the task
names — which the coordinator can verify; never in host memory. Explicit
repository context (`AGENTS.md`, the authoritative `CLAUDE.md`, canonical
governance, delivered skills, and approved MCP tools) is unaffected and stays
available.
"""
EXECUTE_ROLE_INSTRUCTION = """\


# Delegated execute role

This is an already-approved delegated execute task. Begin the assigned scope
immediately: do not invoke coordinator planning or routing approval gates, ask
whether to proceed, or stop at a design for the same approved scope. First read
the assigned repository rules and verify the real worktree and source paths,
then carry out the requested implementation or report. Useful research and
design analysis remain allowed when they serve the task. Honor explicit
review-only, report-only, read-only, and no-commit instructions, and stop and
report genuine authority, credential, or scope blockers rather than requesting
approval.
"""


def _run_config_mcp_instruction(server_names: "Sequence[str]") -> str:
    """Wait instruction for per-run-delivered MCP servers (`--mcp-config`)."""

    listed = ", ".join(f"`{name}`" for name in server_names)
    quoted = json.dumps(list(server_names))
    return f"""


# Claude Code per-run MCP startup

This run delivered MCP server registration(s) {listed} through the
coordinator's validated run config; only the exact per-tool allowlist granted
by this lane's capabilities may be called — never enable or call a tool
outside it. A fresh registration can still be loading when the session
starts: before declaring the capability unavailable, check whether the
granted `mcp__<server>__` tools have appeared, and if they have not, call
`WaitForMcpServers` with `servers: {quoted}`. A successful wait — like any
`mcp` metadata — is presence evidence only, never proof of authentication or
of a read; only a successful permitted tool call is. If after the wait the
granted tools are absent, named differently, or the server reports an
authentication failure, stop and report that exact state — do not substitute
another tool, widen the grant, or claim a read happened.
"""


def _cm_services_startup_instruction(capabilities: Capabilities) -> str:
    """Render the ``cm-services`` wait instruction once, for any of its grants.

    Every cm-services-family capability shares the one fixed user-global
    server; the instruction is emitted once no matter how many of them are
    granted and names only the tools the granted capabilities actually allow.
    The granted set is intersected with ``CM_SERVICES_CAPABILITIES`` exactly:
    a lane granted only a non-cm-services capability (``gitnexus``,
    ``codegraph``, ``playwright``, ``slack-read``) shares the same
    ``USER_SCOPE_MCP_CAPABILITIES`` membership but has no cm-services grant, so
    it must receive no instruction at all rather than one naming an empty tool
    list and a server it was never granted.
    """

    granted = set(capabilities) & CM_SERVICES_CAPABILITIES
    if not granted:
        return ""
    policy = tool_policy()
    tools = sorted(
        rule for name in granted for rule in policy.allowed.get(name, ())
        if rule.startswith("mcp__cm-services__")
    )
    listed = ", ".join(f"`{tool}`" for tool in tools)
    gateway_note = (
        "\nThe server also restricts the Gateway tools to the exact run IDs in "
        "the coordinator's `gateway_run_ids` grant file; a call naming any other "
        "run fails closed, and neither tool accepts a URL, token, or other "
        "credential argument — never pass one."
        if "gateway-read" in granted else ""
    )
    return f"""


# Claude Code cm-services startup

This task grants exactly the tool(s) {listed} on the MCP server registered
exactly as `cm-services` — a fixed local stdio server provisioned
user-globally for the worker account before this run. The server may still
be loading when the session starts: before declaring the tools absent,
check that those exact tool names are available, and if they are not, call
`WaitForMcpServers` with `servers: ["cm-services"]`. A successful wait —
like any `mcp` metadata — is presence evidence only, never proof of
authentication or of a read; only a successful permitted tool call is. If
after the wait the granted tools are absent, named differently, or the
server reports an authentication failure, stop and report that exact state
— do not substitute another tool, widen the grant, or claim a read
happened. Each capability grants only its own tools on this shared server:
granting one never grants another's tools.{gateway_note} The capability is
not task authority: read only the objects the coordinator's task names.
"""


def _graph_startup_instruction(capabilities: Capabilities) -> str:
    """Render the code-graph wait instruction for exactly the granted servers."""

    servers = [
        CAPABILITY_MCP_SERVERS[name]
        for name in ("gitnexus", "codegraph")
        if name in set(capabilities)
    ]
    if not servers:
        return ""
    listed = ", ".join(f"`{server}`" for server in servers)
    quoted = json.dumps(servers)
    return f"""


# Claude Code code-graph startup

This task grants the code-graph capability for exactly the MCP server(s)
registered as {listed}. A fresh registration can still be loading when the
session starts: before declaring the capability unavailable, check whether
the granted `mcp__<server>__` tools have appeared, and if they have not, call
`WaitForMcpServers` with `servers: {quoted}`. Only the exact per-tool
allowlist is granted; never enable or call tools outside it. A successful
wait is tool readiness, not freshness evidence: follow the governance
requirement to check the indexed path, branch, and commit against the lane
worktree HEAD before treating graph output as current. If the wait reports a
server failed, is disabled, or remains pending, stop and report that exact
state instead of declaring the capability missing.
"""


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


def allowed_tools(mode: str, capabilities: Capabilities = (),
                  web_domains: Sequence[str] = ()) -> tuple[str, ...]:
    """Deterministic ``--allowedTools`` rules rendered from canonical governance.

    Capability names are validated first and unknown names raise in every
    mode; review mode then returns an empty tuple. The rule text comes from the
    ``Execute tool allowlist`` section of ``config/lane-governance.md``.

    ``web_domains`` is the one coordinator-supplied grant that is not a static
    allowlist rule: a documentation origin is named per run, so it renders as
    one ``WebFetch(domain:<host>)`` rule per exact host and never as a bare
    ``WebFetch`` (every domain). It is not unlocked by any capability, so
    ``shell``/``workspace-write``/``git-push`` never widen into network reach,
    and ``web_domains.claude_rule`` re-validates each host here, so a synthetic
    direct call fails closed instead of emitting a wider rule.
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
    for rule in claude_rules(web_domains):
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
    return scrub_backend_environment({
        name: value
        for name, value in inherited.items()
        if name not in SCRUB_EXACT and not name.startswith(SCRUB_PREFIXES)
    })


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write one 0600 JSON artifact inside a config directory."""

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", delete=False
    ) as temporary:
        json.dump(payload, temporary, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


def _merge_read_pagination_hook(settings: object, command: str) -> dict[str, Any]:
    """Return copied Claude settings with the Read pagination hook appended.

    Mirrors the Devin adapter's ``_merge_policy_hook``: every inherited
    ``PreToolUse`` entry survives alongside the new one, and a malformed
    ``hooks`` block fails closed at launch preparation.
    """

    if not isinstance(settings, Mapping):
        raise ClaudeAdapterError("Claude settings must be a JSON object")
    merged: dict[str, Any] = {key: value for key, value in settings.items()}
    hooks = merged.get("hooks")
    if hooks is None:
        merged_hooks: dict[str, Any] = {}
    elif isinstance(hooks, Mapping):
        merged_hooks = {key: value for key, value in hooks.items()}
    else:
        raise ClaudeAdapterError("Claude settings hooks must be a JSON object")
    existing = merged_hooks.get("PreToolUse", [])
    if not isinstance(existing, list):
        raise ClaudeAdapterError("Claude settings hooks.PreToolUse must be an array")
    merged_hooks["PreToolUse"] = [*existing, {
        "matcher": routed_read_pagination.hook_matcher(),
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }]
    merged["hooks"] = merged_hooks
    return merged


def _disable_routed_coordinator_plugin(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Copy settings while disabling only the known routed-worker plugin.

    The source user settings remain byte-for-byte untouched. An absent plugin
    map stays absent, and an existing false entry stays false; malformed maps
    fail closed instead of silently changing the worker's hook set.
    """

    merged = dict(settings)
    if "enabledPlugins" not in merged:
        return merged
    enabled_plugins = merged["enabledPlugins"]
    if not isinstance(enabled_plugins, Mapping):
        raise ClaudeAdapterError("Claude settings enabledPlugins must be a JSON object")
    copied_plugins = dict(enabled_plugins)
    for plugin, enabled in copied_plugins.items():
        if not isinstance(plugin, str) or type(enabled) is not bool:
            raise ClaudeAdapterError(
                "Claude settings enabledPlugins entries must map plugin names to booleans")
    if ROUTED_COORDINATOR_PLUGIN in copied_plugins:
        copied_plugins[ROUTED_COORDINATOR_PLUGIN] = False
    merged["enabledPlugins"] = copied_plugins
    return merged


def _prepare_routed_claude_home(
    source_home: Path, repo_path: Path, worktree_path: Path
) -> Path:
    """Create a disposable Claude config dir with exact project trust.

    Routed execute runs keep the original HOME so host tools retain their
    credentials and global skills; ``CLAUDE_CONFIG_DIR`` isolates Claude's
    trust, MCP registrations, and settings.
    """
    runtime_home = Path(tempfile.mkdtemp(prefix=".side-lane-claude-config-", dir=worktree_path.parent))
    try:
        source_config = source_home / ".claude.json"
        config: dict[str, Any] = {}
        if source_config.exists():
            try:
                loaded = json.loads(source_config.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClaudeAdapterError("Claude MCP config is invalid JSON") from exc
            if not isinstance(loaded, dict):
                raise ClaudeAdapterError("Claude MCP config must be a JSON object")
            if "mcpServers" in loaded:
                if not isinstance(loaded["mcpServers"], dict):
                    raise ClaudeAdapterError("Claude MCP registrations must be an object")
                config["mcpServers"] = loaded["mcpServers"]
        config["projects"] = {
            str(path.resolve()): {"hasTrustDialogAccepted": True}
            for path in (repo_path, worktree_path)
        }
        _write_private_json(runtime_home / ".claude.json", config)
        # Bound each native Read call to a line range through a per-run
        # PreToolUse hook. The hook config and the merged settings live in
        # this disposable config dir — never in the symlinked source hooks
        # directory — so the real user home is untouched.
        hook_config_path = runtime_home / "routed-read-pagination.json"
        _write_private_json(
            hook_config_path, {"limit": routed_read_pagination.DEFAULT_LINE_BOUND})
        hook_command = shlex.join((
            sys.executable,
            str(Path(routed_read_pagination.__file__).resolve()),
            str(hook_config_path),
        ))
        source_claude_dir = source_home / ".claude"
        source_settings = source_claude_dir / "settings.json"
        inherited_settings: object = {}
        if source_settings.is_file():
            try:
                inherited_settings = json.loads(
                    source_settings.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClaudeAdapterError("Claude settings are invalid JSON") from exc
        merged_settings = _merge_read_pagination_hook(inherited_settings, hook_command)
        _write_private_json(
            runtime_home / "settings.json",
            _disable_routed_coordinator_plugin(merged_settings),
        )
        source_local = source_claude_dir / "settings.local.json"
        if source_local.is_file():
            target_local = runtime_home / "settings.local.json"
            shutil.copyfile(source_local, target_local)
            os.chmod(target_local, 0o600)
        # Claude resolves global instructions and skills relative to its
        # configuration home. Preserve same-user context and installed plugins
        # through shared links, including plugin bookkeeping. This is not a
        # sandbox: only the trust/MCP config and copied settings are isolated.
        # Top-level credentials and OAuth state are not copied.
        for name in ("CLAUDE.md", "skills", "agents", "plugins", "commands", "output-styles", "hooks"):
            source_resource = source_claude_dir / name
            if source_resource.exists() or source_resource.is_symlink():
                os.symlink(source_resource, runtime_home / name)
        return runtime_home
    except Exception:
        shutil.rmtree(runtime_home, ignore_errors=True)
        raise


def validate_routing_policy_contract(
    provider: str, model: str, model_config: Mapping[str, Any]
) -> frozenset[str]:
    """Validate an explicit routed-provider contract and return its upstream set.

    Distinct from the exact-model ``identity_contract``: a routed provider
    (OmniRoute-style) forwards the requested selector to an upstream pool it
    selects itself, so run-time honesty is bounded by a declared, explicit,
    non-empty allowlist of upstream model identities that the streamed
    attestation must fall inside. A bare attested model id is *model*
    evidence only — ids can collide across upstream providers — so the
    returned set never licenses a provider-identity claim; upstream provider
    attribution stays a router-side audit join (usage rows keyed by the
    router key id), never a receipt inference.
    """

    if provider not in ROUTED_PROVIDERS:
        raise ClaudeAdapterError(f"provider {provider!r} does not use routing policy contracts")
    policy = model_config.get(ROUTING_POLICY_CONTRACT_KEY)
    if not isinstance(policy, Mapping):
        raise ClaudeAdapterError("routed provider lacks an explicit routing policy contract")
    if policy.get("requested_selector") != model:
        raise ClaudeAdapterError("routed route has unresolved selector identity")
    if policy.get("settings_precedence") != "verified":
        raise ClaudeAdapterError("routed route has unverified Claude settings precedence")
    upstream = policy.get("allowed_upstream_models")
    if (
        isinstance(upstream, (str, bytes))
        or not isinstance(upstream, (list, tuple))
        or not upstream
        or not all(isinstance(item, str) and item.strip() for item in upstream)
    ):
        raise ClaudeAdapterError(
            "routing policy contract requires an explicit non-empty upstream model set"
        )
    allowed = frozenset(item.strip() for item in upstream)
    if len(allowed) != len(upstream):
        raise ClaudeAdapterError("routing policy contract upstream model set has duplicates")
    return allowed


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
        if provider in ROUTED_PROVIDERS:
            # A routed provider is separately named and separately validated:
            # it must not borrow the exact-model identity contract, and the
            # routing policy contract bounds every attested upstream model.
            if gateway != ROUTED_GATEWAYS[provider]:
                raise ClaudeAdapterError(
                    f"routed provider must use the {ROUTED_GATEWAYS[provider]!r} gateway"
                )
            if isinstance(model_config.get("identity_contract"), Mapping):
                raise ClaudeAdapterError(
                    "routed provider must not claim an exact model identity contract"
                )
            validate_routing_policy_contract(provider, model, model_config)
        elif provider != "glm":
            if isinstance(model_config.get(ROUTING_POLICY_CONTRACT_KEY), Mapping):
                raise ClaudeAdapterError(
                    "routing policy contracts are reserved for routed providers"
                )
            # GLM predates the identity contract. Every newly configured direct
            # provider must prove that Claude Code settings cannot substitute an
            # alias, fast model, or subagent model behind the requested selector.
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
    runtime_model, gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    child = scrub_environment(inherited)
    # Forced after scrubbing, for every provider and both modes, so an
    # inherited `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0` in the caller's environment
    # cannot override it: the documented switch is read as truthy, and the
    # child always gets the explicit true.
    child[AUTO_MEMORY_DISABLE_ENV] = AUTO_MEMORY_DISABLED_VALUE
    if auth_method == "oauth":
        if secret is not None:
            raise ClaudeAdapterError("native OAuth routes must not receive an API key")
        return child
    if not billable or not isinstance(secret, str) or not secret:
        raise ClaudeAdapterError("explicit billable route credential is absent")
    if gateway == FIRST_PARTY_API_KEY_GATEWAY:
        # First-party Anthropic key auth travels as X-Api-Key, not a bearer
        # token; see the comment on FIRST_PARTY_API_KEY_GATEWAY above.
        child["ANTHROPIC_API_KEY"] = secret
    else:
        child["ANTHROPIC_AUTH_TOKEN"] = secret
    child["ANTHROPIC_BASE_URL"] = _nonempty(provider_config.get("base_url"), "base_url").rstrip("/")
    for name in EXACT_MODEL_ENV_NAMES:
        child[name] = runtime_model
    return child


def _approved_mcp_servers(capabilities: Capabilities) -> tuple[str, ...]:
    """Exact project MCP server names a granted capability approves.

    Every capability with a known project server approves that one server —
    nothing else — for this process. Capabilities whose registration arrives
    per run (``RUN_CONFIG_CAPABILITIES``) or lives at user scope in the
    controlled HOME (``USER_SCOPE_MCP_CAPABILITIES``) are excluded: neither
    is a project ``.mcp.json`` entry. User- and server-scope MCP grants need
    no approval and stay untouched.
    """

    return tuple(sorted({CAPABILITY_MCP_SERVERS[name] for name in capabilities
                         if name in CAPABILITY_MCP_SERVERS
                         and name not in RUN_CONFIG_CAPABILITIES
                         and name not in CM_SERVICES_CAPABILITIES}))


def _required_mcp_servers(capabilities: Capabilities) -> tuple[str, ...]:
    """Exact server names that additionally require the pre-launch probe.

    User-scope servers (``cm-services``) are probed by name like project
    servers — ``mcp get`` resolves every scope — but never receive a
    project-server approval in the probe's settings. Per-run servers are
    excluded: the probe runs before their registration is loaded and cannot
    see it. The result is deduplicated, so the two capabilities sharing
    ``cm-services`` pay for one probe.
    """

    return tuple(sorted({CAPABILITY_MCP_SERVERS[name]
                         for name in set(capabilities) & READINESS_REQUIRED_CAPABILITIES
                         if name in CAPABILITY_MCP_SERVERS
                         and name not in RUN_CONFIG_CAPABILITIES}))


def _merge_stop_hook(settings: object, command: str) -> dict[str, Any]:
    """Return copied Claude settings with the report Stop hook appended.

    Mirrors ``_merge_read_pagination_hook``: every inherited key survives and a
    malformed ``hooks`` block fails closed at launch preparation. Only the
    ``Stop`` key is touched, so an inherited ``PreToolUse`` entry — or any
    other event — keeps running exactly as before.
    """

    if not isinstance(settings, Mapping):
        raise ClaudeAdapterError("Claude settings must be a JSON object")
    merged: dict[str, Any] = {key: value for key, value in settings.items()}
    hooks = merged.get("hooks")
    if hooks is None:
        merged_hooks: dict[str, Any] = {}
    elif isinstance(hooks, Mapping):
        merged_hooks = {key: value for key, value in hooks.items()}
    else:
        raise ClaudeAdapterError("Claude settings hooks must be a JSON object")
    existing = merged_hooks.get("Stop", [])
    if not isinstance(existing, list):
        raise ClaudeAdapterError("Claude settings hooks.Stop must be an array")
    merged_hooks["Stop"] = [*existing, {
        "hooks": [{
            "type": "command",
            "command": command,
            "timeout": REPORT_ONLY_HOOK_TIMEOUT_SECONDS,
        }],
    }]
    merged["hooks"] = merged_hooks
    return merged


def report_only_hook_command(baseline: report_stop_hook.ReportBaseline) -> str:
    """Shell-quote the trusted helper invocation that guards one run's stop.

    The command is built from the current interpreter, this package's own
    helper module, and the run baseline the runner captured — never from model
    input or hook stdin. ``shlex.join`` quotes it so a worktree path containing
    spaces, quotes, or shell metacharacters survives as one argv.
    """

    helper = Path(report_stop_hook.__file__).resolve()
    settings = json.dumps(
        report_stop_hook.hook_settings(baseline), separators=(",", ":"), sort_keys=True
    )
    return shlex.join((sys.executable, str(helper), "--settings", settings))


def report_only_report_path(worktree: Path) -> Path:
    """The one report path a report-only lane is judged on."""

    return worktree / REPORT_ONLY_REPORT_NAME


def _report_only_baseline(
    worktree: Path, baseline: report_stop_hook.ReportBaseline | None
) -> report_stop_hook.ReportBaseline:
    """The run baseline the Stop hook is armed with.

    The runner captures this before the worker starts and passes it down, so
    the hook and the runner's post-run acceptance compare against the same
    recorded state. A direct caller that omits it captures here instead, which
    is still before any process starts and therefore the same rule.

    An unsafe preexisting path fails closed before a model runs: no honest
    baseline can be taken from a symlink, FIFO, socket, device, or directory,
    and silently treating the path as empty would let such an entry stand in
    for a delivery.
    """
    if baseline is not None:
        return baseline
    try:
        return report_stop_hook.capture_report_baseline(
            report_only_report_path(worktree)
        )
    except report_stop_hook.UnsafeReportPath as exc:
        raise ClaudeAdapterError(f"report-only cannot start: {exc}") from exc


def require_report_only_budget(model_config: Mapping[str, Any]) -> str:
    """Return the finite positive USD cap a report-only lane must carry.

    The opt-in promises the Stop hook and the spend cap travel in the same
    invocation, so a missing or unusable ``max_budget_usd`` fails before any
    model starts rather than launching an unbounded lane. Nothing here changes
    the catalog or the ordinary execute path, which still treats the budget as
    optional.
    """

    budget = _optional_budget(model_config)
    if budget is None:
        raise ClaudeAdapterError(
            "report-only requires a finite positive max_budget_usd: the Stop "
            "hook and the USD cap must be part of the same invocation"
        )
    return budget


def _per_launch_settings(
    provider: str,
    model: str,
    mode: str,
    base_url: str | None = None,
    capabilities: Capabilities = (),
    report_only_baseline: report_stop_hook.ReportBaseline | None = None,
) -> str:
    """Return nonsecret settings that outrank user/project/local settings.

    Execute lanes intentionally run in the user workspace under the user
    identity.  The setting is process-local and cannot change a user's saved
    Claude settings.  Direct compatible transports also pin every documented
    model selector here because Kimi's settings environment overrides shell
    environment variables.
    """

    settings: dict[str, Any] = {"sandbox": {"enabled": False}} if mode == "execute" else {}
    approved_servers = _approved_mcp_servers(capabilities)
    if mode == "execute" and approved_servers:
        # A capability grant is also explicit approval for the matching
        # project-scoped MCP server for this one process. Claude otherwise
        # leaves a fresh worktree's .mcp.json entry pending and omits its
        # tools. Only granted server names appear here — never all project
        # servers — and inherited user/server-scope grants are unaffected.
        # This is approval only; the expensive readiness probe is separate.
        settings["enabledMcpjsonServers"] = list(approved_servers)
    if provider != NATIVE_PROVIDER:
        settings["env"] = {name: model for name in EXACT_MODEL_ENV_NAMES}
        if base_url is not None:
            settings["env"]["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
        settings["alwaysThinkingEnabled"] = True
    if report_only_baseline is not None:
        # Process-local only: this hook rides the run's own --settings payload,
        # so no saved settings file, user hook, or permission is written,
        # replaced, or disabled. Inherited hooks keep loading from their own
        # setting sources alongside this one. The run baseline travels in the
        # same payload, so the hook judges the report against the state this
        # run started from rather than accepting a file it inherited.
        settings = _merge_stop_hook(
            settings, report_only_hook_command(report_only_baseline))
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
    if isinstance(value, bool):
        # `true` is not a USD amount, and float(True) == 1.0 would silently
        # become a one-dollar cap instead of the input error it is.
        raise ClaudeAdapterError("max_budget_usd must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ClaudeAdapterError("max_budget_usd must be a positive finite number") from exc
    if not parsed > 0 or parsed == float("inf"):
        raise ClaudeAdapterError("max_budget_usd must be a positive finite number")
    return str(parsed)


def _routed_autocompact_window(model_config: Mapping[str, Any]) -> str:
    """Return the routed execute lane's ``--autocompact`` operand.

    ``autocompact_window_tokens`` is an operator execution budget for the
    routed lane only: it says how much conversation the CLI may carry before it
    compacts, and claims nothing about an upstream vendor's context capacity.
    It is read here and nowhere else, so the native Claude, direct-provider,
    and review argv stay exactly as they were. Absent or null, the routed
    default is unchanged; a non-null value must be a whole number of tokens inside
    the domain the installed CLI accepts — a bool, float, string, or
    out-of-domain integer is an input error and is rejected before any
    provider starts rather than silently coerced or defaulted.
    """

    value = model_config.get(AUTOCOMPACT_WINDOW_TOKENS_KEY)
    if value is None:
        return ROUTED_AUTOCOMPACT_WINDOW
    if isinstance(value, bool) or not isinstance(value, int):
        # ``True`` is not a token count even though ``isinstance(True, int)``
        # holds, and a float or numeric string is rejected rather than coerced:
        # a rounded or half-parsed window would silently become a different
        # budget than the operator configured.
        raise ClaudeAdapterError(
            "autocompact_window_tokens must be a whole number of tokens"
        )
    if not AUTOCOMPACT_WINDOW_TOKENS_MIN <= value <= AUTOCOMPACT_WINDOW_TOKENS_MAX:
        raise ClaudeAdapterError(
            "autocompact_window_tokens must be between "
            f"{AUTOCOMPACT_WINDOW_TOKENS_MIN} and {AUTOCOMPACT_WINDOW_TOKENS_MAX}"
        )
    return str(value)


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
    read_roots: Sequence[Path] = (),
    web_domains: Sequence[str] = (),
    mcp_config_path: str | Path | None = None,
    run_mcp_servers: "Mapping[str, Any] | None" = None,
    report_only: bool = False,
    report_baseline: report_stop_hook.ReportBaseline | None = None,
    strict_mcp_config_path: str | Path | None = None,
    strict_mcp_support: bool | None = None,
) -> list[str]:
    executable = _nonempty(executable, "Claude executable")
    repo_path = validate_worktree(repo)
    worktree_path = validate_worktree(worktree)
    if repo_path == worktree_path:
        raise ClaudeAdapterError(f"{mode} lane requires a dedicated worktree")
    if report_only and mode != "execute":
        # The hook rides the execute lane's own argv and settings; a review
        # lane's argv is the strict read-only form and must stay byte-identical.
        raise ClaudeAdapterError("report-only is execute mode only for the Claude host")
    armed_baseline: report_stop_hook.ReportBaseline | None = None
    if report_only:
        # Fail before the model, not after: the opt-in only makes sense if the
        # Stop hook and the USD cap are both in this one command.
        require_report_only_budget(model_config)
        # Captured before the worker exists, so the hook can tell this run's
        # report from one the worktree already contained at HEAD.
        armed_baseline = _report_only_baseline(worktree_path, report_baseline)
    if read_roots and mode != "execute":
        # A review lane's argv is the strict read-only form; a granted read
        # root cannot be added to it without either assuming the plan-mode
        # tool list is enforcing or widening the workspace grant
        # (`--add-dir` is a workspace grant, not a read grant). Fail closed
        # rather than launch a lane that silently ignores the coordinator.
        raise ClaudeAdapterError("read roots are execute-only for the Claude host")
    if web_domains and mode != "execute":
        # A review lane's argv is the strict read-only form (`--safe-mode`,
        # `--tools Read,Glob,Grep`, strict no-MCP): it has no WebFetch tool at
        # all, so a documentation-domain grant there is inert authority. Fail
        # closed rather than launch a lane whose stated scope the worker
        # cannot be given.
        raise ClaudeAdapterError("web domains are execute-only for the Claude host")
    if mcp_config_path is not None and mode != "execute":
        # Review mode is strict no-MCP by canonical governance: its argv pins
        # `--strict-mcp-config` with an empty server set, and no per-run
        # registration may widen that. The CLI rejects `--mcp-config` in
        # review mode already; this is the adapter-level fail-closed guard.
        raise ClaudeAdapterError("per-run MCP config is execute-only for the Claude host")
    if strict_mcp_config_path is not None and strict_mcp_support is not True:
        raise ClaudeAdapterError(
            "strict MCP bundle requested but CLI support for --strict-mcp-config "
            "has not been confirmed; the routed lane cannot be launched"
        )
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
                    provider, runtime_model, mode, provider_config.get("base_url"),
                    capabilities, armed_baseline,
                ),
            )
        )
        if provider in ROUTED_PROVIDERS:
            # The routed execute lane is the only surface that reads the
            # operator's window; every other provider keeps the argv it had.
            command.extend(("--autocompact", _routed_autocompact_window(model_config)))
        effort = _optional_effort(model_config)
        if effort is not None:
            command.extend(("--effort", effort))
        budget = _optional_budget(model_config)
        if budget is not None:
            command.extend(("--max-budget-usd", budget))
        if strict_mcp_config_path is not None:
            # Routed execute lane: use --strict-mcp-config with the narrow bundle
            # to prevent loading of any inherited MCP registrations. The bundle
            # already contains the per-run servers, user-scope registrations
            # (cm-services), and lane worktree .mcp.json servers.
            command.extend(("--strict-mcp-config", f"--mcp-config={strict_mcp_config_path}"))
        elif mcp_config_path is not None:
            # Native execute lane: additive per-run MCP registration --
            # --strict-mcp-config is deliberately NOT passed, so every existing
            # user/project/server registration (and its auth) keeps loading
            # alongside this file.
            command.extend(("--mcp-config", str(mcp_config_path)))
        for tool in allowed_tools(mode, capabilities, web_domains):
            command.extend(("--allowedTools", tool))
        for tool in disallowed_tools(mode, capabilities):
            command.extend(("--disallowedTools", tool))
    system_prompt = lane_system_prompt(mode, repo_path)
    if mode == "execute" and "playwright" in _capability_set(capabilities):
        system_prompt += PLAYWRIGHT_STARTUP_INSTRUCTION
    if mode == "execute" and "slack-read" in _capability_set(capabilities):
        system_prompt += SLACK_READ_STARTUP_INSTRUCTION
    if mode == "execute" and run_mcp_servers:
        system_prompt += _run_config_mcp_instruction(sorted(run_mcp_servers))
    if mode == "execute":
        system_prompt += _graph_startup_instruction(_capability_set(capabilities))
        system_prompt += _cm_services_startup_instruction(_capability_set(capabilities))
    # An execute lane's `Read` rule is unqualified, so a read root adds no
    # reachable path here; it is named in the worker's instructions because
    # the coordinator granted it for this task and the worker should not have
    # to discover it. No `--add-dir` is emitted: it extends the workspace
    # rather than granting a read, which would make a read root writable.
    note = scope_note(read_roots)
    web_note = web_scope_note(web_domains)
    if web_note:
        note = f"{note}\n\n{web_note}" if note else web_note
    # Every Claude worker carries the host-memory rule, review or execute: it
    # is a note, so no tool list, grant, or argv flag changes with it.
    note = f"{note}\n\n{HOST_MEMORY_READONLY_NOTE}" if note else HOST_MEMORY_READONLY_NOTE
    if note:
        system_prompt += f"\n\n{note}"
    if mode == "execute":
        # Keep this last so host-native skill text or scope notes cannot turn an
        # already authorized execute lane back into a planning/approval dialogue.
        system_prompt += EXECUTE_ROLE_INSTRUCTION
    command.extend(("--append-system-prompt", system_prompt))
    return command


def _install_strict_readiness_config(
    config_dir: Path, bundle_path: Path,
) -> None:
    """Replace the disposable Claude registry with the exact routed bundle.

    Claude 2.1.278 treats ``--mcp-config`` as variadic for the ``mcp get``
    subcommand and can therefore ignore a dynamic bundle while reporting a
    user-global server as connected.  The routed config directory is already
    per-run and disposable; putting the validated bundle in its ``.claude.json``
    gives the readiness probe an exact, isolated registry without touching the
    user's config or performing a model call.
    """
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeAdapterError("strict MCP bundle is not readable JSON") from exc
    if not isinstance(bundle, dict) or not isinstance(bundle.get("mcpServers"), dict):
        raise ClaudeAdapterError("strict MCP bundle must contain an mcpServers object")
    config_path = config_dir / ".claude.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeAdapterError("disposable Claude config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ClaudeAdapterError("disposable Claude config must be an object")
    config["mcpServers"] = bundle["mcpServers"]
    _write_private_json(config_path, config)


def _require_mcp_readiness(
    *, executable: str, cwd: Path, capabilities: Capabilities,
    env: Mapping[str, str], runner: Runner, secret: str | None = None,
    strict_mcp_config_path: Path | None = None,
) -> None:
    """Health-check capability-required MCP servers before starting a model.

    Probes the installed CLI with the EXACT granted strict bundle
    (``--strict-mcp-config --mcp-config <path>``) so that the readiness check
    validates the same configuration that will be active during the run.
    """

    # Probe the installed CLI once per executable to determine whether it supports
    # --strict-mcp-config.  This avoids inferring support from the flag's presence
    # in the command build (which is a contract claim, not a runtime fact).
    cli_supports_strict = _check_strict_mcp_support(
        executable, runner, cwd=cwd, env=env,
    )

    if strict_mcp_config_path is not None and not cli_supports_strict:
        raise ClaudeAdapterError(
            f"{executable} does not support --strict-mcp-config; "
            "a routed execute lane that requires the strict MCP bundle cannot launch"
        )

    readiness_env = dict(env)
    readiness_config_dir: Path | None = None
    try:
        if cli_supports_strict and strict_mcp_config_path is not None:
            # Use a private exact-bundle registry for every strict caller,
            # including direct billable providers that do not use the routed home.
            readiness_config_dir = Path(tempfile.mkdtemp(prefix=".side-lane-mcp-readiness-"))
            _install_strict_readiness_config(readiness_config_dir, strict_mcp_config_path)
            readiness_env["CLAUDE_CONFIG_DIR"] = str(readiness_config_dir)
        for server in _required_mcp_servers(capabilities):
            if cli_supports_strict and strict_mcp_config_path is not None:
                # The exact bundle has already been installed in the disposable
                # CLAUDE_CONFIG_DIR.  Do not pass --mcp-config to `mcp get`: that
                # option is variadic and the CLI may consume the subcommand as
                # additional filenames, or silently fall back to user-global state.
                command = [executable, "mcp", "get", server]
            elif server == "cm-services":
                # The fixed user-global cm-services server is not a project .mcp.json
                # entry, so it is probed by bare name across scopes.
                command = [executable, "mcp", "get", server]
            else:
                # Probe the requested server with explicit project approval.  This
                # is required for project .mcp.json servers such as playwright and
                # matches the per-launch ``enabledMcpjsonServers`` approval.
                settings = json.dumps(
                    {"enabledMcpjsonServers": [server]}, separators=(",", ":"), sort_keys=True
                )
                command = [executable, "--settings", settings, "mcp", "get", server]
            try:
                completed = runner(
                    command,
                    timeout=MCP_READINESS_TIMEOUT_SECONDS,
                    cwd=cwd,
                    env=readiness_env,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ClaudeAdapterError(f"could not check {server} MCP readiness: {_redact(exc, secret)}") from None
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
                    f"required MCP server {server!r} is not ready before worker launch ({_redact(status, secret)})"
                )
    finally:
        if readiness_config_dir is not None:
            shutil.rmtree(readiness_config_dir, ignore_errors=True)


def _redact(value: object, secret: str | None) -> str:
    return redact_provider_secret(value, secret)


def validate_against_capabilities(
    *,
    executable: str,
    cwd: Path,
    strict_mcp_config_path: Path,
    capabilities: Capabilities,
    env: Mapping[str, str],
    runner: Runner,
    secret: str | None = None,
) -> None:
    """Verify bundle servers through a private exact registry snapshot.

    This helper is retained for callers that validate a bundle before launch;
    it deliberately does not pass ``--mcp-config`` to ``mcp get`` because the
    CLI option is variadic and may ignore the dynamic file.
    """
    if not strict_mcp_config_path.exists():
        return
    readiness_dir = Path(tempfile.mkdtemp(prefix=".side-lane-mcp-validate-"))
    try:
        _install_strict_readiness_config(readiness_dir, strict_mcp_config_path)
        bundle = json.loads(strict_mcp_config_path.read_text(encoding="utf-8"))
        servers = bundle.get("mcpServers") or {}
        if not isinstance(servers, dict):
            raise ClaudeAdapterError("strict MCP bundle must contain an mcpServers object")
        probe_env = dict(env)
        probe_env["CLAUDE_CONFIG_DIR"] = str(readiness_dir)
        for server_name in servers:
            result = runner(
                [executable, "mcp", "get", server_name],
                cwd=str(cwd), env=probe_env, secret=secret,
            )
            stdout = str(getattr(result, "stdout", "") or "")
            connected = any(
                re.search(r"^\s*Status:\s*[^\w]*Connected\s*$", ANSI_ESCAPE.sub("", line), re.I)
                for line in stdout.splitlines()
            )
            if result.returncode != 0 or not connected:
                raise ClaudeAdapterError(
                    f"required MCP server {server_name!r} is not available: "
                    f"{_redact(stdout.strip(), secret)!r}"
                )
    finally:
        shutil.rmtree(readiness_dir, ignore_errors=True)


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
    read_roots: Sequence[Path] = (),
    web_domains: Sequence[str] = (),
    run_mcp_servers: "Mapping[str, McpRunServer] | None" = None,
    report_only: bool = False,
    report_baseline: report_stop_hook.ReportBaseline | None = None,
) -> LaneResult:
    if web_domains and mode != "execute":
        # Adapter-level fail-closed mirror of the build_command guard: `launch`
        # may be called without going through that guard's inputs.
        raise ClaudeAdapterError("web domains are execute-only for the Claude host")
    if provider != NATIVE_PROVIDER and provider != "glm":
        qualification = model_config.get("qualification")
        if not isinstance(qualification, Mapping) or qualification.get("verified") is not True:
            raise ClaudeAdapterError("external route lacks verified model transport qualification")
        _nonempty(qualification.get("verified_on"), "qualification.verified_on")
        _nonempty(qualification.get("source"), "qualification.source")
    repo_path = validate_worktree(repo)
    worktree_path = validate_worktree(worktree)
    granted = tuple(sorted(_capability_set(capabilities)))
    # Per-run MCP registration (execute only, validated upstream by
    # side_lane.mcp_run_config): materialized OUTSIDE the lane worktree so it
    # can never appear in the delivery check, and removed after the run —
    # ephemeral by construction, never a user-global config write.
    if run_mcp_servers:
        if mode != "execute":
            raise ClaudeAdapterError("per-run MCP config is execute-only for the Claude host")
        # A same-name user/project registration would make the merge outcome
        # (which server, whose auth) undefined from config alone; fail before
        # the ephemeral file is written or any process starts. The credential
        # env-reference check runs later, inside ``_launch_worker``, against
        # the scrubbed child environment the worker actually gets.
        ensure_no_registration_conflicts(
            run_mcp_servers, "claude", worktree_path, env=os.environ if env is None else env
        )
    # Routed execute lanes use --strict-mcp-config with a narrow bundle that
    # contains ONLY per-run servers, user-scope registrations (cm-services),
    # and lane worktree .mcp.json servers. No other host-registered servers
    # appear in the bundle, and none are loaded.
    is_routed_execute = (
        provider != NATIVE_PROVIDER
        and mode == "execute"
    )
    # Artifact paths: initialised to None; assigned inside the try block;
    # cleaned up in the finally block. All paths are None if no artifact needed.
    run_config_path: Path | None = None
    strict_mcp_config_path: Path | None = None
    try:
        if run_mcp_servers:
            runtime_dir = worktree_path.parent / ".side-lane-runtime" / "mcp"
            run_config_path = write_ephemeral(
                runtime_dir, f"{worktree_path.name}-mcp.json",
                claude_payload(run_mcp_servers),
            )
        if is_routed_execute:
            controlled_home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
            strict_bundle = build_strict_mcp_bundle(
                host="claude",
                repo=repo_path,
                worktree=worktree_path,
                home=controlled_home,
                run_servers=run_mcp_servers,
                granted_capabilities=capabilities,
            )
            runtime_dir = worktree_path.parent / ".side-lane-runtime" / "mcp"
            strict_mcp_config_path = write_strict_mcp_bundle(
                runtime_dir, worktree_path.name, strict_bundle,
            )
        return _launch_worker(
            executable=executable,
            repo_path=repo_path,
            worktree_path=worktree_path,
            provider=provider,
            model=model,
            provider_config=provider_config,
            model_config=model_config,
            prompt=prompt,
            mode=mode,
            granted=granted,
            env=env,
            secret=secret,
            runner=runner,
            readiness_runner=readiness_runner,
            read_roots=read_roots,
            web_domains=web_domains,
            run_mcp_servers=run_mcp_servers,
            run_config_path=run_config_path,
            report_only=report_only,
            report_baseline=report_baseline,
            strict_mcp_config_path=strict_mcp_config_path,
        )
    finally:
        if run_config_path is not None:
            with suppress(OSError):
                run_config_path.unlink()
            with suppress(OSError):
                run_config_path.parent.rmdir()
        if strict_mcp_config_path is not None:
            with suppress(OSError):
                strict_mcp_config_path.unlink()
            with suppress(OSError):
                strict_mcp_config_path.parent.rmdir()


def _launch_worker(
    *,
    executable: str,
    repo_path: Path,
    worktree_path: Path,
    provider: str,
    model: str,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    prompt: str,
    mode: str,
    granted: tuple[str, ...],
    env: Mapping[str, str] | None,
    secret: str | None,
    runner: Runner,
    readiness_runner: Runner,
    read_roots: Sequence[Path],
    web_domains: Sequence[str],
    run_mcp_servers: "Mapping[str, McpRunServer] | None",
    run_config_path: Path | None,
    report_only: bool = False,
    report_baseline: report_stop_hook.ReportBaseline | None = None,
    strict_mcp_config_path: Path | None = None,
) -> LaneResult:
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
    routed_config_dir: Path | None = None
    try:
        if provider in ROUTED_PROVIDERS and mode == "execute":
            source_home = Path(child_env.get("HOME", str(Path.home()))).expanduser()
            routed_config_dir = _prepare_routed_claude_home(source_home, repo_path, worktree_path)
            child_env["CLAUDE_CONFIG_DIR"] = str(routed_config_dir)
            child_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = ROUTED_MAX_OUTPUT_TOKENS
        if run_mcp_servers:
            # Validate the credential env NAME the config references against the
            # scrubbed environment the worker child actually gets.
            require_env_references(run_mcp_servers, child_env)
        timeout = model_config.get("timeout_seconds", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ClaudeAdapterError("timeout_seconds must be a positive integer")
        active_runner = _bounded_process if runner is None else runner
        active_readiness_runner = _bounded_process if readiness_runner is None else readiness_runner
        if strict_mcp_config_path is not None:
            strict_mcp_supported = _check_strict_mcp_support(
                executable, active_readiness_runner, cwd=worktree_path, env=child_env,
            )
        else:
            strict_mcp_supported = False
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
            read_roots=read_roots,
            web_domains=web_domains,
            mcp_config_path=run_config_path,
            run_mcp_servers=run_mcp_servers,
            report_only=report_only,
            report_baseline=report_baseline,
            strict_mcp_config_path=strict_mcp_config_path,
            strict_mcp_support=strict_mcp_supported,
        )
        _require_mcp_readiness(
            executable=executable, cwd=worktree_path, capabilities=granted,
            env=child_env, runner=active_readiness_runner, secret=secret,
            strict_mcp_config_path=strict_mcp_config_path,
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
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClaudeAdapterError(f"could not start Claude Code: {_redact(exc, secret)}") from None
    finally:
        if routed_config_dir is not None:
            shutil.rmtree(routed_config_dir, ignore_errors=True)
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
    if int(completed.returncode) == 0 and provider in ROUTED_PROVIDERS:
        # Routed-contract attestation: the stream must attest at least one
        # actual upstream model and every attested model must fall inside the
        # declared upstream set. Multiple in-set models are legitimate (a
        # worker conversation can fall back inside the pool); anything
        # missing, unknown, or outside the pool fails closed. Claude Code can
        # echo the requested selector in stream frames (for example init); an
        # alias echo is not upstream evidence, so it is ignored rather than
        # counted for or against the route. The receipt records the attested
        # upstream set — never the selector alias as actual identity, and
        # never a provider-identity claim.
        allowed = validate_routing_policy_contract(provider, model, model_config)
        upstream_attested = attested_models - {model}
        attested_models = upstream_attested
        if len(upstream_attested) == 1:
            resolved_model = next(iter(upstream_attested))
        else:
            resolved_model = None
        if not upstream_attested:
            completed = subprocess.CompletedProcess(command, 65, stdout, stderr)
            stderr = (stderr + "\nrouting-unverified: response did not attest an upstream model").lstrip()
        else:
            outside = ", ".join(sorted(upstream_attested - allowed))
            if outside:
                completed = subprocess.CompletedProcess(command, 65, stdout, stderr)
                stderr = (stderr + f"\nrouting-violation: attested models outside the declared upstream set: {outside}").lstrip()
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
        allowed_tools=allowed_tools(mode, granted, web_domains),
        disallowed_tools=disallowed_tools(mode, granted),
        requested_model=model,
        # The transport requests this selector; it cannot attest to the
        # provider's response weight/version without a verified response field.
        resolved_model=resolved_model,
        # Observed upstream model ids exactly as the stream attested them. For
        # routed providers this set — not the selector alias — is the actual
        # model evidence; it licenses no upstream provider-identity claim.
        attested_models=tuple(sorted(attested_models)),
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
