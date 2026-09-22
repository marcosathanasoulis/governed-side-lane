"""Codex adapter with canonical governance injection.

Two gateways are accepted, both selected purely from the provider config:

- ``native-codex``: the developer's signed-in Codex OAuth session, never
  billable, review or execute. No API key may reach the child.
- ``codex-api-key``: an explicit, billable, execute-only provider-key route
  for hosts with no OAuth session (e.g. a cloud worker). The launcher-read
  secret is handed to the Codex CLI as both ``CODEX_API_KEY`` and
  ``OPENAI_API_KEY`` (plus ``OPENAI_BASE_URL`` when the provider config
  carries ``base_url``). Codex CLI 0.154.0 authenticates only from
  ``CODEX_API_KEY`` (or ``codex login --with-api-key``) and ignores
  ``OPENAI_API_KEY``; the latter is kept for older CLIs and base-URL
  gateways. Every other inherited provider credential is still scrubbed, and
  the secret is redacted from captured output.

Both routes run ``codex exec --json`` so the JSONL event stream can be read
for the final ``turn.completed`` usage block; the raw stream stays in
``stdout`` unchanged apart from secret redaction.

``timeout_seconds`` is opt-in on Codex routes: a route that does not set it
launches with no process timeout at all (the historical behaviour), while a
present value must be a positive integer and is enforced through the same
canonical bounded lifecycle the Claude adapter uses — the worker's process
group is stopped on POSIX hosts; Windows attempts an OS-native process-tree
stop and falls back to direct-child termination with bounded output drain.
The lane returns an exit-124 receipt naming the actual cleanup outcome and
any captured partial stream.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence

from side_lane.adapters.claude import _bounded_process
from side_lane.credentials import scrub_backend_environment
from side_lane.governance import lane_system_prompt, report_write_capability_conflicts
from side_lane.hosts import with_support_dir
from side_lane.mcp_run_config import (
    McpRunServer,
    codex_overrides,
    ensure_no_registration_conflicts,
    require_env_references,
    startup_note,
)
from side_lane.read_roots import scope_note
from side_lane.results import LaneResult


NATIVE_PROVIDER = "openai"
NATIVE_GATEWAY = "native-codex"
API_KEY_GATEWAY = "codex-api-key"
SUPPORTED_GATEWAYS = frozenset({NATIVE_GATEWAY, API_KEY_GATEWAY})
NATIVE_PROTOCOLS = frozenset({"native-codex", "native-codex-readonly"})
REDACTED_SECRET = "[REDACTED_PROVIDER_KEY]"

# ``CODEX_*`` is listed by exact name rather than prefix: ``CODEX_HOME`` is a
# config path the CLI needs, not a credential, and must survive scrubbing.
PROVIDER_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
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


def _resolve_timeout_seconds(model_config: Mapping[str, Any]) -> int | None:
    """Return the configured worker timeout, or ``None`` when unset.

    An absent ``timeout_seconds`` keeps the historical unbounded Codex launch
    — no ``timeout`` kwarg reaches the runner at all. A present value must be
    a positive integer; anything else fails closed before launch, matching
    the Claude and Devin adapters.
    """

    if "timeout_seconds" not in model_config:
        return None
    timeout = model_config["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise CodexAdapterError("timeout_seconds must be a positive integer")
    return timeout


def _timeout_stream_text(value: object) -> str:
    """Decode a ``TimeoutExpired`` stream payload the way ``text=True`` would."""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


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
    # Codex CLI 0.154.0 reads CODEX_API_KEY only; OPENAI_API_KEY is kept for
    # older CLIs and OpenAI-compatible base_url gateways.
    child["CODEX_API_KEY"] = secret
    child["OPENAI_API_KEY"] = secret
    base_url = provider_config.get("base_url")
    if isinstance(base_url, str) and base_url:
        child["OPENAI_BASE_URL"] = base_url.rstrip("/")
    return child


def _redact(value: object, secret: str | None) -> str:
    text = str(value or "")
    return text.replace(secret, REDACTED_SECRET) if secret else text


def reject_report_write_capabilities(
    capabilities: "tuple[str, ...] | list[str]", report_deliverable: bool
) -> None:
    """Refuse explicit write capabilities a report-deliverable lane must not hold.

    The CLI refuses these before it builds a lane, and `run_codex` is the only
    entry point here that carries capabilities — this host has no deny seam, so
    a capability rides the instruction text rather than a rule list. A direct
    caller therefore gets the same refusal rather than an instruction naming a
    grant the report contract removed. ``workspace-write`` — the report
    artifact's own write — and every read capability are untouched.
    """

    if not report_deliverable:
        return
    conflicts = report_write_capability_conflicts(capabilities)
    if conflicts:
        raise CodexAdapterError(
            "a report-deliverable lane is never granted "
            + ", ".join(conflicts)
            + ": its deliverable is the report artifact in the lane worktree, so "
            "it makes no push of a branch it was told not to commit and no workflow "
            "or messaging write. Drop the capability, or run an ordinary execute "
            "lane."
        )


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
    read_roots: "Sequence[Path]" = (),
    web_domains: "Sequence[str]" = (),
    run_mcp_servers: "Mapping[str, McpRunServer] | None" = None,
    report_deliverable: bool = False,
) -> tuple[str, ...]:
    if not isinstance(executable, str) or not executable:
        raise CodexAdapterError("Codex executable is required")
    if mode not in {"review", "execute"}:
        raise CodexAdapterError("mode must be review or execute")
    if report_deliverable and mode != "execute":
        # The report contract narrows the execute grant; a review lane is
        # read-only by sandbox mode and never received it.
        raise CodexAdapterError("report deliverable is execute mode only for the Codex host")
    if web_domains:
        # Refused, not silently accepted. The Codex host's execute sandbox is
        # `danger-full-access`, so this lane already reaches every destination
        # and Codex CLI exposes no per-destination network control to narrow
        # it with. Naming an approved origin in the task text would describe a
        # scope nothing enforces, and recording it in the audit would misstate
        # the lane's real network reach as a granted, bounded list. Fail closed
        # and say so instead of pretending the host implements the grant.
        raise CodexAdapterError(
            "web domains are not supported on the Codex host: an execute lane "
            "runs with danger-full-access and Codex CLI has no per-destination "
            "web-fetch permission rule, so --web-domain cannot be enforced or "
            "honestly recorded there"
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise CodexAdapterError("task prompt must be non-empty")
    if read_roots and mode != "execute":
        # A review lane is read-only by sandbox mode and its argv is the
        # strict form; Codex's only directory control (`--add-dir` /
        # `sandbox_workspace_write.writable_roots`) is a WRITE grant, so a
        # granted read root cannot be expressed there without widening.
        raise CodexAdapterError("read roots are execute-only for the Codex host")
    if run_mcp_servers and mode != "execute":
        # Review mode's argv carries `-c mcp_servers={}` — strict no-MCP by
        # canonical governance — and no per-run registration may widen it.
        raise CodexAdapterError("per-run MCP config is execute-only for the Codex host")
    repo_path = _validate_worktree(repo)
    worktree_path = _validate_worktree(worktree)
    if repo_path == worktree_path:
        raise CodexAdapterError(f"{mode} lane requires a dedicated worktree")
    runtime_model = _validate_selection(
        provider, model, provider_config, model_config, mode
    )
    # An execute lane runs `danger-full-access`, so a read root grants no new
    # reachable path here; it is named in the worker's instructions because
    # the coordinator granted it for this task. `--add-dir` is deliberately
    # NOT emitted: it grants write access, which a read root must never carry.
    note = scope_note(read_roots)
    if run_mcp_servers:
        # Codex (0.155 `codex mcp add --help`) delivers streamable-HTTP MCP
        # servers natively with the bearer referenced by env-var NAME
        # (`bearer_token_env_var`). The `-c` dotted overrides touch only the
        # one server being registered, so every server already configured in
        # $CODEX_HOME/config.toml or a project config keeps loading —
        # additive, never a replacement. No file is written and nothing
        # enters the argv except the URL and the env NAME.
        note = (note or "") + startup_note(run_mcp_servers)
    task = (lane_system_prompt(mode, repo_path, report_deliverable=report_deliverable)
            + (f"\n\n{note}" if note else "")
            + "\n\n# Approved task\n\n" + prompt)
    # No deny seam exists on this host: the argv below is `danger-full-access`
    # with no per-command permission rule, so the report instruction above is
    # delivered as instruction, never as prevention. The run's report contract
    # is the runner's after-the-fact delivery check.
    command = [
        executable,
        "exec",
        "--json",
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
    elif run_mcp_servers:
        command.extend(codex_overrides(run_mcp_servers))
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
    read_roots: "Sequence[Path]" = (),
    web_domains: "Sequence[str]" = (),
    run_mcp_servers: "Mapping[str, McpRunServer] | None" = None,
    report_deliverable: bool = False,
) -> LaneResult:
    repo_path = _validate_worktree(repo)
    worktree_path = _validate_worktree(worktree)
    _runtime_model, gateway, auth_method, billable = _route_metadata(
        provider, model, provider_config, model_config, mode
    )
    timeout = _resolve_timeout_seconds(model_config)
    if run_mcp_servers and mode != "execute":
        # Adapter-level fail-closed mirror of the build_codex_command guard;
        # run_codex may be called without going through that guard's inputs.
        raise CodexAdapterError("per-run MCP config is execute-only for the Codex host")
    if report_deliverable and mode != "execute":
        # Same mirror: the report contract narrows the execute grant, and the
        # argv for this host carries no seam that could carry it in review mode.
        raise CodexAdapterError("report deliverable is execute mode only for the Codex host")
    # Capabilities arrive only here in this adapter (`build_codex_command` takes
    # none), so this is the single boundary that can refuse them for a report
    # lane; nothing is launched past it.
    reject_report_write_capabilities(capabilities, report_deliverable)
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
        read_roots=read_roots,
        web_domains=web_domains,
        run_mcp_servers=run_mcp_servers,
        report_deliverable=report_deliverable,
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
    if run_mcp_servers:
        # Fail closed before any process starts when the bearer env NAME the
        # config references is not exported into the worker child environment.
        require_env_references(
            run_mcp_servers,
            child_env,
        )
        # A same-name ``[mcp_servers.<name>]`` entry in config.toml would be
        # silently replaced by these ``-c`` overrides (whose precedence over
        # an existing entry is not equivalence-provable); fail first.
        ensure_no_registration_conflicts(
            run_mcp_servers, "codex", worktree_path, env=child_env
        )
    run_kwargs: dict[str, Any] = {
        "cwd": worktree_path,
        "env": child_env,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "capture_output": True,
        "check": False,
    }
    active_runner = runner
    if timeout is not None:
        run_kwargs["timeout"] = timeout
        if runner is subprocess.run:
            # subprocess.run's own timeout kills only the direct child and
            # raises; the canonical bounded lifecycle stops the worker —
            # its whole process group on POSIX, its process tree where an
            # OS-native tree stop exists (Windows taskkill /T /F), the
            # process itself elsewhere — and returns a truthful exit-124
            # receipt with the partial stream instead. An explicitly
            # injected runner is still honored — it just receives the
            # timeout kwarg.
            active_runner = _bounded_process
    try:
        completed = active_runner(argv, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        if timeout is None:
            raise  # Preserve the injected runner's historical absent-timeout contract.
        # An injected runner that surfaces a raw timeout instead of the
        # bounded receipt still yields one truthful result: exit 124 with
        # the partial output it captured, redacted below like any other
        # run. No cleanup claim is made beyond what that runner did.
        completed = subprocess.CompletedProcess(
            argv,
            124,
            _timeout_stream_text(exc.output),
            _timeout_stream_text(exc.stderr) + "\nworker timed out",
        )
    except OSError as exc:
        raise CodexAdapterError(f"could not start Codex executable: {exc}") from exc
    stdout = getattr(completed, "stdout", "")
    resolved_model, usage = _stream_metadata(stdout if isinstance(stdout, str) else "")
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
        stdout=_redact(stdout, secret),
        stderr=_redact(getattr(completed, "stderr", ""), secret),
        capabilities=tuple(sorted(set(capabilities))),
        requested_model=model,
        resolved_model=resolved_model,
        usage=usage,
    )


def _stream_metadata(stdout: str) -> tuple[str | None, dict[str, Any] | None]:
    """Read the attested model and final usage from ``codex exec --json`` JSONL.

    Codex CLI 0.154 emits one JSON object per line; the last ``turn.completed``
    event carries ``usage`` (``input_tokens``, ``cached_input_tokens``,
    ``cache_write_input_tokens``, ``output_tokens``, ``reasoning_output_tokens``).
    Non-JSON lines are ignored. No cost is estimated here.
    """

    resolved_model: str | None = None
    usage: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if not isinstance(event_type, str):
            # A non-string ``type`` (list, dict, number) would make the set
            # membership test raise TypeError; treat it as an irrelevant line.
            continue
        if event_type in {"thread.started", "turn.started"}:
            model = event.get("model")
            if isinstance(model, str) and model:
                resolved_model = model
        elif event_type == "turn.completed" and isinstance(event.get("usage"), Mapping):
            usage = dict(event["usage"])
    return resolved_model, usage
