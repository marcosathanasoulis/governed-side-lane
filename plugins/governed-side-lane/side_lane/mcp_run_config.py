"""Coordinator-supplied per-run MCP server registrations for execute lanes.

The GCF worker (functions/sideLaneWorker/accounts.py, `aws-mcp` account)
already writes a worker-native registration JSON under
``<worktree_root>/.side-lane/mcp/aws-mcp.json``: a real streamable-HTTP MCP
server entry whose bearer token is referenced BY ENV NAME
(``${CLAUDE_TAG_AWS_MCP_TOKEN}``), never as a value. Until now no host loaded
that file. This module turns such a file into per-run MCP configuration for
the three host CLIs, additively — existing user/project MCP registrations,
their auth, ``CODEX_HOME``, and home settings are never replaced. Where a
host's merge precedence over a SAME-NAME existing registration is not
established (Claude's ``--mcp-config``, Codex's ``-c mcp_servers.<name>.*``
overrides), an explicit name conflict fails closed BEFORE launch
(``ensure_no_registration_conflicts``): the existing registration and its
auth are never silently overwritten or shadowed, and equivalence is never
assumed.

Delivery is capability-narrowed, not wildcarded: a run config may declare only
server names that a GRANTED capability maps to an exact name
(``CAPABILITY_MCP_SERVERS``). No ``mcp__*`` blanket grant is ever produced,
and the per-tool allowlist stays where it has always been rendered — the
canonical ``Execute tool allowlist`` in ``config/lane-governance.md``.

Credentials stay env references end to end. A header value must be exactly an
optional constant auth-scheme prefix plus one ``${ENV_NAME}`` reference, so no
literal token can enter a runtime config file, a command line, or an audit
record. Launch additionally requires every referenced env name to be present
and non-empty in the environment the host process will run with — the GCF
pipeline merges account env into the runner child after ``_base_child_env``,
and none of the three adapters' scrub lists (checked 2026-09-19) strips
``CLAUDE_TAG_AWS_MCP_TOKEN`` or any ``CLAUDE_TAG_*`` name, so the reference
resolves where the hosts resolve it without an allowlist patch.

Host support, established from each CLI's own help/source on 2026-09-19
(claude 2.1.278, codex-cli 0.155.0-alpha.2.6, devin 3000.10.21) — do NOT
assume one host's JSON conventions for another:

- Claude Code: ``--mcp-config <file>`` loads MCP servers from a JSON file
  additively (``--strict-mcp-config`` is NOT passed, so user/project servers
  survive); ``${ENV}`` expansion inside header values is documented and was
  live-verified against a local mock MCP server (initialize + tools/list
  passed bearer auth) without any model call.
- Codex: ``codex mcp add --help`` documents streamable-HTTP servers with
  ``--url`` and ``--bearer-token-env-var``, i.e. an env-referenced bearer by
  design. Delivered as additive ``-c mcp_servers.<name>.*`` config overrides
  (the review form ``-c mcp_servers={}`` is unchanged). The CLI accepts and
  enables the override shape (verified via ``codex mcp get``/``list``); a live
  connect needs a session.
- Devin: ``devin mcp add --help`` documents HTTP transport and a local project
  scope (``.devin/mcp_config.local.json``, not committed) with the same
  ``mcpServers`` container. Delivered by merging that local-scope file; the
  file shape is accepted by ``devin mcp get`` (headers are parsed and
  redacted on display). Whether Devin expands ``${ENV}`` inside header values
  is UNVERIFIED — a live initialize needs a session — so a Devin delivery can
  fail closed at the bridge (401) rather than leak anything; that state must
  be reported, never labeled a success.

Runtime artifacts are ephemeral: Claude's config file lives outside the lane
checkout under the run-local runtime directory, Devin's local-scope file is
written into the lane worktree only for the worker's lifetime and restored
afterwards, and Codex needs no file at all. Nothing is written to any user
global config.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Mapping
from urllib.parse import urlsplit

from side_lane.connector_metadata import json_mcp_name_scopes, toml_mcp_names
from side_lane.capabilities import (
    CAPABILITY_MCP_SERVERS,
    CM_SERVICES_CAPABILITIES,
    RUN_CONFIG_CAPABILITIES,
    USER_SCOPE_MCP_CAPABILITIES,
)


class McpRunConfigError(RuntimeError):
    """A coordinator-supplied per-run MCP config is unusable or unsafe."""


#: Canonical ``USER_SCOPE_MCP_CAPABILITIES`` and ``RUN_CONFIG_CAPABILITIES`` are
#: defined in :data:`side_lane.capabilities` and re-exported here for callers
#: that import from this module.  ``CAPABILITY_MCP_SERVERS`` is likewise imported
#: from that module.

#: Transport is fixed to remote streamable-HTTP MCP servers. stdio entries
#: (``command``/``args``/``env``) are rejected: a per-run config is a remote
#: registration, not a way to hand the worker a process to run.
REQUIRED_TYPE = "http"
ALLOWED_KEYS = frozenset({"type", "url", "headers"})
SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
#: One header value = optional constant auth-scheme word, one ``${ENV_NAME}``
#: reference, nothing else. A value without a reference would be a literal
#: credential; trailing text after the reference could smuggle one.
ENV_REFERENCE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9._+-]{0,31} )?\$\{([A-Z_][A-Z0-9_]*)\}$")
#: Loopback hosts are the one http:// exception, so delivery can be qualified
#: against a local mock MCP server; everything else must be https. EXACT
#: literals only: ``localhost.attacker.tld`` is a public DNS name that happens
#: to start with ``localhost.``, not a loopback address, so a prefix test would
#: send the credential-referencing registration off the machine over plaintext.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_RUN_CONFIG_BYTES = 65_536


@dataclass(frozen=True)
class McpRunServer:
    """One validated per-run MCP server registration (no secret values)."""

    name: str
    url: str
    #: ``(header name, scheme prefix, env name)`` per env-referenced header.
    headers: tuple[tuple[str, str, str], ...] = ()

    @property
    def env_names(self) -> tuple[str, ...]:
        return tuple(sorted({env for _name, _scheme, env in self.headers}))

    def bearer_env(self) -> str | None:
        """Env name of an ``Authorization: Bearer ${ENV}`` header, if any.

        Scheme-exact: a ``Basic ${ENV}`` (or scheme-less) Authorization header
        is NOT a bearer reference and returns ``None`` — a caller that cannot
        preserve the exact scheme must reject the header, never re-label it.
        """
        for name, scheme, env in self.headers:
            if name.lower() == "authorization" and scheme.lower() == "bearer":
                return env
        return None


def _loopback(url) -> bool:
    # urlsplit().hostname is lowercased and strips IPv6 brackets, so exact
    # membership covers every spelling of the three loopback literals.
    return url.hostname in LOOPBACK_HOSTS


def _validate_entry(name: str, entry: object) -> McpRunServer:
    if not isinstance(name, str) or not SERVER_NAME.fullmatch(name):
        raise McpRunConfigError(
            f"mcp server name must be lowercase letters, digits or dashes: {name!r}"
        )
    if not isinstance(entry, dict):
        raise McpRunConfigError(f"mcp server {name!r} entry must be a JSON object")
    unknown = sorted(set(entry) - ALLOWED_KEYS)
    if unknown:
        raise McpRunConfigError(
            f"mcp server {name!r} has unsupported keys (per-run delivery is "
            f"remote HTTP registration only): {', '.join(unknown)}"
        )
    if entry.get("type") != REQUIRED_TYPE:
        raise McpRunConfigError(
            f"mcp server {name!r} must carry \"type\": \"http\""
        )
    url_value = entry.get("url")
    if not isinstance(url_value, str) or not url_value.strip():
        raise McpRunConfigError(f"mcp server {name!r} requires a url string")
    url = urlsplit(url_value.strip())
    if url.scheme not in {"https", "http"} or not url.hostname:
        raise McpRunConfigError(f"mcp server {name!r} url must be http(s) with a host")
    if url.scheme == "http" and not _loopback(url):
        raise McpRunConfigError(
            f"mcp server {name!r} url must use https outside loopback (the "
            "registration carries credential references)"
        )
    if url.username or url.password or url.query or url.fragment:
        raise McpRunConfigError(f"mcp server {name!r} url must be a clean endpoint URL")
    headers_value = entry.get("headers", {})
    if not isinstance(headers_value, dict):
        raise McpRunConfigError(f"mcp server {name!r} headers must be a JSON object")
    headers: list[tuple[str, str, str]] = []
    for header, value in headers_value.items():
        if not isinstance(header, str) or not header.strip():
            raise McpRunConfigError(f"mcp server {name!r} has an invalid header name")
        if not isinstance(value, str):
            raise McpRunConfigError(
                f"mcp server {name!r} header {header!r} must be a string"
            )
        match = ENV_REFERENCE.fullmatch(value.strip())
        if not match:
            raise McpRunConfigError(
                f"mcp server {name!r} header {header!r} must reference a "
                "credential by env name only (for example "
                '"Bearer ${SERVER_TOKEN}"); literal credential values are rejected'
            )
        scheme = value.strip()[: -(len(match.group(1)) + 3)].strip()
        headers.append((header.strip(), scheme, match.group(1)))
    return McpRunServer(name=name, url=url_value.strip(), headers=tuple(headers))


def load_run_mcp_config(path_value: str | Path) -> dict[str, McpRunServer]:
    """Load and fully validate one coordinator-supplied run config file.

    Every rejection is fatal: a lane never starts with a config it did not
    successfully validate. Error messages carry env NAMES only — never a
    header value or URL with embedded credentials.
    """

    path = Path(path_value).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise McpRunConfigError(f"cannot read MCP run config: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_RUN_CONFIG_BYTES:
        raise McpRunConfigError("MCP run config is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpRunConfigError(f"MCP run config is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"mcpServers"}:
        raise McpRunConfigError(
            'MCP run config must be a JSON object with exactly one key "mcpServers"'
        )
    servers = payload["mcpServers"]
    if not isinstance(servers, dict) or not servers:
        raise McpRunConfigError('"mcpServers" must be a non-empty JSON object')
    return {name: _validate_entry(name, entry) for name, entry in servers.items()}


def validate_against_capabilities(
    servers: Mapping[str, McpRunServer], capabilities: "set[str] | frozenset[str] | tuple[str, ...] | list[str]"
) -> None:
    """Every declared server name must map from a granted capability.

    This is the narrowing control: a run config can never grant a server (and
    therefore tools) beyond the capabilities the coordinator explicitly
    requested for the run.
    """

    allowed = {server for capability, server in CAPABILITY_MCP_SERVERS.items()
               if capability in set(capabilities)
               and capability not in CM_SERVICES_CAPABILITIES}
    offenders = sorted(set(servers) - allowed)
    if offenders:
        mapping = ", ".join(f"{capability}={server}" for capability, server
                            in sorted(CAPABILITY_MCP_SERVERS.items()))
        raise McpRunConfigError(
            f"MCP run config declares server(s) no granted capability maps to: "
            f"{', '.join(offenders)}. Capability-to-server mapping: {mapping}"
        )


def require_env_references(
    servers: Mapping[str, McpRunServer], env: Mapping[str, str]
) -> None:
    """Fail closed when a referenced env name is absent or empty.

    Credentials must resolve where the hosts resolve them (the worker child
    environment). A missing reference means the account provisioning that
    exports it did not run or did not export the name this config expects —
    launching anyway would produce a lane whose MCP auth silently fails.
    """

    missing = sorted(
        name for server in servers.values() for name in server.env_names
        if not str(env.get(name, "")).strip()
    )
    if missing:
        raise McpRunConfigError(
            "MCP run config references env name(s) absent from the worker "
            f"environment: {', '.join(missing)}"
        )


def registration_paths(
    host: str, repo: "Path | None" = None, *, env: "Mapping[str, str] | None" = None
) -> list[tuple[str, Path]]:
    """``(scope, path)`` for every MCP registration file ``host`` reads.

    ``devin mcp add --help`` documents three separate registration files — a
    user file under the CLI config directory and two repository files — none
    of which is the general ``--config`` file the Devin adapter generates.
    The Claude and Codex entries mirror those hosts' documented user and
    project config files. Only file paths are produced; no value from them is
    ever read here.

    ``env`` defaults to ``os.environ`` for ``CODEX_HOME``/``APPDATA``, with
    the home directory resolved through ``Path.home()`` (the process's own
    view). A caller that passes an explicit ``env`` — the adapters' pre-launch
    name-conflict check, which must resolve against the worker child's
    environment — pins ``HOME`` from it when present.
    """

    environment = os.environ if env is None else env
    pinned_home = str(environment.get("HOME", "")).strip() if env is not None else ""
    home = Path(pinned_home) if pinned_home else Path.home()
    if host == "codex":
        codex_home = Path(environment.get("CODEX_HOME", "").strip() or home / ".codex")
        paths = [("user", codex_home / "config.toml")]
        if repo is not None:
            paths.append(("project", repo / ".codex" / "config.toml"))
        return paths
    if host == "claude":
        paths = [
            ("user", home / ".claude.json"),
            ("user", home / ".claude" / "settings.json"),
        ]
        if repo is not None:
            paths.append(("project", repo / ".mcp.json"))
        return paths
    if host == "devin":
        # Devin CLI >=3000.3 uses dedicated native MCP files, not Claude's.
        base = (
            Path(environment["APPDATA"])
            if os.name == "nt" and environment.get("APPDATA")
            else home / ".config"
        )
        paths = [("user", base / "devin" / "mcp_config.json")]
        if repo is not None:
            paths.extend(
                [
                    ("project", repo / ".devin" / "mcp_config.json"),
                    ("local", repo / ".devin" / "mcp_config.local.json"),
                ]
            )
        return paths
    return []


def conflicting_server_names(
    servers: Mapping[str, McpRunServer], host: str, worktree: "Path | None" = None,
    *, env: "Mapping[str, str] | None" = None
) -> set[tuple[str, str, Path]]:
    """Declared names a host scope already registers, as ``(name, scope, path)``.

    Only NAMES are extracted (``connector_metadata`` keeps no values). A
    Claude user config also carries per-project entries keyed by the launch
    directory; an entry for exactly this worktree is in scope, one for any
    other path is not — a lane runs in its own worktree.
    """

    declared = set(servers)
    if not declared:
        return set()
    conflicts: set[tuple[str, str, Path]] = set()
    for scope, path in registration_paths(host, worktree, env=env):
        if not path.is_file():
            continue
        try:
            if path.suffix == ".toml":
                names = toml_mcp_names(path)
            else:
                scopes = json_mcp_name_scopes(path)
                names = {
                    name for name, key_paths in scopes.items()
                    if any(keys == () or (worktree is not None
                                          and keys == ("projects", str(worktree)))
                           for keys in key_paths)
                }
        except (OSError, ValueError) as exc:
            raise McpRunConfigError(
                f"cannot verify per-run MCP server name availability: {host} "
                f"registration file {path} is unreadable or unparsable ({exc.__class__.__name__})"
            ) from exc
        for name in sorted(names & declared):
            conflicts.add((name, scope, path))
    return conflicts


def ensure_no_registration_conflicts(
    servers: Mapping[str, McpRunServer], host: str, worktree: "Path | None" = None,
    *, env: "Mapping[str, str] | None" = None
) -> None:
    """Fail closed when any declared name is already registered on the host.

    Same-name merge precedence is NOT established for Claude's ``--mcp-config``
    (against user/project registrations) or Codex's ``-c
    mcp_servers.<name>.*`` overrides (against a ``config.toml`` entry): a
    silent overwrite would misstate which server is live and could replace an
    existing registration's auth. An explicit name conflict therefore fails
    before any model starts; equivalence is never assumed.
    """

    conflicts = conflicting_server_names(servers, host, worktree, env=env)
    if conflicts:
        detail = ", ".join(
            f"{name} ({scope}: {path})" for name, scope, path in sorted(conflicts)
        )
        raise McpRunConfigError(
            "per-run MCP config declares server name(s) the host already "
            f"registers: {detail}. A same-name registration is never "
            "overwritten or shadowed — remove or rename the existing entry, "
            "or choose another server name"
        )


def claude_payload(servers: Mapping[str, McpRunServer]) -> dict[str, object]:
    """The ``--mcp-config`` file payload for Claude Code (env refs preserved)."""

    return {"mcpServers": {
        server.name: {
            "type": REQUIRED_TYPE,
            "url": server.url,
            "headers": {name: (f"{scheme} ${{{env}}}" if scheme else f"${{{env}}}")
                        for name, scheme, env in server.headers},
        } for server in sorted(servers.values(), key=lambda item: item.name)
    }}


def codex_overrides(servers: Mapping[str, McpRunServer]) -> tuple[str, ...]:
    """Additive ``-c`` argv fragments registering streamable-HTTP servers.

    Codex has no headers concept for HTTP servers beyond the documented
    ``bearer_token_env_var`` (``codex mcp add --help``), which always sends
    ``Authorization: Bearer <value>``. A config whose credential reference
    cannot be represented EXACTLY that way fails closed: an Authorization
    header with any other scheme (``Basic ${ENV}``) or no scheme would be
    silently re-labelled Bearer — changing what the bridge receives — and
    other headers cannot be delivered at all.
    """

    fragments: list[str] = []
    for server in sorted(servers.values(), key=lambda item: item.name):
        bearer = server.bearer_env()
        others = [name for name, _scheme, _env in server.headers
                  if name.lower() != "authorization"]
        auth_schemes = [scheme for name, scheme, _env in server.headers
                        if name.lower() == "authorization"]
        if others:
            raise McpRunConfigError(
                f"mcp server {server.name!r}: the Codex host delivers only an "
                "Authorization bearer env reference for per-run HTTP servers; "
                f"unsupported header(s): {', '.join(sorted(others))}"
            )
        for scheme in auth_schemes:
            if scheme.lower() != "bearer":
                described = f"auth scheme {scheme!r}" if scheme else "no auth scheme"
                raise McpRunConfigError(
                    f"mcp server {server.name!r}: the Codex host delivers only a "
                    f"Bearer Authorization env reference (bearer_token_env_var); "
                    f"{described} cannot be preserved and is not converted to Bearer"
                )
        fragments.append(f"mcp_servers.{server.name}.url={json.dumps(server.url)}")
        if bearer is not None:
            fragments.append(
                f"mcp_servers.{server.name}.bearer_token_env_var={json.dumps(bearer)}"
            )
    # Each fragment becomes its own "-c <fragment>" pair; return them flat and
    # let the adapter interleave the flag so ordering stays explicit.
    pairs: list[str] = []
    for fragment in fragments:
        pairs.extend(("-c", fragment))
    return tuple(pairs)


def devin_local_payload(servers: Mapping[str, McpRunServer]) -> dict[str, object]:
    """The Devin local-scope ``mcp_config.local.json`` payload.

    ``devin mcp add --help`` documents the local project scope (not committed)
    and HTTP transport. Header env-reference expansion by the Devin host is
    UNVERIFIED (module docstring); delivery therefore remains fail-visible at
    the bridge rather than assumed.
    """

    return {"mcpServers": {
        server.name: {
            "url": server.url,
            "transport": REQUIRED_TYPE,
            **({"headers": {name: (f"{scheme} ${{{env}}}" if scheme else f"${{{env}}}")
                            for name, scheme, env in server.headers}}
               if server.headers else {}),
        } for server in sorted(servers.values(), key=lambda item: item.name)
    }}


def _server_names_from_config(path: Path) -> set[str]:
    """Extract server names from a JSON MCP config without reading values."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(config, dict):
        return set()
    names: set[str] = set()
    # Top-level mcpServers
    if "mcpServers" in config:
        servers = config["mcpServers"]
        if isinstance(servers, dict):
            for name in servers:
                if isinstance(name, str):
                    names.add(name)
    # Claude projects entry: {"projects": {"/path": {"mcpServers": {...}}}}
    if "projects" in config:
        projects = config["projects"]
        if isinstance(projects, dict):
            for project_entry in projects.values():
                if isinstance(project_entry, dict) and "mcpServers" in project_entry:
                    servers = project_entry["mcpServers"]
                    if isinstance(servers, dict):
                        for name in servers:
                            if isinstance(name, str):
                                names.add(name)
    return names


def _matching_project_servers(
    path: Path, worktree: Path
) -> dict[str, dict[str, object]]:
    """Extract mcpServers from a Claude projects entry matching the worktree path.

    Returns an empty dict if the path does not exist, is not valid JSON, or has
    no entry for the given worktree.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(config, dict):
        return {}
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    entry = projects.get(str(worktree))
    if not isinstance(entry, dict):
        return {}
    servers = entry.get("mcpServers")
    if isinstance(servers, dict):
        return servers
    return {}


def build_strict_mcp_bundle(
    host: str,
    repo: Path,
    worktree: Path,
    home: Path,
    run_servers: "Mapping[str, McpRunServer] | None" = None,
    granted_capabilities: "frozenset[str] | set[str] | tuple[str, ...] | list[str]" = (),
) -> dict[str, object]:
    """Assemble the narrow MCP bundle for a routed execute lane.

    Combines registrations from:
      1. Per-run servers (aws-read via validated --mcp-config file)
      2. User-global config (USER_SCOPE_MCP_CAPABILITIES servers: cm-services, gitnexus,
         codegraph, playwright, slack — loaded from ~/.claude.json)
      3. Project entry in user config (matching repo path)
      4. Lane worktree .mcp.json

    Only server names mapped from granted capabilities are included. No other
    host-registered servers appear in the bundle. This is used with
    ``--strict-mcp-config`` to prevent loading of any inherited registrations.

    Raises ``ClaudeAdapterError`` via ``_effective_mcp_registrations`` if the same
    server name has different definitions across scopes.

    Args:
        host: "claude" | "codex" | "devin"
        repo: The canonical repo checkout (for project-entry matching)
        worktree: The lane worktree path (for worktree-scope .mcp.json)
        home: The worker's controlled HOME (for user-scope configs)
        run_servers: Per-run validated servers (from coordinator's run config)
        granted_capabilities: Set of granted capability names

    Returns:
        A dict with a "mcpServers" key suitable for ``--mcp-config``
    """
    servers: dict[str, dict[str, object]] = {}
    capabilities_set = set(granted_capabilities)

    # 1. Per-run servers (aws-read)
    if run_servers:
        for server in run_servers.values():
            servers[server.name] = {
                "type": REQUIRED_TYPE,
                "url": server.url,
                "headers": {
                    name: (f"{scheme} ${{{env}}}" if scheme else f"${{{env}}}")
                    for name, scheme, env in server.headers
                },
            }

    # 2-4. User-scope registrations via _effective_mcp_registrations
    # (claude adapter handles all scopes + conflict detection; non-claude hosts skip)
    if host == "claude":
        from side_lane.adapters.claude import _effective_mcp_registrations

        regs = _effective_mcp_registrations(
            host=host,
            repo=repo,
            worktree=worktree,
            home=home,
            granted_capabilities=capabilities_set,
        )
        # regs are (server_name, scope, path, definition) tuples
        for name, _scope, _path, definition in regs:
            servers[name] = definition

    return {"mcpServers": servers}


def _secure_runtime_directory(target: "str | Path") -> Path:
    """Ensure a real, private 0700 runtime directory exists.

    Refuses any symlink or non-directory component in the path to prevent
    writes outside the intended runtime.  Pre-existing directories are forced
    to 0700 so a 0755 directory left by a prior run or a permissive umask
    cannot leak bundle contents.
    """
    target = Path(target).expanduser().absolute()
    # Refuse a symlink or non-directory in the immediate parent or the target
    # itself.  Deeper ancestors are trusted system directories (e.g. macOS
    # /var) and are not checked.  The intended runtime directory itself is
    # forced to 0700, replacing any preexisting 0755 mode.
    parent = target.parent
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise McpRunConfigError(
                f"runtime path contains a symlink or non-directory: {parent}"
            )
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise McpRunConfigError(
                f"runtime path contains a symlink or non-directory: {target}"
            )
    # Any missing parents are created with 0o700; umask may still widen the
    # mode, so the final chmod below is the authoritative private bit.
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError as exc:
        raise McpRunConfigError(
            f"cannot set 0700 mode on runtime directory {target}: {exc}"
        ) from exc
    return target


def write_strict_mcp_bundle(
    runtime_dir: Path, worktree_name: str, bundle: dict[str, object]
) -> Path:
    """Atomically write the strict MCP bundle as 0600 outside the worktree.

    The file is written to ``runtime_dir / "<worktree-name>-mcp-strict.json"``.
    Permissions are set to 0600 to prevent group/other access.

    The runtime directory is created with mode 0700 so that only the owning user
    can inspect the bundle contents. The file is written via ``tempfile.mkstemp``
    (mode 0600, O_EXCL to prevent symlink attacks) and renamed into place
    atomically. On any failure the temp file is removed.

    Args:
        runtime_dir: The run-local runtime directory (outside the worktree)
        worktree_name: The worktree directory name (used in the filename)
        bundle: The mcpServers dict from ``build_strict_mcp_bundle``

    Returns:
        Path to the written file
    """
    runtime_dir = _secure_runtime_directory(runtime_dir)
    # Sanitize worktree name for use in a filename
    safe_name = "".join(c if c.isalnum() or c in ".-_" else "_" for c in worktree_name)
    path = runtime_dir / f"{safe_name}-mcp-strict.json"
    # Atomic write: mkstemp gives mode 0600 and O_EXCL (no symlink race).
    fd, tmp_path_str = tempfile.mkstemp(dir=str(runtime_dir), prefix=f".{safe_name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(bundle, indent=2) + "\n")
        os.rename(tmp_path_str, str(path))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path_str)
        raise
    os.chmod(str(path), 0o600)
    return path


def write_ephemeral(directory: str | Path, filename: str,
                    payload: Mapping[str, object]) -> Path:
    """Write one runtime config artifact (0600) under a run-local directory.

    The directory is created with mode 0700. The file is written via
    ``tempfile.mkstemp`` (mode 0600, O_EXCL) and renamed into place atomically.
    On any failure the temp file is removed.
    """

    target = _secure_runtime_directory(directory)
    path = target / filename
    # mkstemp gives mode 0600 and O_EXCL (no symlink race).
    fd, tmp_path_str = tempfile.mkstemp(dir=str(target), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.rename(tmp_path_str, str(path))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path_str)
        raise
    os.chmod(str(path), 0o600)
    return path


def audit_names(servers: Mapping[str, McpRunServer]) -> tuple[str, ...]:
    """Server NAMES for the run audit — never URLs or env references' values."""

    return tuple(sorted(servers))


def startup_note(servers: Mapping[str, McpRunServer]) -> str:
    """Worker instruction naming the delivered per-run MCP registration.

    Presence of a registration is not authentication and not a live read; the
    note tells the worker to check the exact granted tool names and report the
    exact state otherwise (mirrors the slack-read pattern).
    """

    names = ", ".join(f"`{name}`" for name in sorted(servers))
    return (
        "\n\n# Per-run MCP registration\n\n"
        f"This run delivered MCP server registration(s) {names} through the "
        "coordinator's validated run config. Only the exact per-tool allowlist "
        "granted by this lane's capabilities may be called — never enable or "
        "call a tool outside it. A registration (or a successful server wait) "
        "is presence evidence only, not proof of authentication or of a read; "
        "if the granted tools are absent, named differently, or the server "
        "reports an authentication failure, stop and report that exact state "
        "instead of substituting another tool or claiming a read happened.\n"
    )


__all__ = [
    "CAPABILITY_MCP_SERVERS",
    "McpRunConfigError",
    "McpRunServer",
    "audit_names",
    "claude_payload",
    "codex_overrides",
    "conflicting_server_names",
    "devin_local_payload",
    "ensure_no_registration_conflicts",
    "load_run_mcp_config",
    "registration_paths",
    "require_env_references",
    "startup_note",
    "validate_against_capabilities",
    "write_ephemeral",
]
