from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence

from side_lane import evaluation, report_stop_hook, routing, selector_policy
from side_lane.auth import AuthError, auth_status, require_native_oauth
from side_lane.capabilities import (
    CODEX_CONNECTOR_NAME_CAPABILITIES,
    USER_SCOPE_MCP_CAPABILITIES,
)
from side_lane.credentials import CredentialError, credential_present, read_credential
from side_lane.connector_metadata import toml_mcp_names
from side_lane.governance import (
    GovernanceError,
    publication_refusal_capability_conflicts,
    report_write_capability_conflicts,
    validate_repository,
)
from side_lane.hosts import (
    HostExecutableError,
    host_support_dir,
    require_host_executable,
    resolve_host_executable,
)
from side_lane.mcp_run_config import (
    CAPABILITY_MCP_SERVERS as RUN_MCP_CAPABILITY_SERVERS,
    McpRunConfigError,
    _registration_is_file,
    _selected_project,
    audit_names,
    json_registration_scopes,
    load_run_mcp_config,
    registration_paths,
    require_env_references,
    validate_against_capabilities,
)
from side_lane.read_roots import ReadRootError, parse_read_roots
from side_lane.redaction import redact_provider_secret
from side_lane.web_domains import WebDomainError, parse_web_domains
from side_lane.skill_bundle import SkillBundleError, catalog_note, deliver_skills
from side_lane.adapters.claude import (
    EXECUTE_PROFILES,
    LOCAL_DEVELOPER_PROFILE,
    STANDARD_PROFILE,
    ClaudeAdapterError,
    require_report_only_budget,
)
from side_lane.adapters.codex import CodexAdapterError
from side_lane.adapters.devin import DevinAdapterError
from side_lane.worktrees import (
    ASSIGNMENT_SCHEMA_VERSION,
    SCRATCH_DIR_NAME,
    UNTRACKED_STATUS,
    WORKSPACE_IGNORED_BYTE_LIMIT,
    WORKSPACE_IGNORED_PATH_LIMIT,
    AssignmentRecord,
    LaneDelivery,
    WorktreeError,
    WorktreeRun,
    WorkspaceBaseline,
    WorkspaceDelta,
    WorkspaceLock,
    acquire_workspace_lock,
    adopt_existing_workspace,
    capture_workspace_baseline,
    create_worktree,
    dispose_clean_worktree,
    git_status,
    is_linked_worktree,
    lane_delivery,
    publish_lane_branch,
    release_workspace_lock,
    resolve_existing_workspace,
    snapshot_source,
    source_mutations,
    verify_lane,
    workspace_deltas,
    write_assignment,
    write_audit,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "models.json"
MAX_PROMPT_CHARS = 100_000
MAX_PROFILE_CHARS = 100_000
MAX_MEASUREMENT_CHARS = 8_192
# The vocabulary below is the measurement ledger's, not a new one: an
# assignment captured here must be liftable into that ledger unchanged
# (docs/automatic-side-lane/ledger-schema.json).
MEASUREMENT_HOST_FAMILIES = frozenset({"local-codex", "slack-claude-tag"})
MEASUREMENT_WEIGHTS = frozenset({1, 3, 8})
MEASUREMENT_PLANNING_DISPOSITIONS = frozenset(
    {"small-plan-and-run", "substantial-plan-approved"}
)
# The ledger's own task_id pattern, with an explicit length bound so the sidecar
# stays small and its name-safe key stays name-safe.
MEASUREMENT_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MEASUREMENT_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "host_family",
        "preassigned_weight",
        "planning_disposition",
        "rework",
    }
)
MEASUREMENT_OPTIONAL_FIELDS = frozenset({"parent_task_id"})
MEASUREMENT_FIELDS = MEASUREMENT_REQUIRED_FIELDS | MEASUREMENT_OPTIONAL_FIELDS
REVIEW_UNSAFE = tuple(
    re.compile(p, re.I)
    for p in (
        r"\b(?:edit|modify|write|delete|create)\s+(?:the\s+|a\s+)?(?:files?|code|repo)",
        r"(?:^|[.!?]\s+)(?:please\s+)?(?:fix|implement|refactor|update|add|remove|rename|replace|change)\b",
        r"\b(?:fix|implement|refactor|update|add|remove|rename|replace|change)\s+(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+\b",
        r"\b(?:apply|produce)\s+(?:a\s+)?(?:patch|diff)",
        # Imperative/verb usage only, for exactly the reason the EXECUTE_UNSAFE
        # "deploy" alternative below is shaped this way. A bare
        # \b(?:commit|push|merge|deploy)\b made review mode unusable for its most
        # common job: "review commit a321031a" read as an instruction to commit,
        # and so did a prompt telling the reviewer "do not commit, push" — the
        # noun and the negation both matched. Block the instruction to write
        # history, not every mention of the word.
        # The two lookaheads keep a sentence-INITIAL verb from swallowing a noun
        # phrase: "Merge commit 7a8ed4ab introduced ...", "Push notifications are
        # broken", "Deploy scripts live in ...", "commit a321031a on branch foo".
        # A `git commit/push/merge` COMMAND is refused outright (lane-governance.md
        # "Review mode": do not edit, patch, commit, push, deploy). Anchored to a
        # line start or backtick so the command form is caught while prose that
        # merely names it — "inspect git push output" — is not.
        r"(?:^|[\n`])\s*git\s+(?:commit|push|merge)\b"
        r"|(?:^|[.!?]\s+)(?:please\s+)?(?:commit|push|merge|deploy)\b(?![.\-_/])"
        r"(?!\s+(?:commit|message|conflict|notification|script|hash|sha|log|token|key)s?\b)"
        r"(?!\s+[0-9a-f]{7,40}\b)"
        # Negated wording is a PROHIBITION, not an instruction: "Do not commit the
        # changes." must reach the reviewer. Fixed-width lookbehinds, which is all
        # Python's re allows; "not " also covers "must not ", "should not ".
        r"|(?<!not )(?<!n't )(?<!never )"
        r"\b(?:commit|push|merge|deploy)\s+(?:it|this|that|them|these|those|the|your|my|our|to|now)\b"
        r"|\brun\s+the\s+(?:commit|push|merge|deploy)\b",
        r"\b(?:bypass|disable|skip)\s+(?:permissions?|sandbox|guardrails?)\b",
    )
)
# The "deploy" alternative below intentionally matches only verb/imperative
# usage of the word ("deploy it", "run the deploy", a sentence-initial
# "Deploy ..."), never the filename (deploy.py), a noun ("deployment", "the
# deploy script", "deploy config/workflow", DEPLOYMENT_TYPE), or the word
# inside a path or identifier. The goal is to block the instruction to ship
# code, not to ban naming the tooling that does it.
# The internal BE gateway repo's functions/sideLaneGateway/prompt_governance.py
# mirrors these EXECUTE_UNSAFE regexes verbatim and must be re-synced
# whenever this tuple changes.
EXECUTE_UNSAFE = tuple(
    re.compile(p, re.I)
    for p in (
        r"--(?:dangerously-skip-permissions|allow-dangerously-skip-permissions|ignore-user-config)",
        r"\b(?:force[- ]?push|merge\s+(?:the\s+)?(?:pr|branch))\b"
        r"|(?:^|[.!?]\s+)(?:please\s+)?deploy\b(?![.\-_/])"
        r"|\bdeploy\s+(?:it|this|the|to|now|that)\b"
        r"|\brun\s+the\s+deploy\b",
        r"\b(?:insert|update|delete|drop|alter|truncate|create)\s+(?:into\s+|table\s+|database\s+)",
        r"\b(?:change|grant|revoke|rotate|delete)\s+(?:iam|credentials?|secrets?|cloud resources?)\b",
    )
)


LAUNCHABLE_STATES = frozenset({"verified", "present"})

#: Execute-lane tool profiles. The names are the Claude adapter's own, imported
#: so a rename cannot leave this surface selecting a profile the host refuses.
EXECUTE_PROFILE_AUTO = "auto"
EXECUTE_PROFILE_CHOICES = (EXECUTE_PROFILE_AUTO, STANDARD_PROFILE, LOCAL_DEVELOPER_PROFILE)
#: The private route table's own statement that a lane runs on the
#: coordinator's machine: the same vocabulary the routing catalog already uses
#: (`side_lane.routing.EXECUTION_LOCATIONS`). It is the *guard* half of the
#: local developer selection: a route that declares nothing, or declares
#: `cloud-only`, can never resolve to the wider profile however the private
#: policy is written.
LOCAL_USER_WORKSPACE = "local-user-workspace"
#: The *policy* half of that selection: the explicit private statement that
#: routes this table declares local should default to the local developer
#: profile. It lives in the private route table and nowhere else — this public
#: package ships the mechanism and the conservative default, never the policy.
#: A table that declares no policy (the public `config/models.json`, a
#: cloud-generated table, an older table) resolves every route to `standard`
#: however many of its routes declare a local location.
EXECUTE_PROFILE_POLICY_KEY = "execute_profile_policy"
#: The one key that policy block is read for.
EXECUTE_PROFILE_POLICY_DEFAULT_KEY = "local_default"


class SideLaneError(Exception):
    pass


def config_path() -> Path:
    override = os.environ.get("SIDE_LANE_MODELS_PATH")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SideLaneError(f"cannot load model allowlist: {exc}") from exc
    if config.get("schema_version") != 3 or not isinstance(
        config.get("providers"), dict
    ):
        raise SideLaneError("model allowlist requires schema_version 3")
    if not isinstance(config.get("capabilities"), list):
        raise SideLaneError("capability allowlist is invalid")
    for provider, item in config["providers"].items():
        if (
            not isinstance(item, dict)
            or item.get("auth_method") not in {"oauth", "provider-key"}
            or not item.get("gateway")
        ):
            raise SideLaneError(
                f"provider {provider!r} has invalid auth/gateway metadata"
            )
        if item["auth_method"] == "oauth" and item.get("billable") is not False:
            raise SideLaneError(
                f"native provider {provider!r} must be non-billable OAuth"
            )
        if item["auth_method"] == "provider-key" and (
            item.get("billable") is not True
            or not item.get("credential_service")
            or item.get("explicit_only") is not True
        ):
            raise SideLaneError(
                f"key provider {provider!r} must be billable and explicit-only"
            )
        for mode, hosts in item.get("routes", {}).items():
            if mode not in {"review", "execute"} or not isinstance(hosts, dict):
                raise SideLaneError(f"provider {provider!r} has an invalid route")
            for host, route in hosts.items():
                models = route.get("models") if isinstance(route, dict) else None
                if (
                    host not in {"codex", "claude", "devin"}
                    or not route.get("protocol")
                    or not isinstance(models, list)
                    or not models
                ):
                    raise SideLaneError(
                        f"provider {provider!r} has an invalid host route"
                    )
                if len(set(models)) != len(models) or not all(
                    isinstance(model, str) and model for model in models
                ):
                    raise SideLaneError(f"provider {provider!r} has invalid models")
                model_configs = route.get("model_configs", {})
                if not isinstance(model_configs, dict) or any(
                    key not in models or not isinstance(value, dict)
                    for key, value in model_configs.items()
                ):
                    raise SideLaneError(
                        f"provider {provider!r} has invalid model_configs"
                    )
    return config


def select_route(
    config: Mapping[str, Any], host: str, mode: str, provider: str, model: str
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    provider_config = config["providers"].get(provider)
    if not isinstance(provider_config, Mapping):
        raise SideLaneError(f"unknown provider: {provider}")
    route = provider_config.get("routes", {}).get(mode, {}).get(host)
    if route is None:
        raise SideLaneError(f"unsupported route: {host}/{mode}/{provider}")
    if model not in route["models"]:
        raise SideLaneError(
            f"model {model!r} is not allowed for {host}/{mode}/{provider}"
        )
    model_config: dict[str, Any] = {
        "runtime_model": model,
        "protocol": str(route["protocol"]),
        "wire_api": str(route["protocol"]),
        "gateway": provider_config["gateway"],
        "auth_method": provider_config["auth_method"],
        "billable": provider_config["billable"],
    }
    for key in (
        "identity_contract",
        "reasoning_effort",
        "max_budget_usd",
        "execution_location",
    ):
        if key in route:
            model_config[key] = route[key]
    model_config.update(route.get("model_configs", {}).get(model, {}))
    billable = model_config["billable"]
    if not isinstance(billable, bool):
        raise SideLaneError("model billable metadata must be boolean")
    if billable != provider_config["billable"] and not (
        host == "devin"
        and route["protocol"] == "native-devin"
        and provider_config["auth_method"] == "oauth"
    ):
        raise SideLaneError(
            "model billing override is supported only for native Devin OAuth"
        )
    return {**provider_config, "billable": billable}, model_config


def validate_selection(
    config: Mapping[str, Any],
    provider: str,
    model: str,
    *,
    host: str = "claude",
    mode: str = "review",
) -> Mapping[str, Any]:
    return select_route(config, host, mode, provider, model)[1]


def execute_profile_argument(args: object) -> str:
    """Read ``--execute-profile`` off a parsed argument namespace.

    Exact-type read, in the same spirit as the report-only opt-in below: a
    selection is a string, and anything else — a namespace that never declared
    the field, or a stand-in with no value at all — means no profile was
    selected, which resolves to ``auto``. A string that is not one of the
    profile names is still passed through so ``resolve_execute_profile`` can
    refuse it; only a non-selection collapses to the default.
    """

    value = getattr(args, "execute_profile", EXECUTE_PROFILE_AUTO)
    return value if isinstance(value, str) else EXECUTE_PROFILE_AUTO


def execute_profile_policy(config: Mapping[str, Any] | None) -> str:
    """The private route table's explicit default for a route it declares local.

    This is the policy half of the local developer selection, and it is read
    from the configuration the run was loaded with — never from the caller's
    environment, and never inferred from a provider or model name. An ABSENT key
    — no ``config`` at all, or a table that declares no policy block — is the
    public conservative default: ``standard``. A key that is PRESENT must carry
    a block that names one of the two profiles: a policy that is present at all
    is an operator statement about this table, so a null value, a non-object, or
    an object that names no profile is a statement whose meaning cannot be read
    and refuses the run. Reading a present-but-unreadable block as "no policy"
    would silently discard a deliberate opt-in whose key was mistyped, and
    reading it as the wider profile would grant authority from a typo; only the
    absent key is the conservative default.
    """

    if config is None or EXECUTE_PROFILE_POLICY_KEY not in config:
        return STANDARD_PROFILE
    policy = config[EXECUTE_PROFILE_POLICY_KEY]
    if not isinstance(policy, Mapping):
        raise SideLaneError(
            f"`{EXECUTE_PROFILE_POLICY_KEY}` is present and must be an object "
            f"naming `{EXECUTE_PROFILE_POLICY_DEFAULT_KEY}`; got {policy!r}"
        )
    if EXECUTE_PROFILE_POLICY_DEFAULT_KEY not in policy:
        raise SideLaneError(
            f"`{EXECUTE_PROFILE_POLICY_KEY}` is present and must name "
            f"`{EXECUTE_PROFILE_POLICY_DEFAULT_KEY}`; got "
            f"{sorted(policy)!r}"
        )
    declared = policy[EXECUTE_PROFILE_POLICY_DEFAULT_KEY]
    if declared not in EXECUTE_PROFILES:
        raise SideLaneError(
            f"`{EXECUTE_PROFILE_POLICY_KEY}.{EXECUTE_PROFILE_POLICY_DEFAULT_KEY}` "
            f"must be one of {', '.join(EXECUTE_PROFILES)}; got {declared!r}"
        )
    return str(declared)


def route_runs_local(model_config: Mapping[str, Any]) -> bool:
    """Whether this route runs on the operator's own machine, by the shared rule.

    One seam, read through :func:`side_lane.routing.effective_execution_location`,
    so the launch path and the routing catalog cannot answer differently about
    the same route. A declared ``execution_location`` is authoritative — that
    includes the conservative ``cloud-only``, which is what keeps a cloud route
    unable to reach a local-user-workspace contract however the private policy
    is written. A route that declares none is inferred local only for the
    native host protocols (the operator's own CLI, signed in with their own
    OAuth session, in a checkout they own); everything else stays ``unknown``
    and is refused.
    """

    return (
        routing.effective_execution_location(model_config) == LOCAL_USER_WORKSPACE
    )


def resolve_execute_profile(
    *,
    requested: str,
    mode: str,
    host: str,
    model_config: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    existing_workspace: bool = False,
    report_deliverable: bool = False,
) -> str:
    """Resolve this run's execute profile, failing closed on any honest doubt.

    The local developer profile is selected by statements that must all hold:

    - the **policy**: the private route table explicitly opts its local routes
      into the profile (:func:`execute_profile_policy`);
    - the **guard**: this route runs on the operator's own machine — it
      declares ``execution_location: local-user-workspace``, or declares no
      location at all while being one of the native host protocols, which is
      the same narrow inference the routing catalog already applies
      (:func:`route_runs_local`). A route that declares ``cloud-only``, or that
      declares nothing and is not a native host protocol, is never local and
      the profile is refused rather than widened.
    - for a route that got there by inference rather than by declaration, a
      **statement of its own**: the operator named an existing owner workspace
      (``existing_workspace=True``, from ``--existing-workspace``) or named this
      profile itself. Both the declaration and such a statement say that this
      lane works in the operator's own tree; a native route's bare ``protocol``
      says only where the route *would* run, so an ordinary lane on that route
      keeps the conservative per-command allowlist the public default has always
      given it. The profile is never a default a lane drifts into: it takes a
      declaration or a statement, and the private policy on top.

    ``auto`` therefore yields ``local-developer`` only for a private table that
    opted in *and* one of those statements. Everything else — the public
    ``config/models.json``, whose own routes declare a local location but which
    states no policy; a cloud-generated table; a ``cloud-only`` route; a route
    that declares nothing on a lane that selected no workspace — yields the
    conservative ``standard`` profile. Nothing about the caller's environment
    is consulted, so a lane cannot become local by accident.
    A table whose policy key is *present* but unreadable is neither: it refuses
    the run rather than resolving either way (:func:`execute_profile_policy`).

    The two explicit values narrow or confirm:

    - ``standard`` always yields ``standard``, so an otherwise-local route can
      be run under the per-command allowlist.
    - ``local-developer`` is refused unless the policy and the route hold. On a
      route that only infers local, the explicit selection stands in for the
      declaration here, exactly as it guards the mode; the profile is the wider
      of the two, and either a missing premise or a missing policy is authority
      the coordinator did not grant.

    Review mode refuses any explicit selection and yields ``standard``: a review
    lane's argv is the strict read-only form with no shell or MCP tool, so a
    profile there would describe a lane the worker never ran under.

    ``host`` is deliberately **not** a premise. The profile is a property of
    where the lane runs and what the private table authorized, not of which
    product executes it: the same same-user local workspace is the same ordinary
    developer surface whichever qualified host runs there, and gating on the
    host name made the profile reachable only by an operator who had already
    discovered which host to name — the configuration statement was true and the
    routing still denied the read. Each adapter renders the profile on its own
    seam (the Claude ``--allowedTools`` surface, the Devin PreToolUse command
    policy, the Codex native surface, which already runs wider than the profile
    asks) and none of them is consulted here. The parameter is retained for the
    callers and for the refused-run evidence, and it no longer selects a result.

    This is the *tool-surface* selection and nothing else. Whether a lane may be
    pointed at an existing owner workspace is a separate question, answered by
    :func:`_validate_existing_workspace` from the same statements plus the host,
    and it is what a host with no allowlist seam can still carry.

    A lane whose deliverable is the report (``--report-only`` or
    ``--report-deliverable``; ``report_deliverable`` is the effective contract
    both flags normalize to) always resolves ``standard``, whatever the table
    and the route declare: the report contract is the narrowed form of an
    execute lane, and the widened surface — the bare shell class and the
    server-wide rules for the host's own MCP registrations — is exactly what it
    exists to exclude. The private policy is not consulted for such a lane,
    because no policy statement can make a report lane wider than its own
    contract. An *explicit* ``local-developer`` selection together with a
    report flag is two contradictory requests, and it is refused rather than
    silently reconciled: dropping the profile would bypass the operator's own
    selection, and honoring it would bypass the report restrictions, so
    neither narrowing is silent.
    """

    if requested not in EXECUTE_PROFILE_CHOICES:
        raise SideLaneError(
            f"unknown execute profile: {requested!r}; expected one of "
            + ", ".join(EXECUTE_PROFILE_CHOICES)
        )
    if mode != "execute":
        if requested != EXECUTE_PROFILE_AUTO:
            raise SideLaneError(
                "--execute-profile is supported only in execute mode: a review "
                "lane's argv is the strict read-only form and has no shell or "
                "MCP tool for a profile to widen"
            )
        return STANDARD_PROFILE
    if report_deliverable:
        if requested == LOCAL_DEVELOPER_PROFILE:
            raise SideLaneError(
                f"the {LOCAL_DEVELOPER_PROFILE} execute profile cannot carry "
                "a report deliverable: the report contract makes the lane the "
                "narrowed form, so the two selections are contradictory and "
                "the run is refused rather than silently narrowed past an "
                "explicit profile selection"
            )
        return STANDARD_PROFILE
    if requested == STANDARD_PROFILE:
        return STANDARD_PROFILE
    if not route_runs_local(model_config):
        if requested == LOCAL_DEVELOPER_PROFILE:
            raise SideLaneError(
                f"the {LOCAL_DEVELOPER_PROFILE} execute profile requires a route "
                "that declares execution_location: "
                f"{LOCAL_USER_WORKSPACE}; this route does not, so the profile's "
                "own premise is false and the run is refused rather than widened"
            )
        return STANDARD_PROFILE
    if execute_profile_policy(config) != LOCAL_DEVELOPER_PROFILE:
        if requested == LOCAL_DEVELOPER_PROFILE:
            raise SideLaneError(
                f"the {LOCAL_DEVELOPER_PROFILE} execute profile requires the "
                f"route table to opt in with `{EXECUTE_PROFILE_POLICY_KEY}`: "
                f"{{{EXECUTE_PROFILE_POLICY_DEFAULT_KEY!r}: "
                f"{LOCAL_DEVELOPER_PROFILE!r}}}. This table does not, so no "
                "private policy selects the wider profile and the run is "
                "refused rather than widened"
            )
        return STANDARD_PROFILE
    if model_config.get("execution_location") == LOCAL_USER_WORKSPACE:
        return LOCAL_DEVELOPER_PROFILE
    if existing_workspace or requested == LOCAL_DEVELOPER_PROFILE:
        # Local by inference only, and this lane carries a statement of its own:
        # the operator named their own workspace, or named this profile. The
        # route's protocol says where the route *would* run, not that this lane
        # works in the operator's tree — so it is that statement, not the
        # protocol, that turns the inference into the wider tool surface.
        return LOCAL_DEVELOPER_PROFILE
    # Local by inference, with neither statement: leave the tool surface exactly
    # as the public conservative default has always had it.
    return STANDARD_PROFILE


def validate_governance(repo_argument: str) -> Path:
    try:
        return validate_repository(repo_argument)
    except GovernanceError as exc:
        raise SideLaneError(str(exc)) from exc


def load_prompt(
    prompt: str | None, prompt_file: str | None, mode: str = "review"
) -> str:
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.is_file():
            raise SideLaneError(f"prompt file is not readable: {path}")
        value = path.read_text(encoding="utf-8")
    else:
        value = prompt or ""
    if not value.strip() or len(value) > MAX_PROMPT_CHARS:
        raise SideLaneError("prompt is empty or too long")
    if any(
        pattern.search(value)
        for pattern in (REVIEW_UNSAFE if mode == "review" else EXECUTE_UNSAFE)
    ):
        raise SideLaneError(f"prompt requests an action prohibited in {mode} mode")
    return value


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="side-lane", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    candidates = sub.add_parser("candidates", allow_abbrev=False)
    candidates.add_argument("--json", action="store_true")
    credentials = sub.add_parser("credentials", allow_abbrev=False)
    credentials.add_argument("--json", action="store_true")
    auth = sub.add_parser("auth-status", allow_abbrev=False)
    auth.add_argument("--host", choices=("codex", "claude", "devin"), required=True)
    auth.add_argument("--json", action="store_true")
    check = sub.add_parser("check-capabilities", allow_abbrev=False)
    check.add_argument("--host", choices=("codex", "claude", "devin"), required=True)
    check.add_argument("--mode", choices=("review", "execute"), default="execute")
    check.add_argument("--provider")
    check.add_argument("--model")
    check.add_argument("--repo")
    check.add_argument("--json", action="store_true")
    recommend = sub.add_parser("recommend", allow_abbrev=False)
    recommend.add_argument("--repo", required=True)
    recommend.add_argument("--profile", required=True)
    recommend.add_argument(
        "--routed-policy-snapshot",
        metavar="PATH",
        default=None,
        help="repeatable operator-supplied routed-pool server policy snapshot "
        "(a JSON object per file). A snapshot is reviewed "
        "receipt data recorded by an authenticated control-plane collector — "
        "it binds selector, canonical member identities, upstream attested "
        "set, policy revision/fingerprint, nonsecret caps, the fail-closed "
        "context-fit marker, an aware UTC observation timestamp (fresh for "
        "900 seconds), and evidence provenance. It is a consistency/freshness "
        "check only, never proof the live server enforces the policy; the "
        "task profile cannot self-assert verification",
        action="append",
    )
    evaluate = sub.add_parser("evaluate", allow_abbrev=False)
    evaluate.add_argument("--input", required=True)
    run = sub.add_parser("run", allow_abbrev=False)
    run.add_argument("--host", choices=("codex", "claude", "devin"), required=True)
    run.add_argument("--mode", choices=("review", "execute"), default="review")
    run.add_argument("--provider", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--repo", required=True)
    run.add_argument("--lane-name")
    run.add_argument(
        "--worktree-root",
        help="directory for lane worktrees; default <repo>/.side-lanes/worktrees, or $SIDE_LANE_WORKTREE_ROOT; relative paths are anchored to the repo",
    )
    run.add_argument("--capability", action="append", default=[])
    run.add_argument(
        "--execute-profile",
        choices=EXECUTE_PROFILE_CHOICES,
        default=EXECUTE_PROFILE_AUTO,
        help="which execute-lane tool profile this run uses. `auto` (the "
        "default) selects `local-developer` only when two independent private "
        "statements both hold: the route table opts in with "
        f"`{EXECUTE_PROFILE_POLICY_KEY}` "
        f"{{`{EXECUTE_PROFILE_POLICY_DEFAULT_KEY}`: `local-developer`}}, and "
        "this route declares `execution_location: local-user-workspace`. Every "
        "other route resolves to `standard` — including the public "
        "`config/models.json`, which declares local locations but no policy, "
        "and any cloud-generated table. The public default is `standard`: a run "
        "for which nothing selects the profile gets exactly the argv it had. "
        "`standard` narrows an otherwise-local route back to the per-command "
        "allowlist. `local-developer` additionally loads the worker host's own "
        "registered MCP servers rather than a capability-only bundle, so a "
        "developer's own registrations stay usable; it grants no capability, "
        "the cm-services proxy stays gated by its exact capability grants, "
        "and it is refused for a route or a table that "
        "does not opt in. Execute mode only on any qualified host: a review "
        "lane's argv is the strict read-only form, and each host renders the "
        "profile on its own seam — the claude allowlist, the Devin pre-tool "
        "command policy, and the codex native surface, which already runs "
        "wider than the profile asks. A report lane "
        "(--report-only or --report-deliverable) always resolves `standard` — "
        "the report contract is the narrowed form of an execute lane — and an "
        "explicit `local-developer` selection together with a report flag is "
        "refused rather than silently narrowed",
    )
    run.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="NAME",
        help="repeatable. Deliver one more pinned bundle skill by exact name "
        "(for example --skill qa-on-demand), in addition to the discipline "
        "defaults every execute lane receives. Skills that reference "
        "siblings pull them in automatically. Private repo-sourced skills "
        "(the QA skills) resolve only when the runner executes from a "
        "dev-tools checkout; elsewhere the run fails before any worker "
        "starts, naming the flag to drop. Execute mode only",
    )
    run.add_argument(
        "--read-root",
        action="append",
        default=[],
        metavar="PATH",
        help="repeatable. Grant the worker read-only access to one more existing "
        "directory outside its worktree (for example the shared instruction "
        "sources a repository's AGENTS.md/CLAUDE.md point at). The path must be "
        "absolute; glob syntax and the filesystem root are rejected. Execute "
        "mode only. The canonical path is named in the worker's instructions "
        "and recorded in the run audit. A read root never becomes writable: "
        "writes stay confined to the lane worktree",
    )
    run.add_argument(
        "--web-domain",
        action="append",
        default=[],
        metavar="HOST",
        help="repeatable. Grant the worker fetch access to one more public "
        "documentation origin, named as an exact lowercase hostname (for "
        "example --web-domain cloud.google.com). The value must be a bare "
        "hostname: a scheme, port, path, query, userinfo prefix, glob, IP "
        "literal, single-label name, dot-local/internal name or reserved "
        "suffix is rejected before anything starts. One host renders one "
        "host-scoped rule (Devin Fetch(https://<host>/*), Claude Code "
        "WebFetch(domain:<host>)); a bare Fetch/WebFetch grant is never "
        "emitted and no capability unlocks the reach. The canonical host list "
        "is named in the worker's instructions and recorded in the run audit. "
        "This is permission matching, not a network sandbox: neither "
        "unlisted destinations nor an approved origin's own redirects are "
        "covered. Execute mode only; the Codex host refuses it, because an "
        "execute Codex lane runs danger-full-access and exposes no "
        "per-destination rule",
    )
    run.add_argument(
        "--mcp-config",
        metavar="PATH",
        default=None,
        help="deliver one coordinator-supplied per-run MCP server registration "
        "file (a JSON object with exactly key mcpServers, remote streamable-HTTP "
        "entries only, credentials referenced by env name, never as values). "
        "Every declared server name must map from a granted --capability "
        "(aws-read registers the server named aws, omniroute-read registers "
        "omniroute), and each referenced env "
        "name must be present in the launching environment. Execute mode only; "
        "delivery is additive and never replaces existing MCP registrations. "
        "Server names and the config path are recorded in the run audit; URLs "
        "and credential values are not",
    )
    run.add_argument("--approve-billable-route", action="store_true")
    run.add_argument(
        "--report-only",
        action="store_true",
        help="Claude execute lanes only. Require SIDE_LANE_REPORT.md in the lane "
        "worktree and enforce it twice: a deterministic Stop hook inside the "
        "same Claude Code invocation blocks one stop — feeding the same worker "
        "the reason to write the real findings report — and the runner itself "
        "refuses to accept the lane if the report is still missing, empty, or a "
        "symlink when the worker exits. The lane is then judged on that report "
        "artifact rather than on implementation delivery, so no commit is "
        "required — a report-only lane that commits is refused instead, as is "
        "one leaving any file outside the report and the lane's git-excluded, "
        "untracked .side-lane-scratch/ scratch tree, plus — when playwright is "
        "also granted — untracked browser-report artifacts at the lane root "
        "under the namespace --report-deliverable documents (editing, staging, "
        "or deleting a *tracked* path, including one at such a name, is source "
        "work and is refused too). This option implies --report-deliverable and "
        "adds the Claude-only in-loop repair to that same verdict. "
        "delivered for such a lane follows the whole run: the worker's exit "
        "status, the coordinator-checkout comparison, and the report verdict "
        "all have to hold. The branch is never published, the coordinator "
        "checkout is still checked for outside-lane writes, and an unreadable "
        "lane still fails closed. Requires a finite positive "
        "max_budget_usd on the route, which is a client-side estimate guard, "
        "not proof that the upstream server or account enforces the same cap. "
        "The report file is an output exception to ordinary execute rules; the "
        "lane still runs in execute mode and is not a sandbox. Add shell or "
        "workspace-write capabilities when the report generation/read tool needs "
        "to write artifacts. Ordinary execute and review lanes are unchanged",
    )
    run.add_argument(
        "--report-deliverable",
        action="store_true",
        help="Execute lanes only, any host. Require SIDE_LANE_REPORT.md in the "
        "lane worktree and judge the lane on that report artifact rather than on "
        "implementation delivery: no commit is required, one that commits is "
        "refused instead, and so is any file outside the report, the lane's "
        "git-excluded untracked .side-lane-scratch/ scratch tree, and — when "
        "this run also grants --capability playwright — untracked regular files "
        "at the lane root matching SIDE_LANE_REPORT-<name>.(png|jpg|jpeg|webp|"
        "yaml|yml|json|txt|md), at most 40 files and 20MiB each and 100MiB "
        "total. Injects the canonical report override on every host and adds "
        "report Git-write denials on Claude/Devin. Codex receives instructions "
        "and the post-run verdict, without a preventive command hook. These "
        "controls are not universal shell containment. Rejects the git-push and "
        "workflow-write capabilities; workspace-write and authorized read "
        "capabilities remain available. The branch is never "
        "published, the coordinator checkout is still checked for outside-lane "
        "writes, and an unreadable lane still fails closed. Carries no spend "
        "cap and no Stop hook — pass --report-only, which implies this option, "
        "when the Claude in-loop repair and its finite positive max_budget_usd "
        "guard are wanted. The report file is an output exception to ordinary "
        "execute rules; the lane still runs in execute mode and is not a "
        "sandbox. Ordinary execute and review lanes are unchanged",
    )
    run.add_argument(
        "--allow-no-commit",
        action="store_true",
        help="accept a lane that produced no commit; without it an execute lane that "
        "leaves work uncommitted, or changes nothing at all, exits 3 instead of "
        "reporting success",
    )
    run.add_argument(
        "--no-publish",
        action="store_true",
        help="skip pushing a delivered execute lane's branch to its remote; without "
        "it a lane that delivers pushes its branch so the commits are "
        "remote-contained instead of stranded on the launching machine. This is "
        "a decision about the RUNNER's own push: it does not forbid the worker "
        "to push the branch itself, and the audit records the two separately",
    )
    run.add_argument(
        "--no-external-publication",
        action="store_true",
        help="execute lanes only. State that the approved task's authority "
        "forbids this lane any external publication, and enforce it where the "
        "host's own controls support it: the canonical publication denials are "
        "rendered as Claude --disallowedTools rules and as Devin's native "
        "permission deny list plus its PreToolUse command policy, the worker is "
        "told the rule on every host including those that cannot deny it, and a "
        "capability whose whole grant is that publication (git-push) is refused "
        "rather than carried as a dead grant. The audit records the authority, "
        "what the host could enforce, and that the worker's own publication is "
        "otherwise unverified — neither field is evidence that no publication "
        "happened. Those deny rules are command-string rules: git -C <path>, a "
        "compound invocation, and an allowed interpreter are not fully covered "
        "by them. They are an approval-boundary seam, not a sandbox, and this "
        "option is not --no-publish alone: --no-publish governs only the "
        "runner's own push, while this guard governs the worker's publication "
        "and also suppresses the runner's own push of a delivered branch, so "
        "this guard by itself is enough and a caller does not have to remember "
        "--no-publish as well. --no-publish on its own still says nothing about "
        "the worker. A lane carrying this guard also refuses --verify, because "
        "that command is arbitrary shell running before the runner's own "
        "publication decision. Ordinary execute lanes are unchanged",
    )
    run.add_argument(
        "--existing-workspace",
        metavar="PATH",
        help="execute lanes only, on the claude, codex or devin host, and only on "
        "a route that runs on this machine — it declares "
        "execution_location: local-user-workspace, or it is a native host route "
        "that declares none — read from a private route table that opts into the "
        "local-developer profile. Run the worker inside the "
        "absolute PATH of a workspace you own — an existing checkout, which the "
        "worker uses as its own working directory — instead of a lane worktree "
        "created from HEAD. The selection is explicit: nothing defaults to it, "
        "and the shared primary checkout is entered only when its path is named "
        "here. Any staged, unstaged or untracked work already in that workspace "
        "belongs to whoever left it: this run never resets, cleans, stashes, "
        "commits or publishes it, and reports the worker's own deltas against a "
        "content-and-index baseline captured before the worker starts — content, "
        "working-tree mode, and the index's mode, oid and conflict stages, not a "
        "path list — so a file that was already dirty and was changed again, "
        "chmodded, or restaged is reported as changed, and the branch the "
        "workspace stood on is compared too. "
        "The baseline also measures the paths git already ignores — a file "
        "that was there before the worker started is judged by content like "
        "any other, so an edit to one is a delta rather than a silent change, "
        "and no ignored file's contents are ever recorded — bounded to "
        f"{WORKSPACE_IGNORED_PATH_LIMIT:,} ignored paths and "
        f"{WORKSPACE_IGNORED_BYTE_LIMIT // (1024 * 1024)} MiB: a workspace past "
        "either bound is refused before the worker starts rather than measured "
        "in part. Git's own state is compared the same way — the refs, the "
        "local config, the worktree registrations, the object counters, the "
        "reflog, .git/info, the pseudorefs and in-progress operation state, "
        "and object metadata under objects/info — so a write that leaves every "
        "path in the workspace byte-for-byte identical is still refused. "
        "Two pieces of metadata are written outside the workspace's own files, "
        "both under the repository's .git: this run's ephemeral host files (the "
        "routed Claude config directory and any per-run MCP bundle) are created "
        "in .git/side-lane-runtime/ and removed when the run ends, and the "
        "root-anchored line /.side-lane-scratch/ is appended to "
        ".git/info/exclude — a local, untracked file shared with linked "
        "worktrees — so the lane's own scratch directory, created inside the "
        "workspace, cannot appear to the dirty checks or to git add -A. Existing "
        "lines in that file are preserved; the entry is added once. "
        "The workspace is locked for the run (one create-exclusive file under "
        "the repository's .git): a second lane over the same workspace is "
        "refused, naming the holder, rather than allowed to interleave writes. "
        "A run never removes a lock file, not even one whose recorded process is "
        "gone: a stale lock is refused by name too, with the file to release by "
        "hand. "
        "Refused with --report-deliverable, --report-only, --verify and "
        "--worktree-root. Isolation is not claimed for this mode: model and "
        "credential handling are unchanged, but the workspace is the owner's own "
        "and not a dedicated lane. Default behavior is unchanged: without this "
        "flag the lane gets its own worktree",
    )
    run.add_argument(
        "--verify",
        metavar="CMD",
        help="after an execute lane delivers, run this shell command in the lane "
        "worktree (e.g. its test suite); a non-zero exit fails the run with exit "
        "code 5 even though the lane delivered. Verification does not suppress "
        "the normal publication attempt, which --no-publish and a failed push "
        "can still leave without a remote branch. It is refused with "
        "--no-external-publication: the command is arbitrary shell that runs "
        "before the runner's own publication decision, so a command that "
        "publishes would publish before this run recorded that it skipped, and "
        "nothing here filters an arbitrary command. --no-publish is unaffected",
    )
    run.add_argument(
        "--measurement-file",
        metavar="JSON",
        help="capture this run's pre-assigned delegation measurement. The file is "
        "a bounded, metadata-only JSON object (schema_version, task_id, "
        "host_family, preassigned_weight, planning_disposition, rework, and "
        "parent_task_id for a rework); it is validated before any worktree is "
        "created and published as an immutable assignment sidecar beside this "
        "run's audit before the worker starts, so an interrupted or failed lane "
        "still records what it was assigned. Omit it and the run is explicitly "
        "unmeasured. Execute mode only",
    )
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    return parser


def load_measurement(path_argument: str) -> dict[str, Any]:
    """Read and validate one bounded, metadata-only assignment record.

    The file supplies the preassignment a lane's measurement needs and nothing
    else: a task id, its host family, the weight assigned before execution, the
    planning disposition, and the rework lineage. It is deliberately not a
    place to put a prompt, a credential, or an output path — the field set is
    closed and every field is a scalar, so a file that carries anything else,
    however plausible, is refused rather than partly honoured. Validation is
    total and happens before the caller creates a worktree, so an unusable
    record cannot leave a lane behind.

    Returning a normalized record (not the caller's object) keeps the sidecar's
    contents a property of this contract instead of of the file that was read.
    """

    path = Path(path_argument).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SideLaneError(f"cannot read measurement file: {exc}") from exc
    if len(raw) > MAX_MEASUREMENT_CHARS:
        raise SideLaneError(
            f"measurement file is too large (limit {MAX_MEASUREMENT_CHARS} characters)"
        )
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise SideLaneError(f"measurement file is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise SideLaneError("measurement file must contain a JSON object")
    unknown = sorted(set(record) - MEASUREMENT_FIELDS)
    if unknown:
        raise SideLaneError(
            "measurement file has unsupported fields: " + ", ".join(unknown)
        )
    missing = sorted(MEASUREMENT_REQUIRED_FIELDS - set(record))
    if missing:
        raise SideLaneError(
            "measurement file is missing required fields: " + ", ".join(missing)
        )
    if record["schema_version"] != ASSIGNMENT_SCHEMA_VERSION:
        raise SideLaneError(
            f"measurement file schema_version must be {ASSIGNMENT_SCHEMA_VERSION}"
        )

    def text(field: str) -> str:
        value = record[field]
        if not isinstance(value, str) or not MEASUREMENT_TASK_ID.match(value):
            raise SideLaneError(
                f"measurement file {field} must be a non-empty id of letters, "
                "digits, underscores and dashes (at most 128 characters)"
            )
        return value

    task_id = text("task_id")
    host_family = record["host_family"]
    if not isinstance(host_family, str) or host_family not in MEASUREMENT_HOST_FAMILIES:
        raise SideLaneError(
            "measurement file host_family must be one of: "
            + ", ".join(sorted(MEASUREMENT_HOST_FAMILIES))
        )
    weight = record["preassigned_weight"]
    # bool is an int subclass; a JSON `true` is not weight 1.
    if not isinstance(weight, int) or isinstance(weight, bool) or weight not in MEASUREMENT_WEIGHTS:
        raise SideLaneError(
            "measurement file preassigned_weight must be one of: "
            + ", ".join(str(item) for item in sorted(MEASUREMENT_WEIGHTS))
        )
    disposition = record["planning_disposition"]
    if not isinstance(disposition, str) or disposition not in MEASUREMENT_PLANNING_DISPOSITIONS:
        raise SideLaneError(
            "measurement file planning_disposition must be one of: "
            + ", ".join(sorted(MEASUREMENT_PLANNING_DISPOSITIONS))
        )
    rework = record["rework"]
    if not isinstance(rework, bool):
        raise SideLaneError("measurement file rework must be a boolean")
    parent = record.get("parent_task_id")
    if rework:
        if not isinstance(parent, str) or not MEASUREMENT_TASK_ID.match(parent):
            raise SideLaneError(
                "measurement file parent_task_id is required for a rework and "
                "must be a non-empty id of letters, digits, underscores and "
                "dashes (at most 128 characters)"
            )
        if parent == task_id:
            raise SideLaneError(
                "measurement file parent_task_id must differ from task_id"
            )
    elif parent is not None:
        raise SideLaneError(
            "measurement file parent_task_id is only meaningful for a rework"
        )
    return {
        "task_id": task_id,
        "host_family": host_family,
        "preassigned_weight": weight,
        "planning_disposition": disposition,
        "rework": rework,
        "parent_task_id": parent,
    }


def load_recommendation_profile(path_argument: str) -> dict[str, Any]:
    path = Path(path_argument).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SideLaneError(f"cannot read recommendation profile: {exc}") from exc
    if len(raw) > MAX_PROFILE_CHARS:
        raise SideLaneError("recommendation profile is too large")
    try:
        profile = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SideLaneError(f"recommendation profile is not valid JSON: {exc}") from exc
    if not isinstance(profile, dict):
        raise SideLaneError("recommendation profile must be a JSON object")
    forbidden = re.compile(
        r"(?:api[_-]?key|credential|secret|quota|billing|usage"
        r"|verified|snapshot|routed[_-]?policy)", re.I
    )
    pending: list[object] = [profile]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if forbidden.search(str(key)):
                    raise SideLaneError(
                        f"recommendation profile contains prohibited field: {key}"
                    )
                pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    return profile


def load_routed_policy_snapshot(path_argument: str) -> dict[str, Any]:
    """Load one operator-reviewed routed-pool policy snapshot file.

    The file is receipt data (selector, member set, revision, fingerprint,
    caps, observation timestamp, evidence provenance) — hashing an arbitrary
    file is not a signature, so this function verifies syntax only and never
    attests live server state.
    """

    path = Path(path_argument).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SideLaneError(f"cannot read routed policy snapshot: {exc}") from exc
    if len(raw) > MAX_PROFILE_CHARS:
        raise SideLaneError("routed policy snapshot is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SideLaneError(f"routed policy snapshot is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SideLaneError("routed policy snapshot must be a JSON object")
    return payload


def load_routed_policy_snapshots(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Load every operator-supplied ``--routed-policy-snapshot`` file, in order.

    ``_recommend`` reads the parsed option here and the private report-intent
    entrypoint's absent-target path calls this same function with the same
    parsed namespace.  One reader means a named snapshot file is judged by one
    rule on both paths: a path that cannot be read, is over-large, is not JSON,
    or is not a JSON object is the caller's own rejection rather than an answer
    about the catalog.  Order is preserved, and nothing here attests the live
    policy the file describes — see :func:`load_routed_policy_snapshot`.
    """

    snapshot_args = getattr(args, "routed_policy_snapshot", None)
    if isinstance(snapshot_args, str):
        snapshot_args = [snapshot_args]
    if not isinstance(snapshot_args, (list, tuple)):
        snapshot_args = []
    return [
        load_routed_policy_snapshot(snapshot_path)
        for snapshot_path in snapshot_args
    ]


def _ready_routes(config: Mapping[str, Any]) -> frozenset[tuple[str, str, str, str]]:
    present: set[tuple[str, str, str, str]] = set()
    configured_hosts = {
        host
        for item in config["providers"].values()
        for hosts in item.get("routes", {}).values()
        for host in hosts
    }
    statuses = {
        host: auth_status(host, executable=_host_executable(host))
        for host in configured_hosts
    }
    for provider, item in config["providers"].items():
        if item.get("auth_method") == "oauth":
            ready_hosts = {host for host, status in statuses.items() if status.ready}
        else:
            ready_hosts = (
                configured_hosts
                if credential_present(item["credential_service"])
                else set()
            )
        for mode, hosts in item.get("routes", {}).items():
            for host, route in hosts.items():
                if host in ready_hosts:
                    present.update(
                        (provider, host, mode, model)
                        for model in route.get("models", [])
                    )
    return frozenset(present)


def validate_recommendation_request(
    config: Mapping[str, Any], profile: Mapping[str, Any]
) -> tuple[str, str, list[str], list[str]]:
    """Validate what a recommendation request asks for, on its own terms.

    These are the checks that belong to the *request* rather than to a route:
    which host originates it, which mode it runs in, and which connectors and
    capabilities it requires.  Ordering matters — they run before
    ``_capability_report`` — and they are deliberately pure: ``config`` is
    already loaded, and nothing here reads a host executable, an MCP
    registration, a credential store, a provider, or a selector policy.  A
    caller that cannot reach the recommender at all (the private report-intent
    entrypoint's absent-target path) can therefore still refuse a malformed
    request in the core's own wording and status, instead of answering a
    question the caller never asked.

    Returns the validated ``(host, mode, required_connectors,
    required_capabilities)`` so no caller re-derives them.
    """

    host = profile.get("coordinator_host", profile.get("originating_host"))
    mode = profile.get("mode")
    if host not in {"codex", "claude"} or mode not in {"review", "execute"}:
        raise SideLaneError("recommendation profile requires coordinator_host and mode")
    required_connectors, required_capabilities = (
        profile.get("required_connectors", []),
        profile.get("required_capabilities", []),
    )
    for label, value in (
        ("required_connectors", required_connectors),
        ("required_capabilities", required_capabilities),
    ):
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SideLaneError(f"{label} must be a list of non-empty strings")
    unknown = sorted(set(required_capabilities) - set(config["capabilities"]))
    if unknown:
        raise SideLaneError(f"unknown capabilities: {', '.join(unknown)}")
    return host, mode, required_connectors, required_capabilities


def _recommend(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    repo = validate_governance(args.repo)
    profile = load_recommendation_profile(args.profile)
    host, mode, required_connectors, required_capabilities = (
        validate_recommendation_request(config, profile)
    )
    candidate_hosts = tuple(
        sorted(
            {
                candidate_host
                for item in config["providers"].values()
                for hosts in item.get("routes", {}).values()
                for candidate_host in hosts
            }
        )
    )
    snapshots = {
        candidate_host: _capability_report(
            config, candidate_host, mode, None, None, repo
        )
        for candidate_host in candidate_hosts
    }
    normalized = dict(profile)
    normalized.update(
        {
            "coordinator_host": host,
            "required_connectors": sorted(set(required_connectors)),
            "required_capabilities": sorted(set(required_capabilities)),
            "host_capabilities": {
                candidate_host: _recommendation_host_snapshot(report, mode)
                for candidate_host, report in snapshots.items()
            },
        }
    )
    policy_snapshots = load_routed_policy_snapshots(args)
    catalog = routing.load_catalog()
    collection_evidence: list[dict[str, Any]] = []
    if not policy_snapshots:
        # Automatic mode: a routed provider block that sets
        # ``automatic_selector_policy`` opts into fresh authenticated
        # collection instead of an operator-supplied file. Collection
        # failure is fail-closed — no snapshot is passed, so the pool
        # route stays ineligible; nothing falls back to stale file data
        # or to a legacy/direct scorer path. Non-routed providers are
        # never touched.
        for route in catalog.get("routes", []):
            pool_spec = route.get("routed_pool")
            if not isinstance(pool_spec, Mapping):
                continue
            provider_config = config["providers"].get(route["provider"])
            if (
                not isinstance(provider_config, Mapping)
                or provider_config.get(
                    selector_policy.AUTOMATIC_COLLECTION_FLAG
                ) is not True
            ):
                continue
            selector = pool_spec.get("selector")
            try:
                collected = selector_policy.collect_selector_policy(
                    route, provider_config, now_utc=datetime.now(timezone.utc)
                )
            except selector_policy.SelectorPolicyError as exc:
                collection_evidence.append(
                    {
                        "selector": selector,
                        "source": "automatic-selector-policy-collector",
                        "error": exc.reason,
                    }
                )
            else:
                policy_snapshots.append(collected["snapshot"])
                collection_evidence.append(collected["evidence"])
    result = routing.recommend(
        catalog,
        normalized,
        runtime_allowlist=routing.allowlist_from_models(config),
        credential_present_routes=_ready_routes(config),
        routed_contracts=routing.routed_contracts_from_models(config),
        routed_policy_snapshots=policy_snapshots,
    )
    result.update(
        {
            "presence_only": True,
            "originating_host_unchanged": True,
            "required_capabilities": sorted(required_capabilities),
        }
    )
    if collection_evidence:
        result["routed_policy_collection"] = collection_evidence
        if any("error" in item for item in collection_evidence):
            result["reason_codes"] = result["reason_codes"] + [
                "routed-policy-collection-failed"
            ]
    # Presence-only staffing eligibility must never read as verified scope, so
    # the label ships with the output rather than only living in the snapshot
    # that routing normalizes back down to names. It belongs to a task that
    # actually requires the capability: a registered ``cm-services`` on an
    # unrelated task must not carry a pending-verification label nobody asked
    # for.
    if (
        "gateway-read" in normalized["required_capabilities"]
        and any(
            "gateway-read" in snapshot["available_capabilities"]
            for snapshot in normalized["host_capabilities"].values()
        )
    ):
        result["gateway_read_pending_verification"] = GATEWAY_READ_PENDING_VERIFICATION
    # The same label for every other capability the run actually requires whose
    # availability rests on registration alone. It travels with the printed
    # recommendation rather than only inside the snapshot routing normalizes
    # down to names, so a reader cannot mistake a registered server for
    # authenticated access — and it stays scoped to a capability this task
    # requires, so an unrelated registered server carries no label nobody asked
    # for.
    presence_only = sorted(
        name
        for name in normalized["required_capabilities"]
        if any(
            name in snapshot.get("presence_only_capabilities", ())
            for snapshot in normalized["host_capabilities"].values()
        )
    )
    if presence_only:
        result["presence_only_capabilities"] = presence_only
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


#: The label the printed recommendation carries for `gateway-read` when its
#: staffing availability rests on connector-registration presence alone.
#: Registration is not authentication: this text travels beside the promoted
#: names so a reader cannot mistake a registered `cm-services` server for
#: verified access, and dispatch still runs the wrapper's own target preflight
#: and the host's native readiness check before any gateway call. It is the
#: first of the presence-only labels rather than a capability-specific
#: framework: every other merely-present capability the task requires is named
#: in the generic `presence_only_capabilities` list beside it, from the same
#: evidence-driven promotion.
GATEWAY_READ_PENDING_VERIFICATION = (
    "gateway-read staffing availability is cm-services connector-registration "
    "presence only; live authentication and the exact granted run-id scope "
    "remain pending dispatch, and the wrapper target preflight and the host's "
    "native readiness check remain mandatory"
)


def _recommendation_host_snapshot(
    report: Mapping[str, Any], mode: str
) -> dict[str, Any]:
    """Translate presence evidence narrowly for offline route staffing.

    A capability whose server the worker host has actually registered is enough
    for an execute recommendation to staff a route that requires it, because
    dispatch performs a live readiness check and the route catalog still
    requires its own local evaluation and connector evidence. One rule covers
    the whole class: the capability must be a known typed MCP capability, the
    host must list the one exact server it maps to in its registration
    inventory, and the capability's own evidence state must be ``present``.
    "List" means the host's own matching rule — the Codex connector-name rule
    for the code-graph connectors, which is what a Codex lane really inherits,
    and the exact server name for every other capability, whose grants embed it
    — so the promotion and the evidence that admits a capability agree by
    construction rather than by two readings of "registered". A report that
    names no host, as only a synthetic one does, is matched exactly.

    This repairs a false negative rather than broadening a grant. The promotion
    is derived from the capability-to-server mapping the adapters already use,
    so a newly declared typed capability is included without a new hard-coded
    entry, and a merely-present name with no typed mapping — or one whose server
    is registered somewhere else — is still not available. ``capabilities``
    itself is never touched: its booleans stay verified-only, and registration
    is never read as verification. Review lanes expose no MCP server of any
    kind, so review mode drops every capability the canonical mapping delivers
    as one, from the per-run remote servers as much as from the user-scope
    ones, even from a report that claimed one verified.
    """

    connectors = report.get("mcp_connectors", [])
    capabilities = report.get("capabilities", {})
    evidence = report.get("capability_evidence", {})
    host = report.get("host")
    if not isinstance(connectors, list) or not isinstance(capabilities, Mapping):
        raise SideLaneError("capability report is malformed")
    available = {name for name, state in capabilities.items() if state}
    presence_only: set[str] = set()
    if mode != "execute":
        # Every key of the canonical capability-to-server mapping, not only the
        # user-scope subset: ``aws-read`` and ``omniroute-read`` are delivered
        # by the per-run config, and a review lane's strict argv exposes no MCP
        # server either way. Treating a review worker as satisfying one would
        # staff a route on a capability its own launch cannot grant.
        available.difference_update(RUN_MCP_CAPABILITY_SERVERS)
    else:
        registered = {name for name in connectors if isinstance(name, str)}
        for name in sorted(USER_SCOPE_MCP_CAPABILITIES):
            if not isinstance(evidence, Mapping):
                break
            item = evidence.get(name)
            if not isinstance(item, Mapping) or item.get("state") != "present":
                continue
            server = RUN_MCP_CAPABILITY_SERVERS.get(name)
            if server is None or not _connector_name_registered(
                server,
                host,
                registered,
                # A codex lane serves these through a near-name registration;
                # their evidence is the same connector-name rule, so the
                # promotion honours it too. The cm-services family and Slack
                # keep the exact name their grants embed on every host, which
                # also keeps a Codex native ``gcloud``/``psql`` on PATH from
                # standing in for a bridge that is not registered.
                require_exact=name not in CODEX_CONNECTOR_NAME_CAPABILITIES,
            ):
                continue
            if name not in available:
                presence_only.add(name)
            available.add(name)
    return {
        "available_connectors": sorted(connectors) if mode == "execute" else [],
        "available_capabilities": sorted(available),
        # The names above whose availability rests on registration alone, so a
        # caller can label them without having to re-derive why each was
        # admitted. A name the verified map already carried never appears here.
        "presence_only_capabilities": sorted(presence_only),
    }


def _evaluate(path_argument: str) -> int:
    path = Path(path_argument).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SideLaneError(f"cannot read evaluation input: {exc}") from exc
    if len(raw) > MAX_PROFILE_CHARS:
        raise SideLaneError("evaluation input is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SideLaneError(f"evaluation input is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SideLaneError("evaluation input must be a JSON object")
    runs = payload.get("evaluation_runs", [])
    signals = payload.get("community_signals", [])
    if not isinstance(runs, list) or not isinstance(signals, list):
        raise SideLaneError("evaluation_runs and community_signals must be arrays")
    result = {
        "evaluation_aggregates": evaluation.aggregate_runs(runs) if runs else [],
        "community_aggregates": evaluation.summarize_community_signals(signals),
        "provider_calls_performed": False,
        "credentials_accessed": False,
        "activation_performed": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def keychain_present(service: str) -> bool:
    return credential_present(service)


def read_keychain_secret(service: str) -> str:
    return read_credential(service)


def _host_executable(host: str) -> str | None:
    """Resolve the native host executable; a broken explicit override is a CLI error."""

    try:
        return resolve_host_executable(host, which=shutil.which)
    except HostExecutableError as exc:
        raise SideLaneError(str(exc)) from exc


def _require_host_executable(host: str) -> str:
    try:
        return require_host_executable(host, which=shutil.which)
    except HostExecutableError as exc:
        raise SideLaneError(str(exc)) from exc


def _capability_report(
    config: Mapping[str, Any],
    host: str,
    mode: str,
    provider: str | None,
    model: str | None,
    repo: Path | None = None,
) -> dict[str, Any]:
    runtime = _host_executable(host)
    mcp_names, out_of_scope = _discover_mcp_inventory(host, repo)
    lowered = {name.lower() for name in mcp_names}
    evidence = {
        "workspace-write": {
            "state": "verified" if mode == "execute" else "unavailable",
            "basis": "active lane mode",
        },
        "shell": {
            "state": "verified" if runtime else "unavailable",
            "basis": "selected host executable",
        },
        "git-push": {
            "state": "present" if shutil.which("git") else "unavailable",
            "basis": "git executable; remote write authority not tested",
        },
        "gitnexus": _graph_connector_evidence(
            "gitnexus", mcp_names, host, out_of_scope
        ),
        "slack-read": _slack_read_evidence(mcp_names, host, out_of_scope),
        "asana-read": _cm_services_evidence("asana-read", mcp_names, host, out_of_scope),
        "drive-read": _cm_services_evidence("drive-read", mcp_names, host, out_of_scope),
        "gcloud-read": _service_read_evidence(
            "gcloud-read", host, mcp_names, out_of_scope, "gcloud"
        ),
        "database-read": _service_read_evidence(
            "database-read", host, mcp_names, out_of_scope, "psql"
        ),
        "algolia-read": _cm_services_evidence(
            "algolia-read", mcp_names, host, out_of_scope
        ),
        "contentful-read": _cm_services_evidence(
            "contentful-read", mcp_names, host, out_of_scope
        ),
        "contentful-master-read": _cm_services_evidence(
            "contentful-master-read", mcp_names, host, out_of_scope
        ),
        "gateway-read": _cm_services_evidence(
            "gateway-read", mcp_names, host, out_of_scope
        ),
        "codegraph": _graph_connector_evidence(
            "codegraph", mcp_names, host, out_of_scope
        ),
        "secret-use": {
            "state": "unknown",
            "basis": "credential values and access are never tested during preflight",
        },
        "workflow-write": {
            "state": "present"
            if any(
                marker in name
                for name in lowered
                for marker in ("asana", "slack", "teams", "github")
            )
            else "unknown",
            "basis": "connector-name metadata only; write authority not tested",
        },
        "playwright": {
            "state": "present" if "playwright" in mcp_names else "unavailable",
            "basis": "exact connector-name metadata only, from the host's user-global config and this repository's project config; other projects' entries are excluded; browser launch not tested; review mode hides all MCP servers",
        },
    }
    report: dict[str, Any] = {
        "host": host,
        "mode": mode,
        "runtime": runtime,
        "host_support_dir": host_support_dir(host, runtime),
        "route": "not-requested",
        "mcp_connectors": sorted(mcp_names),
        "mcp_connectors_out_of_scope": sorted(out_of_scope),
        # Registration evidence, not capability evidence: it says which files
        # were consulted and what each contributed. An empty ``mcp_connectors``
        # caused by three ``missing`` files is an unregistered host; the same
        # empty list beside a ``no-servers`` file means this scanner did not
        # find the container it looks for and the registration is unproven
        # either way. Neither reading of the file is a qualified capability.
        "mcp_registration_sources": _mcp_registration_sources(host, repo),
    }
    if bool(provider) != bool(model):
        raise SideLaneError("provider and model must be supplied together")
    if provider and model:
        provider_config, route = select_route(config, host, mode, provider, model)
        report.update(
            {
                "route": "configured",
                **{key: route[key] for key in ("gateway", "auth_method", "billable")},
            }
        )
        if route["auth_method"] == "oauth":
            report["auth"] = auth_status(host, executable=runtime).as_dict()
        else:
            report["configured_override"] = (
                "present"
                if credential_present(provider_config["credential_service"])
                else "absent"
            )
            report["requires_one_run_approval"] = True
    report["capability_evidence"] = {
        name: evidence.get(name, {"state": "unknown", "basis": "no evidence"})
        for name in config["capabilities"]
    }
    report["capabilities"] = {
        name: report["capability_evidence"][name]["state"] == "verified"
        for name in config["capabilities"]
    }
    return report


def _slack_read_evidence(
    mcp_names: set[str],
    host: str,
    out_of_scope: set[str] = frozenset(),
) -> dict[str, str]:
    """Registration evidence for the execute-only Slack read capability.

    The capability grants exactly ``mcp__slack__slack_read_thread`` and
    ``mcp__slack__slack_read_channel``, whose IDs embed the server name
    ``slack`` exactly, so the registration check demands the exact name on
    every host, Codex included: unlike the graph capabilities, a Codex
    near-miss registration (``slack-mcp``) is ``name-mismatch`` and fails the
    launch gate, because the grants still embed ``slack`` exactly. A
    ``present`` registration is registration evidence only — Slack
    authentication and a live read stay unproven until the worker observes
    the exact granted tool names, and that separation is stated in every
    basis this function returns.
    """

    evidence = _graph_connector_evidence(
        "slack", mcp_names, host, out_of_scope, require_exact=True
    )
    if evidence["state"] == "present":
        return {
            "state": "present",
            "basis": evidence["basis"]
            + "; Slack authentication and a live read are not tested; grants are the exact "
            "read-only tools slack_read_thread and slack_read_channel",
        }
    return evidence


def _cm_services_evidence(
    capability: str,
    mcp_names: set[str],
    host: str,
    out_of_scope: set[str] = frozenset(),
) -> dict[str, str]:
    """Registration evidence for a ``cm-services`` read capability.

    Every cm-services-family capability (asana, drive, gcloud, database,
    algolia, contentful, Gateway) grants exact ``mcp__cm-services__<tool>``
    IDs, so the tool IDs embed the server name ``cm-services`` exactly on
    every host — like ``slack-read`` the check demands the exact name (a
    near-miss is ``name-mismatch``). The server is a fixed local stdio
    registration the coordinator provisions into the worker host's
    user-global config under the same account the capability reads; a
    ``present`` registration is presence evidence only — same-account
    provisioning, service authentication, and a live read stay unproven until
    the worker observes the exact granted tool names.
    """

    evidence = _graph_connector_evidence(
        "cm-services", mcp_names, host, out_of_scope, require_exact=True
    )
    if evidence["state"] == "present":
        return {
            "state": "present",
            "basis": evidence["basis"]
            + "; same-account provisioning, service authentication, and a live "
            f"read are not tested; the {capability} grant is the exact "
            "read-only mcp__cm-services__ tools listed in canonical governance",
        }
    return evidence


def _service_read_evidence(
    capability: str,
    host: str,
    mcp_names: set[str],
    out_of_scope: set[str],
    executable: str,
) -> dict[str, str]:
    """Prefer the cloud ``cm-services`` bridge, retaining Codex CLI paths.

    Claude and Devin receive exact MCP grants for these service capabilities;
    their local executable presence never proves a usable tool path. Codex
    lanes historically use the native ``gcloud``/``psql`` path, so retain
    that presence-only admission when no fixed bridge is registered there.
    """

    evidence = _cm_services_evidence(capability, mcp_names, host, out_of_scope)
    if evidence["state"] == "present" or host != "codex":
        return evidence
    if shutil.which(executable):
        return {
            "state": "present",
            "basis": f"native {executable} executable; account/project access not tested",
        }
    return evidence


def _connector_name_registered(
    server: str, host: str | None, mcp_names: set[str], *, require_exact: bool
) -> bool:
    """Whether ``mcp_names`` registers ``server`` under the rule ``host`` uses.

    Codex inherits its configured MCP servers directly, so a connector whose
    name merely contains the server name (``gitnexus-local``) is the one the
    lane gets — unless ``require_exact`` says the capability's own grants embed
    the exact name on every host. Every other host, and every other capability,
    needs the exact name: the tool IDs its grants render embed it.

    The single implementation of that rule, so the evidence a capability is
    admitted on and the staffing promotion that reads it cannot drift apart.
    An unknown host is matched exactly, which is the stricter reading.
    """

    if host == "codex" and not require_exact:
        return any(server in name.lower() for name in mcp_names)
    return server in mcp_names


def _graph_connector_evidence(
    capability: str,
    mcp_names: set[str],
    host: str,
    out_of_scope: set[str] = frozenset(),
    require_exact: bool = False,
) -> dict[str, str]:
    """Presence evidence for a code-graph connector.

    The Claude and Devin execute adapters render the fixed ``mcp__<capability>__*``
    grants, whose tool IDs embed the configured server name
    exactly. On those hosts only a server registered under the exact name is
    callable; one that merely contains the word (``gitnexus-local``) would pass
    a substring check and then receive no usable grant, so it is reported as
    ``name-mismatch`` and fails the launch gate like any non-present state.
    Codex lanes inherit their configured MCP servers directly with no such
    allowlist, so connector-name presence remains the evidence there — unless
    ``require_exact`` is set, which a capability whose grants embed the server
    name even on Codex (``slack-read``) uses to demand the exact registration
    on every host. ``_connector_name_registered`` owns that distinction, and
    ``CODEX_CONNECTOR_NAME_CAPABILITIES`` names the capabilities it exempts.
    """

    inherited = host == "codex" and not require_exact
    if _connector_name_registered(capability, host, mcp_names, require_exact=require_exact):
        if inherited:
            return {
                "state": "present",
                "basis": "connector-name metadata only; Codex lanes inherit configured MCP servers directly",
            }
        return {
            "state": "present",
            "basis": f"connector registered under the exact name {capability!r}; tool access not tested",
        }
    if inherited:
        return {
            "state": "unknown",
            "basis": "connector-name metadata only; see mcp_registration_sources for which registration files were consulted and what each contributed",
        }
    if capability in out_of_scope:
        return {
            "state": "unknown",
            "basis": f"{capability!r} is registered only under another project's scope in the host user config; a lane worktree does not inherit it — register it user-globally or in this repository's .mcp.json",
        }
    similar = sorted(name for name in mcp_names if capability in name.lower())
    if similar:
        return {
            "state": "name-mismatch",
            "basis": f"connector(s) {', '.join(repr(name) for name in similar)} found but grants target mcp__{capability}__*; register the server as {capability!r}",
        }
    return {
        "state": "unknown",
        "basis": "connector-name metadata only; see mcp_registration_sources for which registration files were consulted and what each contributed",
    }


def _discover_mcp_names(host: str, repo: Path | None = None) -> set[str]:
    """Connector names a lane launched for ``repo`` on ``host`` can actually see."""

    return _discover_mcp_inventory(host, repo)[0]


def _mcp_registration_paths(
    host: str, repo: Path | None = None
) -> list[tuple[str, Path]]:
    """Return ``(scope, path)`` for every MCP registration file ``host`` reads.

    Thin delegation to
    :func:`side_lane.mcp_run_config.registration_paths` — the single source,
    also used by the per-run ``--mcp-config`` name-conflict check — with the
    default environment resolution (this process's own view).
    """

    return registration_paths(host, repo)


def _require_inherited_project_mcp_config(host: str, repo: Path) -> None:
    """Refuse project registrations that a fresh lane worktree cannot inherit.

    A dedicated lane is created from HEAD. Its worker will see tracked project
    registration files as committed, but not untracked, ignored, staged, or
    modified copies in the coordinator checkout. Reading those copies for
    admission would claim a connector the worker does not have (or miss one it
    does). Existing-workspace lanes instead read their actual worker directory.
    """

    for scope, path in _mcp_registration_paths(host, repo):
        if scope not in {"project", "local"}:
            continue
        relative = path.relative_to(repo)
        result = subprocess.run(
            [
                "git", "-C", str(repo), "status", "--porcelain=v1",
                "--untracked-files=all", "--ignored=matching", "--", str(relative),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise SideLaneError(
                f"cannot verify project MCP registration {relative} against HEAD"
            )
        if result.stdout:
            raise SideLaneError(
                f"project MCP registration {relative} differs from HEAD and "
                "will not be inherited by the new lane worktree; commit the "
                "registration or use an authorized per-run MCP config"
            )


def _mcp_registration_sources(
    host: str, repo: Path | None = None
) -> list[dict[str, str]]:
    """Report each consulted registration file and what it actually contributed.

    An absent registration file and a present file that yields no connector
    name both leave ``mcp_connectors`` empty, and they need different repairs:
    the first is an unregistered host, while the second means the file's shape
    was not recognised or its definitions are scoped elsewhere. Naming each file
    with its disposition keeps an empty inventory from being read as either one
    on no evidence — in particular a present file reported ``no-servers`` is the
    signal that this scanner did not find the container it looks for, not proof
    that the host has nothing registered.

    ``missing`` is a genuine absence and only that: the probe is the shared
    strict ``stat`` (``_registration_is_file``), because ``Path.is_file()``
    answers False for any ``OSError`` — an unsearchable parent directory among
    them — and would report a registry this run simply could not reach as one
    that is not there. Every other ``OSError`` reaches the ``unreadable``
    disposition beside it.
    """

    sources: list[dict[str, str]] = []
    for scope, path in _mcp_registration_paths(host, repo):
        source = {"scope": scope, "path": str(path)}
        try:
            if not _registration_is_file(path):
                sources.append({**source, "state": "missing"})
                continue
            if path.suffix == ".toml":
                # Flat TOML tables carry no scope of their own.
                in_scope, declared = toml_mcp_names(path), set()
            else:
                # Same shape rule as the worker-side inventory, so the
                # disposition reported here is the registry the child loads.
                # The one ``projects.<path>`` entry judged here is the
                # directory this run names, the same path
                # ``_mcp_registration_paths`` resolved the file from: an
                # unrelated project's malformed container is not a registry
                # the child opens, and it stays attributed to its own path
                # below rather than refusing this file. The in-scope filter is
                # unchanged and still counts root scope alone, so naming that
                # entry narrows the shape rule only — it does not promote a
                # coordinator's project registration into the lane's inventory.
                scopes = json_registration_scopes(
                    host, path, selected_project=_selected_project(repo)
                )
                declared = set(scopes)
                in_scope = {name for name, keys in scopes.items() if () in keys}
        except OSError:
            sources.append({**source, "state": "unreadable"})
            continue
        except ValueError:
            sources.append({**source, "state": "unparsed"})
            continue
        if in_scope:
            sources.append({**source, "state": "registered"})
        elif declared:
            sources.append({**source, "state": "out-of-scope-only"})
        else:
            sources.append({**source, "state": "no-servers"})
    return sources


def _discover_mcp_inventory(
    host: str, repo: Path | None = None
) -> tuple[set[str], set[str]]:
    """Return ``(in_scope, out_of_scope)`` connector names for a lane on ``host``.

    Claude reads MCP servers from the root-level ``mcpServers`` of its user
    config files and from the ``.mcp.json`` of the directory it is launched in.
    That ``.mcp.json`` carries its registrations as its own top-level keys when
    it declares no ``mcpServers`` object — the shape the adapter's own
    registration merge and the worker's inventory both read, and the one
    ``json_registration_scopes`` applies here, so this scan names exactly the
    servers the child loads.
    Its user config also carries per-project entries (``projects.<path>.
    mcpServers``) keyed by the directory Claude was started in; a lane runs in a
    fresh worktree, so no such entry applies to it. Those names are reported
    separately as out of scope instead of being unioned into the inventory.
    Codex reads flat TOML tables and has no per-project layer in its user config.
    Devin reads its own three registration files, which ``devin mcp add --help``
    documents as user, project and local scope.

    A registration file that exists but cannot be read or parsed refuses the
    scan rather than contributing nothing. The worker-side reader
    (:func:`side_lane.mcp_run_config.host_registered_server_names`) already
    fails closed on the same file, and an empty contribution would make a
    *broken* registry indistinguishable from a *missing* connector — the
    report would say ``unknown`` and the launch gate would claim the
    capability is unavailable, an absence claim the evidence does not support.
    The probe is the same shared strict ``stat`` for the same reason: an
    unreadable registry is not an absent one, so it refuses here rather than
    being skipped as if the host had nothing registered.

    The shape rule covers one ``projects.<path>`` entry — the directory this
    run names, which is the path the registration file was resolved from — so a
    malformed container under an unrelated project does not refuse a registry
    the child never opens. The names are still scoped by root alone: an entry's
    names land in ``out_of_scope``, never in this lane's inventory.
    """

    names: set[str] = set()
    out_of_scope: set[str] = set()
    for _scope, path in _mcp_registration_paths(host, repo):
        try:
            if not _registration_is_file(path):
                continue
            if path.suffix == ".toml":
                names.update(toml_mcp_names(path))
                continue
            # The worker's own reader and the adapter's registration merge both
            # read Claude's worktree ``.mcp.json`` as its top-level keys when it
            # declares no ``mcpServers`` container; a scan that knew only the
            # container would report no connector for a server the child loads,
            # and the launch gate would then refuse the capability as absent.
            for name, scopes in json_registration_scopes(
                host, path, selected_project=_selected_project(repo)
            ).items():
                if () in scopes:
                    names.add(name)
                else:
                    out_of_scope.add(name)
        except (OSError, ValueError) as exc:
            # Fail closed, as the per-run name-conflict check and the worker's
            # own registry reader do: refusing names the file the operator has
            # to fix, where a silent empty inventory misreports it as a
            # connector that was never registered.
            raise SideLaneError(
                f"cannot read host MCP registration {path}: {exc}"
            ) from exc
    return names, out_of_scope - names


#: Execute lane finished without its work reaching git. Distinct from the
#: worker's own non-zero exit and from argparse's 2.
LANE_NOT_DELIVERED = 3

#: Lane tree could not be inspected at all. Fail closed rather than claim a
#: delivery nobody verified.
LANE_DELIVERY_UNVERIFIED = 4

#: A delivered lane failed its caller-supplied verification command
#: (``--verify``). The lane reached git and its branch was still published;
#: the work itself does not pass its own checks. Distinct from
#: LANE_NOT_DELIVERED (nothing landed to inspect) and from the worker's own
#: non-zero exit (the work never claimed to be finished).
LANE_VERIFY_FAILED = 5

#: The coordinator checkout changed during an execute run. Same-user
#: execution is not an OS sandbox, so a worker CAN write outside its lane —
#: observed 2026-09-19, when a worker's report landed in the coordinator
#: source path while the run reported a clean accepted delivery. The lane's
#: own work is left untouched; the run fails so nobody mistakes a mutated
#: source checkout for an accepted lane.
LANE_SOURCE_MUTATED = 6

# Operator wording for the report-only gate's outcome. ``report_freshness_state``
# returns the key; "current" never reaches this table because it is the only
# state that is delivered.
REPORT_STATE_DETAIL = {
    "unusable": (
        "it is missing, empty, whitespace-only, or not a plain file"
    ),
    "stale": (
        "the file there is unchanged from what the lane already contained when "
        "it started, so this run did not write it"
    ),
    "unverified": "no run baseline was captured, so nothing can be claimed about it",
}

#: Lane-relative prefix a report-only lane may leave uncommitted besides the
#: report artifact and, when this run granted ``playwright``, the browser-report
#: artifacts below: the lane's git-excluded scratch tree, the
#: governance-documented home for throwaway scripts, notes, and intermediate
#: output. Git normally never reports it, so this is a guard for the case where
#: that exclusion is missing — the alternative is failing a lane over a path
#: the rules told it to use. The prefix is only half the rule: see the status
#: requirement below.
REPORT_ONLY_SCRATCH_PREFIX = f"{SCRATCH_DIR_NAME}/"

#: The browser-report artifact namespace, the same rule and the same caps the
#: cloud worker's own collector applies. A report lane granted ``playwright``
#: saves screenshots and page dumps at the lane root beside its report, and both
#: halves of the pipeline have to agree on what an allowed artifact is: the
#: cloud half's collector and this half's delivery verdict are fed by the same
#: worker output. ``tests/fixtures/report_artifact_names.json`` carries the one
#: accept/refuse table both halves assert against, so a change to the namespace
#: or its caps on one side fails on the other instead of silently drifting.
BROWSER_REPORT_ARTIFACT_RE = re.compile(
    r"^SIDE_LANE_REPORT-[A-Za-z0-9_-]+\.(png|jpg|jpeg|webp|yaml|yml|json|txt|md)$"
)
BROWSER_ARTIFACT_MAX_FILES = 40
BROWSER_ARTIFACT_MAX_BYTES = 20 * 1024 * 1024
BROWSER_ARTIFACT_MAX_TOTAL_BYTES = 100 * 1024 * 1024

#: Why each capability that carries explicit write authority is refused on a
#: report run. The list of refusals is canonical
#: (`governance.report_forbidden_write_capabilities`, read from the declaration
#: line in the `Report deliverable` section); this maps each one to the
#: operator-facing sentence naming the option they can look up.
REPORT_WRITE_CAPABILITY_REFUSALS = {
    "git-push": (
        "a report run never publishes, so it is not granted --capability "
        "git-push: the lane's deliverable is its report artifact and it "
        "makes no commit. Drop the capability, or run an ordinary execute "
        "lane that publishes its branch."
    ),
    "workflow-write": (
        "a report run makes no external write, so it is not granted "
        "--capability workflow-write: the lane's deliverable is its report "
        "artifact, and the workflow or messaging exemption this capability "
        "grants is not part of that contract. Drop the capability, or run an "
        "ordinary execute lane whose approved task names the exact update and "
        "recipient or object."
    ),
}


#: How each host can enforce a task no-external-publication guard, as this
#: runner can honestly record it. Claude and Devin each carry a deny seam the
#: canonical publication rules are rendered into; Codex has none, so its guard
#: is instruction only. No value here is a claim of an operating-system
#: sandbox, and "instruction only" is recorded as exactly that rather than as
#: enforcement the host never had.
PUBLICATION_ENFORCEMENT_BY_HOST = {
    "claude": (
        "canonical publication rules rendered as --disallowedTools denials; "
        "command-string matching, not shell containment"
    ),
    "devin": (
        "canonical publication rules rendered into the native permission deny "
        "list and the PreToolUse command policy; command-string matching, not "
        "shell containment"
    ),
    "codex": (
        "instruction only: this host runs danger-full-access with no deny seam, "
        "so the refusal is never prevention"
    ),
}


def _is_browser_report_artifact_name(name: object) -> bool:
    """True when ``name`` is exactly one allowed artifact name at the lane root.

    One path component only: a name carrying a separator could name a file
    outside the lane root, and this namespace's artifacts are root-level by
    definition. ``fullmatch`` rather than ``search``, so a name with anything
    before or after the pattern — a prefix path, a trailing space, a newline —
    is refused rather than silently trimmed into a match.
    """

    if not isinstance(name, str) or not name:
        return False
    if os.sep in name or "/" in name:
        return False
    return BROWSER_REPORT_ARTIFACT_RE.fullmatch(name) is not None


def _plain_file_size(worktree: Path, relative: str) -> "int | None":
    """Size of the plain file at a lane-relative path, or None.

    None covers every way a path can fail to be the artifact it claims to be:
    absent, a symlink, a directory, a FIFO, or anything else ``lstat`` does not
    report as a regular file. Only what ``lstat`` confirmed is ever measured, so
    a link at an artifact name can never point the accepted set at a file this
    lane does not own, and a FIFO can never make the run block.
    """

    try:
        info = os.lstat(worktree / relative)
    except (OSError, ValueError):
        return None
    if not (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)):
        return None
    return info.st_size


def _report_only_browser_artifacts(
    worktree: Path, delivery: LaneDelivery, *, browser: bool
) -> "frozenset[str]":
    """The browser-report artifacts this run's own verdict may accept.

    Empty unless this run granted ``playwright``: the namespace is that run's
    exception, granted by this argv, and never a general pass on the lane root.
    A candidate must be untracked — the status rule the scratch tree is held to,
    for the same reason: a repository may track a file at a namespace name, and
    git then reports a modification, staging, deletion, rename, or copy of it,
    which is source work however it is named — must match the name rule exactly,
    and must be a plain, non-symlink regular file at the lane root.

    The caps bound the set in sorted order, so which of an over-long set falls
    outside them is deterministic rather than dependent on git's output order:
    the 41st file, and the file that would take the total past the aggregate
    cap, are not this run's artifact and are refused as source work. A file over
    the per-file cap is skipped and never counted.
    """

    if not browser:
        return frozenset()
    statuses = {entry.path: entry.status for entry in delivery.changed}
    accepted: list[str] = []
    total = 0
    for path in sorted(delivery.uncommitted):
        if statuses.get(path) != UNTRACKED_STATUS:
            continue
        if not _is_browser_report_artifact_name(path):
            continue
        if len(accepted) >= BROWSER_ARTIFACT_MAX_FILES:
            break
        size = _plain_file_size(worktree, path)
        if size is None or size > BROWSER_ARTIFACT_MAX_BYTES:
            continue
        if total + size > BROWSER_ARTIFACT_MAX_TOTAL_BYTES:
            break
        accepted.append(path)
        total += size
    return frozenset(accepted)


#: The safe-open primitives an accepted artifact is read through, resolved
#: once at import. A ``None`` here means this build cannot open a path in a way
#: that refuses a link and cannot block on a FIFO; that state fails closed
#: (see :func:`_open_artifact`) instead of falling back to an unchecked open,
#: and it is a state a test can reach by patching these two names.
_ARTIFACT_NOFOLLOW_OPEN = getattr(os, "O_NOFOLLOW", None)
_ARTIFACT_NONBLOCK_OPEN = getattr(os, "O_NONBLOCK", None)


def _close_artifact(descriptor: int) -> None:
    """Close an artifact descriptor, treating a failing close as nothing.

    The read is the verdict. A descriptor that will not close must not become
    an exception raised past a gate whose whole job is to answer ``None``.
    """
    try:
        os.close(descriptor)
    except OSError:
        pass


def _open_artifact(path: Path, info: os.stat_result) -> "int | None":
    """An open descriptor for the artifact ``info`` describes, or ``None``.

    ``O_NOFOLLOW`` refuses a link sitting in place of the file, and
    ``O_NONBLOCK`` makes a read-only open of a FIFO return instead of waiting
    for a writer, so a path swapped after the caller's ``lstat`` cannot be
    followed, read, or blocked on. The descriptor is then checked against that
    ``lstat``: a regular file whose device and inode are the checked file's.
    That comparison is the identity check — ``fstat`` cannot report a link,
    because the open has already resolved one or refused it, so refusing links
    is ``O_NOFOLLOW``'s job and this check does not claim to do it.

    ``None`` is the fail-closed answer, and it is also the answer where either
    flag is unavailable: the fallback would be exactly the unchecked open this
    function exists to prevent. The caller owns the descriptor.
    """

    if _ARTIFACT_NOFOLLOW_OPEN is None or _ARTIFACT_NONBLOCK_OPEN is None:
        return None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | _ARTIFACT_NOFOLLOW_OPEN | _ARTIFACT_NONBLOCK_OPEN
        )
    except (OSError, ValueError):
        return None
    try:
        opened = os.fstat(descriptor)
    except (OSError, ValueError):
        opened = None
    if opened is not None and stat.S_ISREG(opened.st_mode) and (
        (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino)
    ):
        return descriptor
    _close_artifact(descriptor)
    return None


def _artifact_identity(path: Path) -> "str | None":
    """The whole-content identity of one accepted browser artifact, or None.

    Deliberately *not* the report's own rule.
    :func:`report_stop_hook.report_identity` samples a bounded prefix, which is
    the right bargain for the report's freshness contract and the wrong one
    here: an artifact is admitted up to ``BROWSER_ARTIFACT_MAX_BYTES``, so a
    rewrite in place that keeps the length and lands past that sampled prefix
    would leave the sample — and with it the whole ``--verify`` comparison —
    unchanged. The report's own identity rule is untouched and still sampled;
    only this namespace, which admits much larger files, is hashed whole.

    Everything read is bounded by the same per-file cap that admitted the
    artifact. ``lstat`` must first confirm a plain, non-symlink regular file at
    or under the cap, and the file is then opened through
    :func:`_open_artifact`, which refuses a link in place of the path, cannot
    block on a FIFO, and must yield the very file that ``lstat`` described. A
    link, a FIFO, a directory, an absent path, or a file over the cap is None
    rather than a read of foreign or unbounded content, and so is a build that
    cannot open a path that way — the read fails closed instead of falling
    back to a name nothing has confirmed. ``fstat`` does not prove the path is
    not a link; the descriptor check is the identity one, and ``O_NOFOLLOW``
    is what refuses a link. The read stops at the cap plus one byte, so a file
    that grows while it is being read is refused instead of streamed.
    """

    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > BROWSER_ARTIFACT_MAX_BYTES:
        return None
    descriptor = _open_artifact(path, info)
    if descriptor is None:
        return None
    try:
        opened = os.fstat(descriptor)
        if opened.st_size > BROWSER_ARTIFACT_MAX_BYTES:
            return None
        digest = hashlib.sha256()
        remaining = BROWSER_ARTIFACT_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            # It grew past the cap while being read, so this is a truncated
            # look at it. An identity taken from a truncated read is not
            # the file's.
            return None
    except (OSError, ValueError):
        return None
    finally:
        # Every path out of the read releases the descriptor, the refusals
        # included.
        _close_artifact(descriptor)
    return f"sha256:{digest.hexdigest()}:size:{opened.st_size}"


def _artifact_identities(
    worktree: Path, artifacts: "frozenset[str]"
) -> tuple[tuple[str, "str | None"], ...]:
    """``(path, identity)`` for each accepted artifact, in sorted order.

    One function for both looks, so the state captured before ``--verify`` and
    the state read after it are the same facts about the same paths: a before
    side built any other way can never equal its after side, and the
    comparison then refuses a lane whose artifacts the command did not touch.
    """

    return tuple(
        (path, _artifact_identity(worktree / path)) for path in sorted(artifacts)
    )


def _report_only_lane_artifacts(
    worktree: Path, report_path: Path
) -> "frozenset[str] | None":
    """The lane path a report-only run's own artifact must occupy.

    Exactly one path is accepted — the fixed report name directly inside the
    lane worktree — and ``None`` means the report path this run recorded is not
    that path. There is no second guess available in that state: the run cannot
    tell which of the lane's changed files is the report it was dispatched to
    collect, so it fails closed rather than judge a lane whose artifact it
    cannot identify.
    """

    relative = os.path.relpath(os.fspath(report_path), os.fspath(worktree))
    if os.path.isabs(relative) or os.sep in relative:
        return None
    if relative != report_stop_hook.REPORT_NAME:
        return None
    return frozenset({relative})


def _report_only_unexpected_paths(
    delivery: LaneDelivery,
    report_artifacts: "frozenset[str]",
    browser_artifacts: "frozenset[str]" = frozenset(),
) -> tuple[str, ...]:
    """Lane-relative paths a report-only lane left that are not its report.

    Three entries are permitted, and each for its own reason:

    * the report artifact itself, untracked where the repository does not
      track it or modified where it does; rename/copy statuses are refused
      because they can conceal a different source path;
    * a path under the lane's scratch tree that git reports as *untracked*,
      which is the throwaway artifact the governance told the lane to write
      there and which the runner's own exclusion normally hides entirely;
    * a browser-report artifact this run's own ``playwright`` grant made
      allowed, as decided by :func:`_report_only_browser_artifacts` — that
      function holds the name rule, the untracked-status requirement, the
      plain-file requirement, and the caps, and returns only the paths that meet
      all of them, so this rule never has to restate or second-guess them.

    The scratch exemption is deliberately not a prefix match on its own. A
    repository may track a file under that directory, and git reports a
    modification, deletion, staged addition, or rename of a tracked path with
    one of those codes rather than ``??`` — that is source work by any other
    name, and reading the prefix as permission would let a worker edit a
    tracked file under a documented throwaway directory and still be accepted.
    A path whose status the inspection did not carry is refused too: an
    unknown state is not evidence of an untracked artifact, and the one thing
    this rule must never do is guess in the worker's favour.
    """

    statuses = {entry.path: entry.status for entry in delivery.changed}
    unexpected: list[str] = []
    for path in delivery.uncommitted:
        status = statuses.get(path)
        if path in report_artifacts and not (
            status is not None and ("R" in status or "C" in status)
        ):
            continue
        if path in browser_artifacts:
            continue
        if (
            status is not None
            and status == UNTRACKED_STATUS
            and path.startswith(REPORT_ONLY_SCRATCH_PREFIX)
        ):
            continue
        unexpected.append(path)
    return tuple(unexpected)


def _report_only_blocker(
    *,
    report_state: str | None,
    report_path: Path,
    report_artifacts: "frozenset[str] | None",
    committed: bool,
    unexpected: Sequence[str],
    report_deliverable_flag: str = "--report-only",
    browser: bool = False,
) -> str | None:
    """Why a report lane is not a delivery, or None when it is.

    Report delivery is decided here, on the report artifact and the absence of
    source work, and never on the commit state an execute lane is judged by.
    The two are genuinely different deliverables: the worker is instructed to
    change no source and make no git change, so requiring a commit plus a clean
    tree would reject the very lane this option exists to accept — and
    ``--allow-no-commit`` could not repair that, because the report file is
    itself one of the uncommitted paths that flag's condition excludes.

    Nothing is relaxed by the split. Source-checkout changes still fail the run
    (``LANE_SOURCE_MUTATED``), an unreadable lane still fails it
    (``LANE_DELIVERY_UNVERIFIED``), and a worker that commits, or that leaves
    any file outside the report, the scratch tree, and this run's own browser
    artifacts, is not delivered. ``report_deliverable_flag`` is the flag the
    operator actually passed, so the sentence names the option they can look up;
    the rule is the same one either way.
    """

    if report_state != "current":
        return (
            f"{report_deliverable_flag} lane produced no usable "
            f"{report_stop_hook.REPORT_NAME} at {report_path}: "
            f"{REPORT_STATE_DETAIL.get(report_state, 'the report could not be judged')}. "
            "The worker's completion prose is not the report. The lane's "
            "worktree, any commit, and the audit are intact."
        )
    if report_artifacts is None:
        return (
            f"{report_deliverable_flag} lane's report path {report_path} is not "
            f"{report_stop_hook.REPORT_NAME} at the lane worktree root, so this "
            "run cannot tell the report apart from implementation work. Nothing "
            "was removed. The lane's worktree and audit are intact."
        )
    if committed:
        return (
            f"{report_deliverable_flag} lane committed work, so it is not a "
            "report outcome: the worker was told to make no git change, and a "
            "commit also means a branch this run must not publish. Nothing was "
            "removed; inspect the lane's commit on its branch before accepting "
            "it."
        )
    if unexpected:
        listed = "\n  ".join(unexpected[:20])
        more = (
            ""
            if len(unexpected) <= 20
            else f"\n  ... and {len(unexpected) - 20} more"
        )
        permitted = (
            f"untracked files in the lane's {SCRATCH_DIR_NAME}/ scratch tree"
        )
        if browser:
            permitted += (
                " or untracked browser-report artifacts at the lane root in the "
                "namespace this run granted with --capability playwright"
            )
        artifacts_clause = (
            ", or — for this run, which granted playwright — at the lane root "
            "under the browser-report artifact name rule"
            if browser
            else ""
        )
        return (
            "report lane left changes that are not its report and are not "
            f"{permitted}:"
            f"\n  {listed}{more}\n"
            "Screenshots and intermediate output belong in the lane's "
            f"git-excluded {SCRATCH_DIR_NAME}/ directory as new, untracked "
            f"files{artifacts_clause}; editing or removing a *tracked* path is "
            "source work, and source work needs an execute lane, not a report "
            "one. Nothing was removed. The lane's worktree and audit are intact."
        )
    return None


@dataclass(frozen=True)
class _ReportOnlyState:
    """The facts a report verdict is made of, read from the tree.

    Taken twice in a run that verifies: once from the verdict's own inputs,
    and once immediately after the verification command, so that a command
    which wrote into the lane or the coordinator checkout is a fact the run
    reports rather than a change it silently absorbs into the delivery.
    """

    committed: bool
    unexpected: tuple[str, ...]
    source_changes: tuple[str, ...]
    report_identity: "str | None"
    #: ``(path, identity)`` for each accepted browser artifact, in sorted order,
    #: where the identity is the whole file's (see :func:`_artifact_identity`).
    #: A name set alone would absorb a rewrite in place — same path, same
    #: untracked status, same size — so the identity is part of the compared
    #: state, as it is for the report itself.
    artifacts: tuple[tuple[str, "str | None"], ...] = ()
    #: Why the lane, or the coordinator checkout, could not be inspected at
    #: all. A failed look is a fact about the run, not an exception to raise
    #: past the verdict that has to report it.
    lane_error: "str | None" = None
    source_error: "str | None" = None


def _report_only_state(
    lane,
    report_path: Path,
    report_artifacts: "frozenset[str]",
    repo: Path,
    source_baseline: "frozenset[str]",
    *,
    browser: bool = False,
) -> _ReportOnlyState:
    """Read the report contract's inputs fresh from the real tree.

    Never raises: an inspection that fails is one of the facts the caller has
    to map onto a contract, and swallowing it into a delivery is exactly the
    failure this state exists to prevent.

    ``browser`` is this run's own namespace grant, passed through rather than
    inherited from the earlier verdict: the accepted artifact set is re-decided
    from the fresh tree, so an artifact the verification command added or
    removed is the difference it is instead of a set this call assumes still
    holds.
    """

    lane_error = None
    committed = False
    unexpected: tuple[str, ...] = ()
    artifacts: tuple[tuple[str, "str | None"], ...] = ()
    try:
        delivery = lane_delivery(lane)
    except WorktreeError as exc:
        lane_error = str(exc)
    else:
        committed = delivery.committed
        browser_artifacts = _report_only_browser_artifacts(
            lane.worktree, delivery, browser=browser
        )
        unexpected = _report_only_unexpected_paths(
            delivery, report_artifacts, browser_artifacts
        )
        artifacts = _artifact_identities(lane.worktree, browser_artifacts)
    source_error = None
    source_changes: tuple[str, ...] = ()
    try:
        source_changes = source_mutations(repo, source_baseline)
    except WorktreeError as exc:
        source_error = str(exc)
    try:
        identity = report_stop_hook.report_identity(report_path)
    except report_stop_hook.UnsafeReportPath:
        # Something the freshness gate would refuse now sits at the path; that
        # is a change to the artifact from whatever was there before.
        identity = None
    return _ReportOnlyState(
        committed=committed,
        unexpected=unexpected,
        source_changes=source_changes,
        report_identity=identity,
        artifacts=artifacts,
        lane_error=lane_error,
        source_error=source_error,
    )


def _report_only_verification_changes(
    before: _ReportOnlyState, after: _ReportOnlyState, *, report_path: Path
) -> tuple[str, ...]:
    """What the verification command itself introduced, in the operator's words.

    ``--verify`` runs an arbitrary shell command inside the lane, so it can
    commit, leave files, rewrite the report the verdict was just reached on, or
    write into the coordinator checkout. None of that is the worker's report
    delivery, and accepting the lane after it would report a state the run
    never judged. Every difference found here makes the lane not delivered,
    and nothing is removed to make the difference go away.

    One entry is not a difference the command made: an accepted artifact whose
    content this run could not read is refused here too, because identity
    ``None`` on both sides compares equal and would otherwise accept, as
    unchanged, an artifact nothing verified.
    """

    changes: list[str] = []
    if after.report_identity != before.report_identity:
        changes.append(f"it changed the run's report artifact at {report_path}")
    # An accepted artifact is admitted by name and status, so an identity that
    # could not be read — a link, a FIFO, or anything else the safe open
    # refuses — is a hole the comparison below cannot see through: two
    # unreadable identities are equal, and the lane would be accepted on an
    # artifact this run never read. It is refused whichever look found it,
    # including on both sides, and it is never reported as a change the
    # verification command made.
    unreadable = sorted(
        {
            path
            for path, identity in before.artifacts + after.artifacts
            if identity is None
        }
    )
    if unreadable:
        changes.append(
            "the run's report artifacts at " + ", ".join(unreadable)
            + " cannot be read as a plain file, so their content cannot be "
            "verified"
        )
    elif after.artifacts != before.artifacts:
        # Same path, same untracked status, same length: only the whole-content
        # identity distinguishes a rewrite in place from the artifact the
        # verdict accepted, and a name-set comparison — or a prefix sample of a
        # file large enough to hold the change past that prefix — would absorb
        # it silently.
        named = sorted(
            {path for path, _identity in before.artifacts + after.artifacts}
        )
        changes.append(
            "it changed the run's report artifacts at " + ", ".join(named)
        )
    if after.committed and not before.committed:
        changes.append("it committed work on the lane branch")
    for path in after.unexpected:
        if path not in before.unexpected:
            changes.append(f"it left {path} in the lane")
    for path in after.source_changes:
        if path not in before.source_changes:
            changes.append(f"it changed the coordinator checkout at {path}")
    if after.lane_error is not None:
        changes.append(
            f"the lane could not be inspected afterwards: {after.lane_error}"
        )
    if after.source_error is not None:
        changes.append(
            "the coordinator checkout could not be compared afterwards: "
            f"{after.source_error}"
        )
    return tuple(changes)


#: The hosts whose adapter can hand the worker an existing owner workspace
#: instead of a worktree it created. Each of the three host launchers takes the
#: workspace as its working directory already; what each had to be told is that
#: a workspace which is not a dedicated lane is a legitimate target, and on
#: which terms. A host absent from here is refused by name rather than silently
#: given a lane whose isolation story does not hold.
EXISTING_WORKSPACE_HOSTS = ("claude", "codex", "devin")


def _validate_existing_workspace(
    args: argparse.Namespace,
    workspace: str,
    execute_profile: str | None,
    model_config: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    report_deliverable: bool,
    report_flag: str,
) -> None:
    """Refuse an existing-workspace selection this run cannot honour.

    Every refusal here is a case where the run would otherwise start a worker
    under a promise that does not hold: work in someone else's tree, a lane
    that would be judged on a commit rule the mode does not use, or an
    isolation claim the workspace cannot make. Each is refused before any
    workspace is claimed, so none of them costs a lock.

    The contract has two halves, and a run must satisfy both on every host:
    the **route** must run on the operator's own machine
    (:func:`route_runs_local`: a declared ``local-user-workspace`` location, or
    a native host protocol that declares none), and the **private route table**
    must be the one that opted in (:func:`execute_profile_policy`). Neither is
    inferred from a provider or model name, and neither is reachable from the
    public table or a cloud-generated one, so this mode cannot select itself.
    The selection itself is always the operator's: only an explicit
    ``--existing-workspace`` reaches any of this.

    The claude host additionally requires its own ``local-developer`` profile
    to have been resolved, because that profile is what carries the widened
    tool surface for that lane. The codex and devin hosts have no allowlist
    seam for a profile to widen — a codex execute lane already runs
    ``danger-full-access``, and the devin adapter keeps the per-command policy
    its route selects — so for them the two halves above are the whole gate,
    and :func:`resolve_execute_profile` honestly records ``standard``.
    """

    if args.mode != "execute":
        raise SideLaneError("--existing-workspace is supported only in execute mode")
    if args.host not in EXISTING_WORKSPACE_HOSTS:
        raise SideLaneError(
            "--existing-workspace is supported on the "
            + ", ".join(EXISTING_WORKSPACE_HOSTS)
            + f" hosts only; the {args.host} adapter has no seam that tells a "
            "worker its working directory is an owner's checkout rather than a "
            "dedicated lane"
        )
    if not route_runs_local(model_config):
        raise SideLaneError(
            "--existing-workspace requires a route that runs on this machine: "
            f"either it declares execution_location: {LOCAL_USER_WORKSPACE}, or "
            "it is a native host route that declares no location. This route is "
            "neither, so the owner's own checkout is not the tree it runs in "
            "and the run is refused rather than pointed at somebody else's"
        )
    if execute_profile_policy(config) != LOCAL_DEVELOPER_PROFILE:
        # The policy, not the flag, is what says this private table's local
        # routes run as the local developer in the user's own tree. A table
        # that declares no policy — the public `config/models.json`, a
        # cloud-generated one — states no such thing, and the run is refused
        # rather than taking local authority from a route's location alone.
        raise SideLaneError(
            f"--existing-workspace requires the private route table to opt in "
            f"with `{EXECUTE_PROFILE_POLICY_KEY}`: "
            f"{{{EXECUTE_PROFILE_POLICY_DEFAULT_KEY!r}: "
            f"{LOCAL_DEVELOPER_PROFILE!r}}}. This table does not, so no private "
            "policy selects a local-user workspace and the run is refused "
            "rather than kept local by a location a public table could declare"
        )
    if args.host == "claude" and execute_profile != LOCAL_DEVELOPER_PROFILE:
        # A route that resolves back to the conservative per-command allowlist
        # would be given a workspace its own profile says it must not have.
        raise SideLaneError(
            f"--existing-workspace requires the {LOCAL_DEVELOPER_PROFILE} execute "
            "profile on the claude host, which this route does not select. Drop "
            "the flag to run in a lane worktree instead, or point the run at a "
            "route whose location is local-user-workspace and whose table opts in"
        )
    if report_deliverable:
        # A report lane's deliverable is a report artifact under a fixed path,
        # and its verdict rejects any commit or unexpected path. In a workspace
        # that already holds other people's uncommitted work, that verdict is
        # unreachable and meaningless — it would fail the run for work the
        # worker did not do.
        raise SideLaneError(
            f"--existing-workspace cannot be combined with {report_flag}: a "
            "report lane is judged on a report artifact and a clean tree, and a "
            "workspace holding others' uncommitted work can satisfy neither"
        )
    if getattr(args, "verify", None):
        # A verification command runs inside the lane, and here the lane is the
        # owner's own checkout. The coordinator's task is bounded to running a
        # worker there; running an unvetted command in it as well is a wider
        # write than that, so it is refused rather than silently skipped.
        raise SideLaneError(
            "--existing-workspace cannot be combined with --verify: the "
            "verification command would run in the owner's workspace, which this "
            "run does not own"
        )
    if getattr(args, "worktree_root", None):
        raise SideLaneError(
            "--worktree-root is invalid with --existing-workspace: no worktree is "
            "created, so there is no root to place one in"
        )
def _publication_record(
    authority: Mapping[str, object],
    runner: str,
    runner_skip_reason: "str | None",
) -> dict[str, object]:
    """One run's publication record: authority carried, and what its runner did.

    Built here so that every outcome reaching a summary carries the same
    record. A run whose tree could not be inspected stops before the push
    decision, but it still carried whatever authority is in ``authority``, and
    dropping the record there would leave a reader of an explicit
    no-external-publication run unable to tell it from one that carried none.
    ``runner`` is what this runner did about publication and is never a
    statement about the worker's own.
    """

    return {
        **authority,
        "runner": runner,
        "runner_skip_reason": runner_skip_reason,
    }


def _launch(
    args: argparse.Namespace, config: Mapping[str, Any], repo: Path, prompt: str,
    *, read_roots: Sequence[Path] = (), web_domains: Sequence[str] = (),
    run_mcp_servers: "Mapping[str, Any] | None" = None,
    measurement: "Mapping[str, Any] | None" = None,
) -> int:
    provider_config, model_config = select_route(
        config, args.host, args.mode, args.provider, args.model
    )
    # The operator's own selection of an existing owner workspace, read here
    # because the local developer profile needs it as its third statement on a
    # route that declares no location: see `resolve_execute_profile`. Exact
    # -string read, like `report_only` below, so nothing but an explicit path on
    # this command line selects the mode. The profile itself is resolved after
    # the report contract below, because a report lane always narrows to
    # `standard` whatever this workspace selection and the route declare.
    raw_workspace = getattr(args, "existing_workspace", None)
    existing_workspace = raw_workspace if isinstance(raw_workspace, str) else None
    # The report-only opt-in is the same-invocation repair for a worker that
    # ended its turn with exit 0 and reported a report it never wrote. It is
    # deliberately narrow: execute mode only, the Claude host only (the
    # mechanism is a Claude Code Stop hook), and gated on an explicit spend cap
    # so the cap and the hook are part of one command. Its implied report
    # deliverable also injects the canonical override and Claude command
    # denials. MCP handling and timeout remain unchanged; review lanes never
    # see this flag.
    # Exact-boolean read: an argparse Namespace always carries the declared
    # flag, and anything other than an explicit True means the opt-in was not
    # given, so no lane can be steered into report-only mode by accident.
    report_only = getattr(args, "report_only", False) is True
    # The report is the deliverable. Two concerns were fused in one flag: that
    # the report artifact is this lane's deliverable, and that a Claude Code
    # Stop hook plus a USD cap buys one more turn to write it. Only the second
    # is Claude's, and only the second bounds spend — so `--report-only`
    # implies this and keeps its hook and its cap, while `--report-deliverable`
    # selects the report contract without that hook or cap: canonical
    # instructions, host-specific command denials, and the post-run verdict.
    # A report lane can run on any host and route, including provider-key routes with no
    # `max_budget_usd` and the Devin/Codex hosts where no Stop hook exists.
    report_deliverable = report_only or (
        getattr(args, "report_deliverable", False) is True
    )
    # The flag the operator actually passed, so every operator-facing sentence
    # names an option they can look up. `--report-only` implies the verdict, so
    # a run carrying both is named for the flag that also bought the Stop hook.
    report_flag = "--report-only" if report_only else "--report-deliverable"
    # The execute profile is resolved from the private route table this run was
    # configured with, before any worktree, credential, or host process exists:
    # only a table that explicitly opts in *and* a route it declares local take
    # the local developer profile, and every other route — the public table,
    # which declares local locations but no policy; a cloud-generated table; a
    # route that declares nothing — takes the public conservative default.
    # Review lanes resolve to the default. The host is not a premise: a route
    # whose table opted in and which declares a local location resolves the
    # profile on every qualified host, and an explicit selection that the
    # policy, the route, or the mode does not support stops the run here. The
    # report contract is read FIRST and resolved with the profile, because it
    # is the narrower of the two selections: a lane whose deliverable is the
    # report always resolves the restricted profile — never the widened
    # surface its route's table might otherwise grant — and an explicit wider
    # selection beside a report flag is refused here, before anything is
    # created. The workspace selection read above is the profile's third
    # statement on a route that only infers local.
    execute_profile = resolve_execute_profile(
        requested=execute_profile_argument(args),
        mode=args.mode,
        host=args.host,
        model_config=model_config,
        config=config,
        existing_workspace=existing_workspace is not None,
        report_deliverable=report_deliverable,
    )
    # Running a worker inside an existing owner workspace rather than a lane
    # worktree created from HEAD. Named on the command line by the operator, so
    # it is a deliberate selection and never something a route, a policy table,
    # or a default can arrive at — that is what keeps the shared primary
    # checkout from being entered silently. Read above, as an exact string: a
    # flag that was not given, or was given as anything but a string, selects
    # nothing.
    # The workspace's own staged, unstaged and untracked work is the owner's,
    # not this run's: no path here resets, cleans, stashes, commits, or
    # publishes it, and the deltas the run reports are measured against a
    # content-and-index baseline taken before the worker started.
    if existing_workspace is not None:
        _validate_existing_workspace(
            args, existing_workspace, execute_profile, model_config, config,
            report_deliverable, report_flag,
        )
    # The directory this run's worker will actually run in, resolved once and
    # before the capability gate below rather than at the claim, because the
    # gate reads *project* MCP registrations: Claude's `.mcp.json`, Codex's
    # `.codex/config.toml`, Devin's `.devin/mcp_config*.json`. Those files are
    # per-worktree and may be untracked, so for an owner workspace they are the
    # workspace's own and not the coordinator checkout's — an owner workspace is
    # a different worktree of the same repository, and its registry can differ
    # or be absent entirely. Deciding admission from `repo` would staff a route
    # on a registration the worker's host never loads, in either direction.
    # Resolution is read-only and claims nothing, so a workspace this run
    # cannot resolve fails here — closed — rather than being admitted on a
    # config file it will not read. A created lane's worker directory is a
    # fresh worktree of `repo`, so `repo` is its project scope and is used
    # directly.
    worker_workspace: Path | None = None
    if existing_workspace is not None:
        worker_workspace = resolve_existing_workspace(repo, existing_workspace)
    if report_deliverable:
        # A report lane's deliverable is the report artifact in the lane
        # worktree, so it never holds a capability whose grant is explicit write
        # authority: no push of a branch it was told not to commit, and no
        # workflow or messaging write, even though an ordinary execute lane may
        # make one when its approved task names the exact update and recipient.
        # Refused here — before the spend cap the `--report-only` repair
        # requires, because no cap makes it honorable — and on `--report-only`
        # and `--report-deliverable` alike.
        for capability in report_write_capability_conflicts(args.capability):
            raise SideLaneError(REPORT_WRITE_CAPABILITY_REFUSALS[capability])
    # A separate authority from `--no-publish`, and deliberately not derived
    # from it: that option governs the runner's own push of a delivered branch,
    # while this states that the task forbids the worker to publish at all.
    # Read as an exact boolean, like the report options, so nothing inherited
    # can steer a lane into the refusal.
    no_external_publication = (
        getattr(args, "no_external_publication", False) is True
    )
    if no_external_publication and args.mode != "execute":
        # Same reason as every other execute-only option here: a review lane's
        # argv is the strict read-only form and carries no permission rule this
        # could narrow, so accepting it would be a refusal the operator
        # believes happened.
        raise SideLaneError(
            "--no-external-publication is supported only in execute mode"
        )
    if no_external_publication:
        # The guard denies the publication command family, so a capability whose
        # whole grant is that family cannot be honored: the same run would deny
        # the only rule it adds. Refused here, before a worktree, a credential
        # or a host process exists, and on direct adapter calls too.
        for capability in publication_refusal_capability_conflicts(args.capability):
            raise SideLaneError(
                "a lane whose task authority refuses external publication is "
                f"not granted --capability {capability}: the whole grant is the "
                "publication this lane must not make, so its only allow rule "
                "could only be denied by the same run. Drop the capability, or "
                "run an ordinary execute lane for a task that authorizes "
                "publication."
            )
    # Assembled here, before any worker starts, but the audit that carries it
    # is not written until the adapter has returned — write_audit is called
    # further down, and an adapter that raises writes no audit at all. So this
    # is not "the audit already exists": it is the authority this run was
    # given, what this host's own controls make of it, and the honest statement
    # that the worker's publication is not verified. It is deliberately not
    # folded into the `published` outcome below, so the two can never be read
    # as one another.
    publication_authority: dict[str, object] = {
        "task_authority": (
            "--no-external-publication" if no_external_publication else None
        ),
        "host_enforcement": (
            PUBLICATION_ENFORCEMENT_BY_HOST[args.host]
            if no_external_publication
            else None
        ),
        "worker_publication_verified": "not-checked",
    }
    if report_only:
        if args.mode != "execute":
            raise SideLaneError("--report-only is supported only in execute mode")
        if args.host != "claude":
            raise SideLaneError(
                "--report-only is supported only on the claude host: the repair "
                "is a Claude Code Stop hook inside the same invocation"
            )
        # The cap is not a general report-lane rule: it is what the hook buys,
        # so it is required exactly where the hook is armed. `--report-deliverable`
        # alone carries no implicit spend cap.
        try:
            require_report_only_budget(model_config)
        except ClaudeAdapterError as exc:
            raise SideLaneError(str(exc)) from exc
    if (
        getattr(args, "report_deliverable", False) is True
        and args.mode != "execute"
    ):
        # Same reason as every other execute-only option here: a review lane's
        # argv is the strict read-only form, and a verdict it cannot reach would
        # be a silent no-op the operator believes happened.
        raise SideLaneError(
            "--report-deliverable is supported only in execute mode"
        )
    if args.capability and args.mode != "execute":
        raise SideLaneError("--capability is supported only in execute mode")
    if run_mcp_servers and args.mode != "execute":
        # Review mode is strict no-MCP by canonical governance; no per-run
        # registration may widen it, and the adapters refuse it too.
        raise SideLaneError("--mcp-config is supported only in execute mode")
    if run_mcp_servers:
        # The narrowing control: every declared server must map from a
        # capability the coordinator also passed with --capability, so the
        # run config can never grant tools beyond the requested capabilities.
        validate_against_capabilities(run_mcp_servers, set(args.capability))
        # Fail closed before anything is created when a referenced env name is
        # absent from the launching environment (the adapters re-check against
        # the scrubbed child environment they actually build).
        require_env_references(run_mcp_servers, os.environ)
    if read_roots and args.mode != "execute":
        # A review lane's argv is the strict read-only form, and the only
        # directory control the hosts expose for extra directories is a
        # workspace/write grant. Refuse rather than launch a lane whose stated
        # scope the worker cannot be given.
        raise SideLaneError("--read-root is supported only in execute mode")
    if getattr(args, "verify", None) and args.mode != "execute":
        # A review lane disposes its worktree before any verification could
        # run, so accepting the flag there would be a silent no-op the
        # operator believes happened. Fail closed instead.
        raise SideLaneError("--verify is supported only in execute mode")
    if getattr(args, "verify", None) and no_external_publication:
        # The caller's command is arbitrary shell, and it runs in the lane
        # before the runner reaches its own publication decision below — so
        # `--verify "git push origin HEAD"` would publish *first* and be
        # recorded as a skipped push after, making the run's own record claim
        # the opposite of what happened. The guard is the task's refusal of any
        # external publication by this lane, and no command-string filter makes
        # an arbitrary caller-supplied command safe to run under it — the
        # canonical section says as much about its own deny rules. So the
        # combination is refused outright instead of being contained: refused
        # here, before a worktree, a credential, a host process, or the command
        # itself exists. Only the guard is incompatible — `--no-publish` is the
        # runner's own push decision and never forbade the worker to publish,
        # so `--verify` keeps its existing meaning there.
        raise SideLaneError(
            "--verify is not accepted in a lane whose task authority refuses "
            "external publication (--no-external-publication): the command is "
            "arbitrary shell that runs in the lane before the runner's own "
            "publication decision, so a command that publishes would publish "
            "before this run recorded that it skipped. Nothing here filters an "
            "arbitrary shell command. Drop --verify, or run an ordinary execute "
            "lane — with or without --no-publish — when a verification command "
            "is wanted."
        )
    if getattr(args, "skill", None) and args.mode != "execute":
        # Skills materialize inside the execute lane's worktree; a review
        # lane has nowhere to put them and would silently drop the request.
        raise SideLaneError("--skill is supported only in execute mode")
    if measurement is not None and args.mode != "execute":
        # An assignment measures execution work handed to a worker. A review
        # lane produces no deliverable to accept or reject, so recording it as
        # an assignment would put work in the measurement denominator that the
        # measurement cannot score. Fail closed rather than half-record it.
        raise SideLaneError("--measurement-file is supported only in execute mode")
    unknown = sorted(set(args.capability) - set(config["capabilities"]))
    if unknown:
        raise SideLaneError(f"unknown capabilities: {', '.join(unknown)}")
    if args.capability:
        if worker_workspace is None and any(
            capability in RUN_MCP_CAPABILITY_SERVERS for capability in args.capability
        ):
            _require_inherited_project_mcp_config(args.host, repo)
        # Launch needs evidence that the capability exists on this host
        # ("verified" or "present"); "unknown"/"unavailable" fail closed. The
        # stricter boolean `capabilities` map (verified only) is what
        # `recommend` uses to rank routes, not what gates a launch.
        evidence = _capability_report(
            config, args.host, args.mode, args.provider, args.model,
            # The worker's own project scope, not the coordinator's: see
            # `worker_workspace` above.
            repo if worker_workspace is None else worker_workspace,
        )["capability_evidence"]
        if run_mcp_servers:
            # A per-run registration IS this run's delivery of the capability:
            # the host-inventory scan cannot see a file that has not been
            # written for the host yet, so the delivered server names supply
            # the presence evidence instead — registration evidence only, on
            # the same terms as every other "present" basis: bridge
            # authentication and a live tool call stay unproven.
            delivered = set(run_mcp_servers)
            for name in args.capability:
                server = RUN_MCP_CAPABILITY_SERVERS.get(name)
                if server is not None and server in delivered:
                    evidence[name] = {
                        "state": "present",
                        "basis": (
                            f"per-run --mcp-config registration of the server named "
                            f"{server!r}; bridge authentication and any live tool "
                            "call not tested"
                        ),
                    }
        missing = [
            name
            for name in args.capability
            if evidence.get(name, {}).get("state") not in LAUNCHABLE_STATES
        ]
        if missing:
            raise SideLaneError(
                f"required capabilities unavailable: {', '.join(missing)}"
            )
    if not args.lane_name:
        raise SideLaneError(f"{args.mode} mode requires --lane-name")
    if not provider_config["billable"] and args.approve_billable_route:
        raise SideLaneError(
            "--approve-billable-route is invalid for non-billable native OAuth routes"
        )
    if provider_config["billable"] and not args.approve_billable_route:
        raise SideLaneError(
            "billable route requires explicit --approve-billable-route for this run"
        )
    executable = _require_host_executable(args.host)
    capabilities = tuple(sorted(set(args.capability)))
    # This run's own exception, granted by this argv and never inherited from
    # elsewhere: a report lane without `--capability playwright` gets no
    # root-level artifact pass at all, and with it the namespace, the untracked
    # status requirement, and the caps are the cloud worker's own — see
    # BROWSER_REPORT_ARTIFACT_RE.
    browser_report = report_deliverable and "playwright" in capabilities
    workspace_lock: WorkspaceLock | None = None
    workspace_baseline: WorkspaceBaseline | None = None
    if existing_workspace is None:
        lane = create_worktree(
            repo, args.lane_name, worktree_root=getattr(args, "worktree_root", None)
        )
    else:
        # The lock is taken BEFORE the baseline is captured: the baseline is
        # only a baseline if nothing wrote between the two, and the claim is
        # what stops a second lane starting on top of this one. The order also
        # means a refusal costs nothing — no lane exists yet.
        workspace_lock, workspace_baseline, lane = _claim_existing_workspace(
            args, repo, existing_workspace, resolved=worker_workspace
        )
    try:
        return _launch_in_lane(
            args, repo, prompt, lane, provider_config, model_config, executable,
            capabilities, execute_profile, report_only, report_deliverable,
            report_flag, browser_report, read_roots, web_domains,
            run_mcp_servers, measurement, workspace_baseline, workspace_lock,
            no_external_publication, publication_authority,
        )
    finally:
        # Released on every way out of the lane, including the exceptions the
        # body raises: a claim left behind would refuse the next run against a
        # workspace nothing is writing to.
        if workspace_lock is not None:
            release_workspace_lock(workspace_lock)


def _claim_existing_workspace(
    args: argparse.Namespace,
    repo: Path,
    workspace: str,
    *,
    resolved: Path | None = None,
) -> "tuple[WorkspaceLock, WorkspaceBaseline, WorktreeRun]":
    """Claim the owner workspace this run was pointed at, and baseline it.

    Both steps are fail-closed and both happen before any worker exists: a
    workspace another lane is writing to is refused with the holder named, and
    a workspace that cannot be read is refused rather than half-claimed. A
    failure after the claim releases it, so a run that never started leaves
    nothing behind.

    ``resolved`` is the directory a caller has already resolved for this same
    workspace — the runner resolves it before its capability gate, so the
    claim, the baseline and the gate's project-config read are provably one
    directory rather than three lookups that merely ought to agree. Absent,
    the workspace string is resolved here as before.
    """

    candidate = (
        resolve_existing_workspace(repo, workspace) if resolved is None else resolved
    )
    lock = acquire_workspace_lock(repo, candidate, lane_name=args.lane_name)
    # The baseline is captured *before* the tool writes its own exclude lines
    # (the scratch entry landed by ``prepare_scratch_directory`` below), so
    # the recorded info-state digest already reflects only the owner's
    # content. ``appended_exclude_lines`` is populated by the tool writers
    # and stamped onto the baseline here, so the post-run reading can apply
    # the same filter to its own digest and the comparison reads the owner's
    # tree rather than a tree the tool itself edited.
    appended_exclude_lines: list[bytes] = []
    try:
        baseline = capture_workspace_baseline(candidate)
        lane = adopt_existing_workspace(
            repo,
            candidate,
            args.lane_name,
            appended_exclude_lines=appended_exclude_lines,
        )
    except Exception:
        release_workspace_lock(lock)
        raise
    if appended_exclude_lines:
        baseline = dataclasses.replace(
            baseline,
            tool_appended_exclude_lines=tuple(appended_exclude_lines),
        )
    return lock, baseline, lane


def _prohibited_git_writes(
    delta_records: "Sequence[WorkspaceDelta]",
) -> list[str]:
    """The paths a worker wrote the owner's index for, by name.

    Existing-workspace mode hands a worker the operator's own checkout and
    tells it to make no git write of any kind. A content, mode or status delta
    is the work it was sent to do — including for a path that was already dirty
    when the run started. A path whose *index record* moved is a different
    thing: git's stage-0 oid, staged mode and conflict stages are what a
    ``git add``, ``git reset`` or ``git rm --cached`` writes, and the one who
    gets to decide what the owner commits next is the owner. So is a path whose
    index *tag* moved: ``git update-index --assume-unchanged`` and
    ``--skip-worktree`` are index writes that change no file, and they are the
    writes that make a path invisible to every ``git status`` reading after
    them — the ones a path-only guard could never see.

    The ways either record moves are already compared, once, in
    :func:`side_lane.worktrees.workspace_deltas`, which is why this reads the
    recorded change kinds rather than re-deriving them: whatever that seam
    found about a path's index is the same comparison the audit publishes. A
    path the baseline already held reports ``index`` when its record moved —
    the two oids, modes or conflict stages differ — and ``flag`` when its tag
    did. A path the baseline never held has no record to move, so that seam
    reports ``index`` for it when the entry it arrived with is not the one HEAD
    gives that path — what ``git add`` writes — which is why an untracked file
    the run created and staged is named here rather than passing as ``added``.
    """

    return [
        delta.path
        for delta in delta_records
        if "index" in delta.changes or "flag" in delta.changes
    ]


def _git_state_phrase(
    component: str, audit: "Mapping[str, object]"
) -> str | None:
    """How the refusal names one git-state component that is not a path.

    ``index``, ``head`` and ``branch`` are phrased at the call site, because
    each is already recorded in its own field there; the rest are read back
    from the recorded component names and the objects they moved. ``None`` for
    anything not named here, so a component added to the record without a
    phrasing cannot silently produce an empty clause.
    """

    if component == "refs":
        names = [str(name) for name in (audit.get("git_state_refs") or ())]
        listed = ", ".join(names[:20]) if names else "(names not recorded)"
        return (
            f"{len(names)} ref(s) moved in the owner's repository: {listed}"
        )
    if component == "worktrees":
        paths = [
            str(path) for path in (audit.get("git_state_worktrees") or ())
        ]
        listed = ", ".join(paths[:20]) if paths else "(paths not recorded)"
        return f"the repository's worktree registrations changed: {listed}"
    if component == "config":
        return "the repository's local config was rewritten"
    if component == "objects":
        return (
            "the object's loose/pack counters changed in the owner's repository "
            "without a matching change to any tracked path, ref, HEAD or branch"
        )
    if component == "reflog":
        return (
            "the reflog changed in the owner's repository without a matching "
            "change to any tracked path, ref, HEAD or branch"
        )
    if component == "info":
        return (
            "Git's own info metadata changed under the owner's .git — the "
            "exclude rules, the attributes or another info file — without a "
            "matching change to any tracked path, ref, HEAD or branch"
        )
    if component == "pseudorefs":
        return (
            "a pseudoref or in-progress operation file changed in the owner's "
            ".git — FETCH_HEAD or ORIG_HEAD for a fetch, the sequencer or "
            "rebase state for a stopped cherry-pick, rebase or merge — without "
            "a matching change to any tracked path, ref, HEAD or branch"
        )
    if component == "object_metadata":
        return (
            "the object metadata changed in the owner's repository — the "
            "commit graph, the multi-pack-index or another file under "
            "objects/info — without moving the object counters or any tracked "
            "path, ref, HEAD or branch"
        )
    return None


def _with_adapter_exclude_lines(
    baseline: "WorkspaceBaseline | None", appended: "Sequence[bytes]"
) -> "WorkspaceBaseline | None":
    """``baseline`` recording the exclude lines an adapter appended too.

    This launch's own writers — the scratch entry, and the lane entry on a
    created lane — are recorded by the claim, which happens before the
    baseline is compared. An *adapter* is the one writer that appends later:
    the Devin adapter adds its local-MCP entry once the run is under way, so
    its bytes have to be folded into the recorded set here, before either the
    success or the failure path measures the workspace. Without them the
    post-run reading of ``info/exclude`` counts the tool's own line as an edit
    the worker made, and refuses a run for a git write that never happened.

    ``None`` for a created lane (no owner workspace is measured there) and for
    a host that appended nothing, so this is a no-op on every path that does
    not have the problem.
    """

    extra = tuple(appended)
    if baseline is None or not extra:
        return baseline
    return dataclasses.replace(
        baseline,
        tool_appended_exclude_lines=baseline.tool_appended_exclude_lines + extra,
    )


def _workspace_after_run(
    lane: WorktreeRun,
    baseline: "WorkspaceBaseline",
    workspace_lock: "WorkspaceLock | None",
    *,
    shared_primary_checkout: bool,
    adapter_error: "Mapping[str, object] | None" = None,
) -> "tuple[dict[str, object], tuple[WorkspaceDelta, ...], bool | None, bool | None, tuple[str, ...], str | None, tuple[str, ...]]":
    """Read the owner's workspace again and shape the audit record from it.

    One seam for both outcomes that reach it: the run whose adapter returned a
    result, and the run whose adapter raised after the worker had already
    written. The second measurement, the per-path deltas it yields, and the
    record built from them are the same work either way — which is the point,
    because the outcome that loses this measurement is exactly the outcome that
    must still be *recorded*, not reported as a tree that did not change.

    Returns the record, the deltas, whether HEAD and the branch moved, the
    paths nothing could be measured for, why the second read failed (or
    ``None``), and the git-state components the run moved. When the read
    failed, the record carries ``None`` in every "after" field: nothing about
    the workspace after the run is claimed, and the baseline it could not be
    compared against is still published as what the run knew.

    The record also answers, separately from the deltas, whether the worker
    made a git write this mode forbids — the index, HEAD, the branch, the ref
    store, the local config and the worktree registrations. That is what the
    caller's delivery verdict turns on, and it is here rather than there
    because the durable record is what an operator reads afterwards: a run
    refused for a prohibited write has to be able to say which one. The
    components that are not paths are recorded as component names, with the ref
    names and worktree paths beside them, so a refusal can name the ref a
    worker moved rather than reporting that *something* moved.
    """

    after: "WorkspaceBaseline | None" = None
    after_error: str | None = None
    try:
        # Pass ``tool_appended_exclude_lines`` only when this launch actually
        # wrote something — the no-arg form keeps the call signature
        # identical to the original tests' ``capture_workspace_baseline(path)``
        # wrappers, and the empty-tuple filter is a no-op anyway.
        if baseline.tool_appended_exclude_lines:
            after = capture_workspace_baseline(
                lane.worktree,
                tool_appended_exclude_lines=baseline.tool_appended_exclude_lines,
            )
        else:
            after = capture_workspace_baseline(lane.worktree)
    except WorktreeError as exc:
        after_error = str(exc)
    record: dict[str, object] = {
        "workspace": str(lane.worktree),
        "branch": lane.branch,
        "linked_worktree": is_linked_worktree(lane.worktree),
        # Recorded, not inferred: selecting the shared primary checkout is a
        # deliberate act this run must be able to show, and it is the one case
        # where the source-mutation check has nothing to compare against — the
        # lane's tree IS the coordinator checkout.
        "shared_primary_checkout": shared_primary_checkout,
        "baseline_sha256": baseline.sha256,
        "baseline_paths": len(baseline.entries),
        # An adapter that raised is not a failed measurement: the workspace was
        # still read. Kept beside `after_error` so a reader can tell "the
        # worker's run ended in an error" from "the workspace could not be
        # read back", which are different failures with different remedies.
        "adapter_error": dict(adapter_error) if adapter_error is not None else None,
        "lock": (
            None
            if workspace_lock is None
            else {
                "path": str(workspace_lock.path),
                "reclaimed": workspace_lock.reclaimed,
            }
        ),
    }
    if after is None:
        record.update(
            {
                "branch_after": None,
                "branch_moved": None,
                "head": baseline.head,
                "head_after": None,
                "head_moved": None,
                "unverified_paths": sorted(baseline.unverified_paths()),
                "after_error": after_error,
                "deltas": [],
                # No second reading, so nothing was compared: `Null` in both,
                # never `False`, which would be the claim that nothing moved.
                "git_state_mutated": None,
                "git_state_paths": None,
                "git_state_components": None,
                "git_state_refs": None,
                "git_state_worktrees": None,
            }
        )
        return record, (), None, None, (), after_error, ()
    records = workspace_deltas(baseline, after)
    head_moved = after.head != baseline.head
    # A switch to another branch that stands on the same commit moves neither a
    # path nor HEAD. Comparing HEAD alone would report such a workspace as
    # exactly as it was found, when the owner's checkout is now on a different
    # branch and their next commit lands elsewhere.
    branch_moved = after.branch != baseline.branch
    # Paths neither the baseline nor this look could measure. Nothing about them
    # was compared, so they are not comparable, and no verdict is rendered from
    # them below.
    unmeasured = tuple(
        sorted(
            set(baseline.unverified_paths()) | set(after.unverified_paths())
        )
    )
    # A git write this mode forbids: the index half is per path (`git add`,
    # `git reset`, `git rm --cached`, and the tags `git update-index` sets),
    # HEAD and the branch are the workspace-wide half, and the ref store, the
    # local config and the worktree registrations are the parts of git's state
    # that are neither a path nor HEAD — `git update-ref`, `git symbolic-ref`,
    # `git config` and `git worktree add` all move one of them without moving
    # any path this comparison holds. Recorded as one answer because the
    # verdict is one answer, with the paths and the component names kept beside
    # it so the refusal names what moved. A command-string deny rule cannot be
    # complete over an arbitrary shell; this comparison is what makes the guard
    # effective rather than declarative.
    git_state_paths = _prohibited_git_writes(records)
    git_state = baseline.git_state
    after_git_state = after.git_state
    git_state_dimensions = git_state.moved_by(after_git_state)
    moved_refs = git_state.moved_refs(after_git_state)
    moved_worktrees = git_state.moved_worktrees(after_git_state)
    git_state_components = tuple(
        name
        for name, moved in (
            ("index", bool(git_state_paths)),
            ("head", bool(head_moved)),
            ("branch", bool(branch_moved)),
        )
        if moved
    ) + git_state_dimensions
    record.update(
        {
            "branch_after": after.branch,
            "branch_moved": branch_moved,
            "head": baseline.head,
            "head_after": after.head,
            "head_moved": head_moved,
            "unverified_paths": list(unmeasured),
            "after_error": None,
            "deltas": [delta.as_dict() for delta in records],
            "git_state_mutated": bool(git_state_components),
            "git_state_paths": list(git_state_paths),
            # What moved, in the order the refusal reads it: the two git-wide
            # answers this mode already had, then the components of the state
            # that is not a path at all. `null` above when nothing was
            # compared, never `[]` — a comparison nobody made is not a list of
            # things that did not move.
            "git_state_components": list(git_state_components),
            "git_state_refs": list(moved_refs),
            "git_state_worktrees": list(moved_worktrees),
        }
    )
    return (
        record,
        records,
        head_moved,
        branch_moved,
        unmeasured,
        None,
        git_state_components,
    )


def _assignment_link(
    assignment: AssignmentRecord | None,
    measurement: "Mapping[str, Any] | None",
) -> "dict[str, object] | None":
    """The audit's pointer to the assignment sidecar this run published.

    ``None`` is the explicit unmeasured case — no measurement was requested for
    this run. It never means "measurement was lost": a sidecar that could not be
    published aborted the run before an adapter started.
    """

    if assignment is None:
        return None
    return {
        "path": str(assignment.path),
        "sha256": assignment.sha256,
        "task_id": measurement["task_id"] if measurement else None,
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "reused": assignment.reused,
    }


def _adapter_failure(
    error: BaseException, secret: str | None
) -> dict[str, object]:
    """The recorded failure of a run whose adapter raised before returning.

    The exception's type and its redacted message, and nothing else: no prompt,
    no file content, no traceback, and no credential — the message goes through
    the same provider-secret redaction every other recorded string does. The
    two verdict fields are ``null`` rather than ``False`` because neither a
    delivery nor a verification was ever rendered for this run; ``False`` would
    claim one was, and came out negative.
    """

    return {
        "stage": "adapter",
        "error_type": type(error).__name__,
        "message": redact_provider_secret(str(error), secret),
        "delivered": None,
        "verified": None,
    }


def _record_failed_workspace_run(
    *,
    lane: WorktreeRun,
    repo: Path,
    args: argparse.Namespace,
    prompt: str,
    workspace_baseline: "WorkspaceBaseline | None",
    workspace_lock: "WorkspaceLock | None",
    shared_primary_checkout: bool,
    source_baseline: "frozenset[str] | None",
    read_roots: Sequence[Path],
    web_domains: Sequence[str],
    run_mcp_servers: "Mapping[str, Any] | None",
    skill_catalog: "Sequence[Mapping[str, object]]",
    execute_profile: str | None,
    assignment: AssignmentRecord | None,
    measurement: "Mapping[str, Any] | None",
    secret: str | None,
    error: BaseException,
    publication_authority: "Mapping[str, object]",
) -> Path:
    """Write the run record for an owner workspace whose adapter raised.

    An owner workspace has no lane worktree and no branch this run created, so
    unlike a created lane it leaves no durable evidence of *which* workspace a
    worker touched unless this record is written. The adapter never returned a
    result, so there is no stdout, no stderr and no provider verdict to record —
    only the workspace as it now stands, measured best effort, and the failure.

    The second measurement goes through the same seam the successful path uses,
    so the deltas reported here are computed identically. It is best effort: if
    the workspace cannot be read at all, the record says so (``after_error``)
    rather than claiming an unchanged tree. A failure to write this record is
    not caught here — the caller turns it into an explicit second error, because
    a run that leaves the owner's workspace with no record must not look like a
    run whose record was written.

    ``publication_authority`` is passed through and recorded exactly as the
    success path records it. The authority is this run's own — the task's
    ``--no-external-publication`` refusal and the host's enforcement of it —
    and it was assembled before the worker started, so a run that raised still
    carried it. Omitting it here made the one record an operator reads after a
    failure the only record that could not answer whether the lane was allowed
    to publish: the two outcomes would disagree about the same run's authority,
    and a reader could not tell a failed refusing lane from a failed one that
    carried no refusal at all.

    The coordinator checkout is compared here by the same rule the success path
    applies, and for the same reason: same-user execution is not an OS sandbox,
    so a worker whose lane raised can have written into the checkout on its way
    out. Reporting the workspace delta alone answered only half the question —
    and the failure record, being the one an operator reads after a raise, is
    exactly where ``source_changes: []`` would be believed. So the comparison
    runs here too, and a comparison that could not be taken is recorded as
    unverified rather than as a clean checkout: ``source_baseline`` is ``None``
    when the run raised before the checkout was ever read, and the reason is
    written to the record instead of the empty answer.
    """

    failure = _adapter_failure(error, secret)
    workspace_audit: "dict[str, object] | None" = None
    after_error: str | None = None
    unmeasured: tuple[str, ...] = ()
    if workspace_baseline is not None:
        (
            workspace_audit,
            _deltas,
            _head_moved,
            _branch_moved,
            unmeasured,
            after_error,
            _git_state_components,
        ) = _workspace_after_run(
            lane,
            workspace_baseline,
            workspace_lock,
            shared_primary_checkout=shared_primary_checkout,
            adapter_error=failure,
        )
    try:
        status = git_status(lane)
    except WorktreeError:
        # The tree could not be described; that is not a reason to lose the
        # record that names the workspace and the failure.
        status = ""
    # The coordinator-checkout comparison, by the success path's own rules: it
    # applies to an execute lane that is NOT itself that checkout, a run whose
    # baseline was never taken is unverified rather than clean, and a failed
    # comparison is unverified too. `source_changes` stays empty where the
    # question does not apply — a review lane carries no source guarantee, and
    # a workspace that IS the coordinator checkout has no outside to compare
    # against — so `[]` there means "not asked", never "asked and clean".
    source_changes: "tuple[str, ...]" = ()
    source_check_error: str | None = None
    if args.mode == "execute" and not shared_primary_checkout:
        if source_baseline is None:
            source_check_error = (
                "the coordinator checkout was never read before the worker "
                "started, so this run cannot say whether it changed"
            )
        else:
            try:
                source_changes = source_mutations(repo, source_baseline)
            except WorktreeError as exc:
                source_check_error = str(exc)
    return write_audit(
        lane,
        host=args.host,
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        prompt=prompt,
        # Both of the mode's own failure codes mean "no usable completion". The
        # unverified one is for the run whose workspace could not be read back
        # OR that holds a path nothing could be measured for OR whose
        # coordinator checkout could not be compared — the same rule the
        # success path applies, because the record is written from the same
        # measurement: a comparison that was never made for part of the tree is
        # not a verdict, whichever way the run ended. Either way nothing is
        # delivered and nothing is verified, which is what the record's two
        # verdict fields say.
        exit_status=(
            LANE_DELIVERY_UNVERIFIED
            if after_error is not None or unmeasured or source_check_error is not None
            else LANE_NOT_DELIVERED
        ),
        status=status,
        read_roots=[str(root) for root in read_roots],
        web_domains=list(web_domains),
        skill_catalog=skill_catalog,
        source_changes=list(source_changes),
        source_check_unverified=source_check_error,
        run_mcp_servers=(
            [
                {"server": name, "config": str(Path(args.mcp_config).expanduser())}
                for name in audit_names(run_mcp_servers)
            ]
            if run_mcp_servers
            else []
        ),
        execute_profile=execute_profile,
        assignment=_assignment_link(assignment, measurement),
        existing_workspace=workspace_audit,
        failure=failure,
        # Same rule as the success path further down: an execute lane's
        # authority is recorded, a review lane carries no grant this narrows.
        # The mode is not re-derived from what reached this function — the
        # caller has already refused every other mode for this lane — but the
        # two records must be built by the same rule, so the guard is repeated
        # rather than assumed.
        publication=publication_authority if args.mode == "execute" else None,
    )


def _launch_in_lane(
    args: argparse.Namespace,
    repo: Path,
    prompt: str,
    lane: WorktreeRun,
    provider_config: Mapping[str, Any],
    model_config: Mapping[str, Any],
    executable: str,
    capabilities: Sequence[str],
    execute_profile: str | None,
    report_only: bool,
    report_deliverable: bool,
    report_flag: str,
    browser_report: bool,
    read_roots: Sequence[Path],
    web_domains: Sequence[str],
    run_mcp_servers: "Mapping[str, Any] | None",
    measurement: "Mapping[str, Any] | None",
    workspace_baseline: "WorkspaceBaseline | None",
    workspace_lock: "WorkspaceLock | None",
    no_external_publication: bool,
    publication_authority: Mapping[str, object],
) -> int:
    """Everything a lane does once it has a worktree to run in.

    Split out of :func:`_launch` for one reason: an existing owner workspace is
    held under a lock for the whole life of the lane, so the release has to run
    on the exception paths too. ``_launch`` builds or claims the workspace and
    releases the claim in a ``finally``; this function is the body that must
    run inside it. A created lane's arguments are unchanged — it simply passes
    ``None`` for the workspace baseline and the lock.
    """

    # Exact-boolean read, the same way `report_only` is read above and for the
    # same reason: a lane object that carries no such attribute — an adapter
    # mock in a test, a caller's own record — must select the default isolated
    # behavior, never a mode nothing turned on. Everything below this line
    # branches on this one value rather than re-reading the attribute, so no
    # arm can disagree with another about which mode the run is in.
    existing_workspace_mode = lane.existing_workspace is True

    # Execute lanes only, at this shared preparation layer rather than in any
    # adapter: every host receives the same catalog in its task context and
    # the same materialized files inside its worktree, so local and cloud
    # workers read identical pinned skill instructions without touching user
    # settings, CODEX_HOME, or MCP configuration. Review lanes are unchanged.
    # Delivery is fail-closed: a bundle that cannot validate aborts the run
    # before a worker starts (the pin is only meaningful if drift is fatal),
    # and the abort disposes the lane worktree it had begun preparing. Named
    # skills (--skill) are additive to the discipline defaults and closed
    # under references; a private skill unavailable outside a dev-tools
    # checkout fails here, naming the flag to drop.
    skill_catalog: list[dict[str, object]] = []
    assignment: AssignmentRecord | None = None
    secret: str | None = None
    report_baseline: report_stop_hook.ReportBaseline | None = None
    # Bound here rather than inside the `try`, because the failure path records
    # the coordinator-checkout comparison too and a run that raised before the
    # snapshot has no baseline to compare. `None` means exactly that: the
    # checkout was never read, so the failure record says unverified instead of
    # claiming a delta it never measured.
    source_baseline: "frozenset[str] | None" = None
    #: ``info/exclude`` line bytes an adapter appends *during* the run — the
    #: Devin adapter's local-MCP entry is the one writer that happens after the
    #: claim. The claim recorded its own writes before the baseline was
    #: compared; these are folded into the baseline's record out of the lane
    #: below, on the success and the failure path alike, so the post-run
    #: reading normalizes the tool's own write instead of reporting it as the
    #: worker's. Empty for every host that appends nothing.
    adapter_appended_exclude_lines: list[bytes] = []
    try:
        if report_deliverable:
            # Run-bound freshness, captured before anything can write it: what
            # the fixed report path already holds. A lane worktree is added from
            # HEAD, so a repository that tracks SIDE_LANE_REPORT.md hands every
            # new lane a complete-looking report no worker wrote, and an
            # unrelated commit or an empty session would then satisfy the gate.
            # Captured here, rather than in the adapter, so the in-loop Stop
            # hook and the acceptance below compare against one recorded state.
            # The inherited file is copied into the lane's ignored scratch
            # (never the coordinator checkout, never deleted) so a worker
            # overwriting it erases no history. A `--report-deliverable` lane
            # needs this exactly as much as a `--report-only` one: it is the
            # verdict's own input, not the hook's.
            try:
                report_baseline = report_stop_hook.capture_report_baseline(
                    lane.worktree / report_stop_hook.REPORT_NAME,
                    preserve_dir=lane.worktree / SCRATCH_DIR_NAME,
                )
            except report_stop_hook.UnsafeReportPath as exc:
                raise SideLaneError(
                    f"{report_flag} cannot start: {exc}. No honest per-run "
                    "baseline can be taken from it; remove or replace it before "
                    "dispatching."
                ) from exc
        if args.mode == "execute":
            # `--skill` is declared by the `run` subparser, like `--verify`
            # and `--no-publish` below, so read it the same tolerant way.
            named_skills = tuple(getattr(args, "skill", None) or ())
            records = deliver_skills(lane.worktree, skills=named_skills)
            skill_catalog = [record.as_dict() for record in records]
            note = catalog_note(records)
            if note:
                prompt = prompt + "\n\n" + note
        if provider_config["auth_method"] == "oauth":
            require_native_oauth(args.host, executable=executable)
        else:
            secret = read_credential(provider_config["credential_service"])
        # Snapshot the coordinator checkout immediately before the worker
        # starts: whatever the source tree already carried is baseline, and
        # only the delta after the run is reported. Execute lanes only — a
        # review lane never gets write authority, and its whole worktree is
        # disposable, so the extra git call buys nothing there.
        source_baseline = (
            snapshot_source(repo) if args.mode == "execute" else frozenset()
        )
        if measurement is not None:
            # The assignment is published here — after every precondition that
            # can still fail for free, and BEFORE the adapter is invoked — so
            # the sidecar exists for exactly the runs a worker could have
            # started. A conflict or an unpublishable sidecar raises out of
            # this block, which disposes the lane without ever starting a
            # worker: an assignment is never retrofitted onto a run that
            # already executed. The instant is this runner's own UTC clock,
            # recorded once and never revised; the identity is what the route
            # was *configured* with, not what the provider later resolved to.
            assignment = write_assignment(
                lane,
                measurement=measurement,
                assigned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                planned={
                    "provider": args.provider,
                    "model": args.model,
                    "host": args.host,
                    "gateway": provider_config["gateway"],
                },
            )
        if args.host == "codex":
            from side_lane.adapters.codex import run_codex

            result = run_codex(
                executable=executable,
                repo=repo,
                worktree=lane.worktree,
                provider=args.provider,
                model=args.model,
                provider_config=provider_config,
                model_config=model_config,
                prompt=prompt,
                mode=args.mode,
                capabilities=capabilities,
                support_dir=host_support_dir(args.host, executable),
                secret=secret,
                read_roots=read_roots,
                web_domains=web_domains,
                run_mcp_servers=run_mcp_servers,
                report_deliverable=report_deliverable,
                existing_workspace=existing_workspace_mode,
                no_external_publication=no_external_publication,
            )
        elif args.host == "claude":
            from side_lane.adapters.claude import launch

            result = launch(
                executable=executable,
                repo=repo,
                worktree=lane.worktree,
                provider=args.provider,
                model=args.model,
                provider_config=provider_config,
                model_config=model_config,
                prompt=prompt,
                mode=args.mode,
                capabilities=capabilities,
                secret=secret,
                read_roots=read_roots,
                web_domains=web_domains,
                run_mcp_servers=run_mcp_servers,
                report_only=report_only,
                report_deliverable=report_deliverable,
                report_baseline=report_baseline,
                execute_profile=execute_profile,
                existing_workspace=existing_workspace_mode,
                no_external_publication=no_external_publication,
            )
        else:
            from side_lane.adapters.devin import launch

            result = launch(
                executable=executable,
                repo=repo,
                worktree=lane.worktree,
                provider=args.provider,
                model=args.model,
                provider_config=provider_config,
                model_config=model_config,
                prompt=prompt,
                mode=args.mode,
                capabilities=capabilities,
                read_roots=read_roots,
                web_domains=web_domains,
                run_mcp_servers=run_mcp_servers,
                report_deliverable=report_deliverable,
                execute_profile=execute_profile,
                existing_workspace=existing_workspace_mode,
                no_external_publication=no_external_publication,
                appended_exclude_lines=adapter_appended_exclude_lines,
            )
    except Exception as exc:
        # Whatever an adapter appended to the owner's exclude file before it
        # raised is this tool's own write, and the measurement below still has
        # to read the owner's file rather than the tool's line.
        workspace_baseline = _with_adapter_exclude_lines(
            workspace_baseline, adapter_appended_exclude_lines
        )
        # A created lane is disposable and is removed so a failed preparation
        # strands nothing; an existing workspace is the owner's and is left
        # exactly as it stands, because this run never owned it to dispose of.
        if not existing_workspace_mode:
            dispose_clean_worktree(lane)
            raise
        # An owner workspace leaves no lane worktree and no branch this run
        # created behind it, so — unlike a created lane — a run that raises has
        # nothing at all on disk naming which workspace a worker touched. The
        # adapter never returned a result, so there is no stdout, no stderr and
        # no provider verdict either. Measure the workspace as it now stands,
        # best effort, and write that failure to the same durable record every
        # other outcome reaches, then re-raise the original exception unchanged:
        # the caller sees exactly the failure it would have seen, and the record
        # agrees that nothing was delivered or verified.
        try:
            _record_failed_workspace_run(
                lane=lane,
                repo=repo,
                args=args,
                prompt=prompt,
                workspace_baseline=workspace_baseline,
                workspace_lock=workspace_lock,
                shared_primary_checkout=bool(
                    lane.worktree.resolve() == repo.resolve()
                ),
                source_baseline=source_baseline,
                read_roots=read_roots,
                web_domains=web_domains,
                run_mcp_servers=run_mcp_servers,
                skill_catalog=skill_catalog,
                execute_profile=execute_profile if args.mode == "execute" else None,
                assignment=assignment,
                measurement=measurement,
                secret=secret,
                error=exc,
                publication_authority=publication_authority,
            )
        except Exception as record_error:
            # The record itself could not be written. That is a second, worse
            # failure — the operator's workspace now has no record of this run —
            # and it must not be swallowed into the original exception as if the
            # record had been written. Raised as the cause, so the original
            # failure is still what propagates and the missing record is visible
            # in the traceback rather than inferred from its absence.
            raise exc from record_error
        raise
    # The adapter returned: fold whatever it appended to the owner's exclude
    # file into the baseline's record before the workspace is measured again,
    # so the tool's own line is normalized out of the comparison rather than
    # reported as the worker's git write.
    workspace_baseline = _with_adapter_exclude_lines(
        workspace_baseline, adapter_appended_exclude_lines
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    summary = result.as_dict()
    status = git_status(lane)
    # Whether the worker's own deltas can be read from the workspace it ran in.
    # An existing workspace is measured against the content-and-index baseline
    # captured before it started, because a workspace handed to a lane is
    # expected to arrive dirty and a path-only or HEAD-relative answer would
    # call an already-dirty file the worker rewrote "unchanged".
    workspace_delta_records: tuple[WorkspaceDelta, ...] = ()
    workspace_audit: dict[str, object] | None = None
    workspace_head_moved: bool | None = None
    workspace_branch_moved: bool | None = None
    workspace_unmeasured: tuple[str, ...] = ()
    #: The git write this mode forbids, and the paths it landed on. `None` —
    #: not `False` — whenever no comparison of the workspace was made, on the
    #: same contract as `workspace_unmeasured` above: an unread workspace
    #: cannot be reported as one the worker left alone.
    workspace_git_state_mutated: bool | None = None
    workspace_git_state_paths: tuple[str, ...] | None = None
    #: Which parts of the workspace's git state moved, by component name —
    #: `index`, `head`, `branch`, `refs`, `config`, `worktrees`. Kept beside
    #: the paths because the components that are not paths have no path to be
    #: named by, and a refusal that only said "something moved" would leave the
    #: owner to work out what. Empty when the comparison found nothing.
    workspace_git_state_components: tuple[str, ...] = ()
    #: Why the workspace could not be read again after the worker ran, if it
    #: could not be. This is the one failure that must never be silent: the
    #: whole safety story of this mode is a measurement taken before the run
    #: and compared with one taken after, so the outcome that loses the second
    #: measurement is the outcome that must still be recorded — as unverified,
    #: with the run's exit code and its durable record saying the same thing.
    #: Left unguarded, a `WorktreeError` here propagated out of the lane, past
    #: the audit, and left no run record at all for the operator to find.
    workspace_after_error: str | None = None
    same_checkout = bool(
        existing_workspace_mode and lane.worktree.resolve() == repo.resolve()
    )
    if existing_workspace_mode and workspace_baseline is not None:
        (
            workspace_audit,
            workspace_delta_records,
            workspace_head_moved,
            workspace_branch_moved,
            workspace_unmeasured,
            workspace_after_error,
            workspace_git_state_components,
        ) = _workspace_after_run(
            lane,
            workspace_baseline,
            workspace_lock,
            shared_primary_checkout=same_checkout,
        )
    # Compare the coordinator checkout against its pre-dispatch baseline
    # BEFORE writing the audit so the audit itself records what changed. A
    # failed comparison is not silently clean: the run reports the check as
    # unverified and refuses to claim a clean delivery below. When the lane's
    # workspace IS the coordinator checkout there is no outside to compare
    # against — the worker's writes there are the delivery, recorded as
    # workspace deltas above — so the check is recorded as not applicable
    # rather than reported as a clean checkout it never examined.
    source_changes: tuple[str, ...] | None = ()
    source_check_error: str | None = None
    if same_checkout:
        summary["source_check_applies"] = False
    elif args.mode == "execute":
        try:
            source_changes = source_mutations(repo, source_baseline)
        except WorktreeError as exc:
            source_changes = None
            source_check_error = str(exc)
    # Preserve the provider's outcome separately from this runner's check.
    # Machine readers must see the same source-check failure as the exit code.
    # A path in the owner's workspace this run could not measure — unreadable,
    # or of a kind it never opens — is the same kind of failure as an unreadable
    # coordinator checkout: the run could not look, so it is not clean. So is a
    # second reading of the workspace that could not be taken at all. All three
    # are folded in here so the exit code a machine reads and the status the
    # audit records are the same answer.
    workspace_unverified = bool(workspace_unmeasured) or (
        workspace_after_error is not None
    )
    source_exit_status = result.returncode or (
        LANE_DELIVERY_UNVERIFIED
        if source_check_error is not None or workspace_unverified
        else LANE_SOURCE_MUTATED if source_changes else 0
    )
    summary["provider_exit_status"] = result.returncode
    summary["exit_status"] = source_exit_status
    # The operator-facing record of which tool profile the worker actually ran
    # under, beside the capability set it ran with: a lane that defaulted to the
    # local developer profile from its route's own location and one that was
    # narrowed back to the allowlist are otherwise indistinguishable from the
    # receipt alone. `None` for a review lane, which has no profile.
    summary["execute_profile"] = execute_profile if args.mode == "execute" else None
    # The terminal audit links the assignment on every outcome that reaches it:
    # a run that failed, or verified nothing, still points at what it was
    # assigned. `None` is the explicit unmeasured case — no measurement was
    # requested for this run. It never means "measurement was lost"; a sidecar
    # that could not be published aborted the run before an adapter started,
    # and nothing downstream could accept that worker's result either.
    assignment_link = _assignment_link(assignment, measurement)
    audit = write_audit(
        lane,
        host=result.host,
        mode=args.mode,
        provider=result.provider,
        gateway=result.gateway,
        auth_method=result.auth_method,
        billable=result.billable,
        model=result.model,
        prompt=prompt,
        exit_status=source_exit_status,
        status=status,
        stdout=result.stdout,
        stderr=result.stderr,
        requested_model=result.requested_model,
        resolved_model=result.resolved_model,
        usage=result.usage,
        provider_artifact=result.provider_artifact,
        read_roots=[str(root) for root in read_roots],
        web_domains=list(web_domains),
        execute_profile=execute_profile if args.mode == "execute" else None,
        skill_catalog=skill_catalog,
        run_mcp_servers=(
            [
                {"server": name, "config": str(Path(args.mcp_config).expanduser())}
                for name in audit_names(run_mcp_servers)
            ]
            if run_mcp_servers
            else []
        ),
        source_changes=list(source_changes or ()),
        source_check_unverified=source_check_error,
        assignment=assignment_link,
        existing_workspace=workspace_audit,
        # Execute lanes only: a review lane carries no grant this narrows, and
        # the flag is refused there before a lane exists.
        publication=publication_authority if args.mode == "execute" else None,
    )
    summary.update(
        {
            "assignment": assignment_link,
            "branch": lane.branch,
            "worktree": str(lane.worktree),
            "git_status": status,
            "audit": str(audit),
            "result_artifact": str(audit),
            "skill_catalog": skill_catalog,
            "web_domains": list(web_domains),
            "run_mcp_servers": list(audit_names(run_mcp_servers)) if run_mcp_servers else [],
            # None means the comparison itself failed — never report that as
            # a clean checkout, same contract as delivered/verified above.
            "source_mutated": None if source_changes is None else bool(source_changes),
            "source_changes": list(source_changes or ()),
            "existing_workspace": workspace_audit,
            "workspace_deltas": [
                delta.as_dict() for delta in workspace_delta_records
            ],
        }
    )
    if source_check_error is not None:
        summary["source_check_unverified"] = source_check_error
    if args.mode == "review":
        dispose_clean_worktree(lane)
        summary["worktree_disposed"] = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        return result.returncode

    # Execute mode: a zero exit is not evidence the work landed. A worker can
    # write correct files, exit 0, and leave them untracked — the run then
    # reports success for work that vanishes with the worktree. Ask the tree.
    try:
        delivery = lane_delivery(lane)
    except WorktreeError as exc:
        # Fail closed: this repository fails closed when worktree state is
        # uncertain, and an unverifiable tree is exactly that. An earlier
        # revision kept the worker's exit code here, which turned "we could not
        # look" into a green run — the very failure this check exists to
        # prevent, one level up.
        summary["delivered"] = None
        # Same contract as below: no verdict was rendered, so none is claimed.
        summary["verified"] = None
        summary["delivery_unverified"] = str(exc)
        # The publication record is not conditional on reaching a delivery
        # verdict: this run carried the authority in it whatever the tree turned
        # out to be, and the runner made no push attempt because it stopped
        # here. The reason says exactly that — the runner skipped, and the
        # reason it skipped is the inspection failure — so neither delivery nor
        # publication is claimed, and `worker_publication_verified` stays
        # "not-checked" because this run observed no publication by anyone.
        summary["publication"] = _publication_record(
            publication_authority, "skipped", "delivery-unverified"
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(
            f"side-lane: could not verify lane delivery, so it is not claimed: {exc}",
            file=sys.stderr,
        )
        return result.returncode or LANE_DELIVERY_UNVERIFIED

    summary["committed"] = delivery.committed
    summary["uncommitted"] = list(delivery.uncommitted)
    if existing_workspace_mode:
        # The lane-tree rule above is not this mode's rule and saying so is the
        # point: it asks whether HEAD moved past a commit this run created and
        # whether the tree is clean, and a workspace handed to a lane is dirty
        # on purpose, on a branch this run never created. Restating it here as
        # "committed: false, uncommitted: <everyone's preexisting work>" would
        # read as a failed lane. What the worker did is the delta against the
        # baseline; everything the baseline already carried is somebody else's
        # work, neither claimed nor failed by this run, and left untouched.
        summary["committed"] = bool(workspace_head_moved)
        summary["uncommitted"] = [delta.path for delta in workspace_delta_records]
        summary["branch_moved"] = bool(workspace_branch_moved)
        summary["unverified_paths"] = list(workspace_unmeasured)
        summary["delivery_rule"] = "existing-workspace-deltas"
        # A delta is the ordinary outcome here; a git write is not, so it is
        # recorded on its own line rather than folded into the deltas above —
        # a commit makes every committed path a delta too, and reading the
        # delta list would not tell the two apart. `None` in both fields when
        # no comparison was made, for the same reason `delivered` is `None`
        # below: a movement nobody looked for is not a movement nobody made.
        if workspace_after_error is not None or workspace_unmeasured:
            summary["git_state_mutated"] = None
            summary["git_state_paths"] = None
            summary["git_state_components"] = None
        else:
            workspace_git_state_paths = tuple(
                _prohibited_git_writes(workspace_delta_records)
            )
            workspace_git_state_mutated = bool(workspace_git_state_components)
            summary["git_state_mutated"] = workspace_git_state_mutated
            summary["git_state_paths"] = list(workspace_git_state_paths)
            summary["git_state_components"] = list(workspace_git_state_components)
        if workspace_after_error is not None:
            # `committed`/`branch_moved` above are False only because nothing
            # was read back. Said plainly here, so a machine reader does not
            # take the absence of a measured movement for a measured absence.
            summary["after_check_unverified"] = workspace_after_error
    # The runner's own look at the report, on the same rule — and against the
    # same baseline — the in-loop Stop hook applied. A report is only this
    # run's artifact when it differs from what the worktree already held at
    # lane start: non-emptiness and a recent mtime cannot distinguish a real
    # delivery from a report the repository carried at HEAD. For a report-only
    # lane this is the delivery verdict itself, and it must be reached before
    # both publication and the summary, so neither a committed branch nor a
    # stale report can be mistaken for an accepted lane.
    report_present: bool | None = None
    report_state: str | None = None
    report_only_blocker: str | None = None
    if report_deliverable:
        if report_baseline is not None:
            report_path = report_baseline.report_path
        report_state = report_stop_hook.report_freshness_state(report_baseline)
        report_present = report_state == "current"
        summary["report_present"] = report_present
        summary["report_state"] = report_state
        summary["report_path"] = str(report_path)
        summary["report_preexisting"] = bool(
            report_baseline is not None and report_baseline.preexisting
        )
        summary["report_preserved"] = (
            None
            if report_baseline is None or report_baseline.preserved_path is None
            else str(report_baseline.preserved_path)
        )
        # This lane's deliverable is the report, so this — not the execute
        # lane's commit-plus-clean-tree rule — is its delivery verdict. The
        # lane tree verdict above is still wanted and still authoritative for
        # what it says: whether the worker committed, and every path it left
        # uncommitted, both of which this decision reads.
        report_artifacts = _report_only_lane_artifacts(lane.worktree, report_path)
        browser_artifacts = _report_only_browser_artifacts(
            lane.worktree, delivery, browser=browser_report
        )
        unexpected = (
            ()
            if report_artifacts is None
            else _report_only_unexpected_paths(
                delivery, report_artifacts, browser_artifacts
            )
        )
        summary["report_only_unexpected_paths"] = list(unexpected)
        summary["report_only_committed"] = delivery.committed
        report_only_blocker = _report_only_blocker(
            report_state=report_state,
            report_path=report_path,
            report_artifacts=report_artifacts,
            committed=delivery.committed,
            unexpected=unexpected,
            report_deliverable_flag=report_flag,
            browser=browser_report,
        )
    # 2026-09-17: a lane told to run the test suite ran it 18 times, saw
    # JSONDecodeError nine times, committed the failing tests anyway, and
    # exited 0 — caught only because a human re-ran the suite by hand. A
    # lane's claim about tests is prose in a transcript; the runner must
    # check. Verification runs BEFORE publication on purpose: publication must
    # still happen either way, because preserving the branch is what stops
    # work being stranded on one machine, and failing work is exactly the work
    # someone needs to be able to look at.
    #
    # Whether a verification may be attempted at all. For an execute lane this
    # is unchanged — a delivered lane and nothing else. A report-only lane
    # adds this run's other outcomes to the gate, because the worker's exit
    # status and the coordinator checkout are already decided by the time a
    # verification would run: spending a shell command on a run that has
    # failed for either reason changes nothing about the verdict, and that
    # command is itself free to write into the lane, the report, or the
    # checkout. The verdict, not the lane's git state, is what a report-only
    # lane is gated on — gating it on `delivery.delivered` made --verify a
    # silent no-op for that lane, which is exactly the "the operator believes
    # a check ran" failure this runner refuses elsewhere.
    if report_deliverable:
        verify_eligible = (
            report_only_blocker is None
            and not result.returncode
            and source_check_error is None
            and not source_changes
        )
    elif existing_workspace_mode:
        # `--verify` is refused alongside `--existing-workspace` before any
        # workspace is claimed, so no run can arrive here eligible. The arm
        # exists so the flag is a refusal rather than a silent no-op reached
        # through a lane-tree rule this mode does not use.
        verify_eligible = False
    else:
        verify_eligible = delivery.delivered
    verify = None
    verification_changes: tuple[str, ...] = ()
    if report_deliverable:
        summary["report_only_verification_changes"] = []
    if getattr(args, "verify", None) and verify_eligible:
        before = None
        if report_deliverable:
            try:
                report_identity = report_stop_hook.report_identity(report_path)
            except report_stop_hook.UnsafeReportPath:
                report_identity = None
            # The same facts the after state reads, taken from the inputs the
            # verdict itself was reached on — including the accepted artifact
            # identities, through the same helper. An identity-free before
            # state can never equal an after state that carries them, so a
            # report lane that left an artifact and ran a successful --verify
            # was refused for a change the command had not made.
            before = _ReportOnlyState(
                committed=delivery.committed,
                unexpected=unexpected,
                source_changes=tuple(source_changes or ()),
                report_identity=report_identity,
                artifacts=_artifact_identities(lane.worktree, browser_artifacts),
            )
        verify = verify_lane(lane, args.verify)
        summary["verified"] = verify.passed
        summary["verify_command"] = verify.command
        summary["verify_exit"] = verify.exit_code
        summary["verify_output"] = verify.output
        if report_deliverable:
            # The command may have committed, left files, rewritten the report
            # the verdict was just reached on, or written into the coordinator
            # checkout. Re-read the tree instead of assuming it did not: the
            # lane that gets accepted must be the lane that was judged.
            after = _report_only_state(
                lane, report_path, report_artifacts or frozenset(), repo,
                source_baseline, browser=browser_report,
            )
            verification_changes = _report_only_verification_changes(
                before, after, report_path=report_path
            )
            summary["report_only_verification_changes"] = list(verification_changes)
            if after.source_changes:
                # A checkout change is reported through the same channel and
                # the same exit code whether a worker or --verify made it, so
                # extend the recorded delta rather than hide it behind the
                # lane-level refusal below.
                merged = list(source_changes or ())
                merged.extend(
                    path for path in after.source_changes if path not in merged
                )
                source_changes = tuple(merged)
                summary["source_changes"] = list(source_changes)
                summary["source_mutated"] = True
    else:
        # None, not False: nobody rendered a verdict, and the summary must
        # not imply one was reached and lost.
        summary["verified"] = None
    # The report-only verdict is reached here, after verification, so the
    # machine-readable `delivered` a downstream consumer (a model
    # qualification, for one) reads can never claim a lane this run has
    # already failed: a non-zero worker exit, an unverifiable or changed
    # coordinator checkout, a failed verification, and a verification that
    # dirtied the lane are all part of the same verdict as the report itself.
    # Exit codes alone would not be enough — a consumer that reads the summary
    # never sees them. An execute lane's `delivered` stays exactly what it
    # was: the lane tree's own commit-plus-clean-tree answer.
    if report_deliverable:
        delivered = (
            report_only_blocker is None
            and not result.returncode
            and source_check_error is None
            and not source_changes
            and (verify is None or verify.passed)
            and not verification_changes
        )
    elif existing_workspace_mode:
        # What the worker changed, not whether a tree it never owned is clean —
        # and only that. Leaving the work uncommitted in the owner's workspace
        # is an ordinary outcome here, not a failure, because committing
        # somebody else's tree is not this run's decision to make. But the
        # git writes that would make it this run's decision are not a delivery
        # either, however much they changed: a restaged path, a HEAD moved past
        # a commit no one asked for, and a branch this run did not find the
        # workspace on each end the run as `not delivered`, which is what
        # `workspace_git_state_mutated` carries. Counting deltas cannot stand
        # in for that check: a commit makes every committed path a delta too.
        #
        # None, not False, when a path could not be measured at all — unreadable,
        # or a kind this walk never opens — and again when the second reading of
        # the workspace could not be taken: in both cases no comparison was made,
        # so neither "changed" nor "unchanged" is a verdict this run can render,
        # and the guards below refuse to claim a delivery on top of it.
        if workspace_after_error is not None:
            delivered = None
            summary["delivery_unverified"] = (
                "the existing workspace could not be read again after the "
                "worker ran, so nothing about what it holds now was measured "
                f"and no delta was computed: {workspace_after_error}"
            )
        elif workspace_unmeasured:
            delivered = None
            summary["delivery_unverified"] = (
                f"the existing workspace holds {len(workspace_unmeasured)} "
                "path(s) this run could not measure, so nothing about them was "
                "compared: " + ", ".join(workspace_unmeasured[:20])
                + (
                    ""
                    if len(workspace_unmeasured) <= 20
                    else f", and {len(workspace_unmeasured) - 20} more"
                )
            )
        else:
            delivered = (
                bool(workspace_delta_records) and not workspace_git_state_mutated
            )
    else:
        delivered = delivery.delivered
    summary["delivered"] = delivered
    publish_warning = None
    # What the runner itself did about publication, tracked separately from the
    # `published` field it writes: that field has never meant more than "this
    # runner pushed, did not push, or failed to push", and the record below is
    # what stops it being read as a statement about the worker.
    runner_publication = "not-delivered"
    runner_skip_reason: str | None = None
    if summary["delivered"] and result.returncode:
        # A provider that failed can still leave a committed, clean branch: an
        # execute lane's `delivered` is the lane tree's own answer and does not
        # depend on the worker's exit code. A failed run's branch is not published
        # automatically, but neither is it undelivered — saying
        # "not-delivered" here would contradict the delivery record beside it
        # and hide why this runner skipped its own push.
        runner_publication = "skipped"
        runner_skip_reason = "provider-failed"
    elif not result.returncode and summary["delivered"]:
        # A delivered lane's commits live on one machine until they are pushed:
        # measured 2026-09-17, 40 commits across 36 worktrees were on no remote,
        # and nothing ever reclaimed the trees. Pushing makes the commits
        # remote-contained, which is what lets worktree_doctor's PRUNE path
        # reclaim the worktree later with its guards intact. A failed push is
        # reported, never fatal: the work is committed with or without the
        # remote, and failing here would be worse than today's behavior.
        if report_deliverable:
            # Never published, with or without --no-publish: a report lane's
            # deliverable is the report artifact in its worktree, and an
            # accepted one has no commit on its branch to make remote-contained
            # (one that did commit is rejected above, so this cannot hide an
            # unpublished commit). Recorded explicitly, as --no-publish does,
            # so "not pushed" is never confused with "publishing was skipped".
            summary["published"] = None
            runner_publication = "skipped"
            runner_skip_reason = report_flag
        elif existing_workspace_mode:
            # Never published, and not for a reason the operator chose: the
            # branch here is the owner's own, on a workspace that stood on it
            # before this run existed. Pushing it would send the owner's
            # commits — and everyone else's on that branch — to a remote as a
            # side effect of a worker run. publish_lane_branch refuses this
            # itself; recording it here as well keeps "not pushed" from
            # reading as "publishing was skipped".
            summary["published"] = None
            runner_publication = "skipped"
            runner_skip_reason = "--existing-workspace"
        elif getattr(args, "no_publish", False):
            summary["published"] = None
            runner_publication = "skipped"
            runner_skip_reason = "--no-publish"
        elif no_external_publication:
            # The task guard refuses external publication outright, and the
            # runner's own push of a delivered branch is exactly one of the
            # publications it refuses. Honoring that must not depend on the
            # caller also remembering `--no-publish`: the guard alone suppresses
            # the runner's push, and records its own reason so the audit still
            # distinguishes this skip from the runner-only option's. `--no-publish`
            # keeps its legacy meaning untouched — checked first above, so a run
            # carrying both records the option it was given, as before.
            summary["published"] = None
            runner_publication = "skipped"
            runner_skip_reason = "--no-external-publication"
        else:
            try:
                summary["published_ref"] = publish_lane_branch(lane)
                summary["published"] = True
                runner_publication = "published"
            except WorktreeError as exc:
                summary["published"] = False
                summary["publish_error"] = str(exc)
                runner_publication = "failed"
                publish_warning = (
                    f"side-lane: lane delivered but could not publish the branch: {exc}"
                )
    # One record, beside the outcome, that answers the publication question
    # honestly: what authority this run carried, what the host's own controls
    # could do with it, what this runner itself did — and, explicitly, that the
    # worker's own publication was not verified. This is a record of the
    # runner: nothing in this run observes the worker's own publication, so a
    # `skipped` or `failed` here says what this runner did and is never
    # evidence that nothing was published.
    summary["publication"] = _publication_record(
        publication_authority, runner_publication, runner_skip_reason
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if publish_warning:
        print(publish_warning, file=sys.stderr)
    if verify is not None and not verify.passed:
        # The command and its output tail are the whole evidence; without
        # them the operator is back to trusting prose.
        print(
            f"side-lane: verification failed (exit {verify.exit_code}) "
            f"for command: {verify.command}\n{verify.output}",
            file=sys.stderr,
        )
    if verification_changes:
        # Not a warning: `delivered` is false because of it, and the operator
        # has to know the difference between "the worker left this" and "the
        # verification command left this" before touching the lane.
        print(
            "side-lane: --verify changed what this report-only run had judged: "
            + "; ".join(verification_changes)
            + ". Nothing was removed, but this run judged the lane as it stood "
            "before the command ran, so the lane is not a delivery.",
            file=sys.stderr,
        )
    if result.returncode:
        return result.returncode
    if report_deliverable and report_only_blocker is not None:
        # One feedback round was already spent inside the invocation; the lane
        # is still not an accepted report-only delivery — no report this run
        # wrote, a commit it was told not to make, or changes that are neither
        # the report nor its scratch tree — and the exit code says so rather
        # than leaving an operator to read the prose. Nothing was removed: the
        # lane's worktree, any commit, and the audit are all intact. The outer
        # GCF consumer's own report collection stays authoritative for source
        # changes, containment, sizes, and scrubbing.
        print(f"side-lane: {report_only_blocker}", file=sys.stderr)
        return LANE_NOT_DELIVERED
    if verify is not None and not verify.passed:
        return LANE_VERIFY_FAILED
    if source_check_error is not None:
        # Fail closed like an unverifiable lane tree: "we could not look at
        # the coordinator checkout" must not read as a clean run.
        print(
            "side-lane: could not verify the coordinator checkout stayed "
            f"unchanged, so the run is not claimed clean: {source_check_error}",
            file=sys.stderr,
        )
        return LANE_DELIVERY_UNVERIFIED
    if source_changes:
        # Report the changed paths, not invented blame: a worker writing
        # outside its lane and a concurrent human edit are indistinguishable
        # here, and either way the run is not a clean accepted delivery. The
        # lane's own work — commits, worktree, audit — is left untouched.
        listed = "\n  ".join(source_changes[:20])
        more = (
            ""
            if len(source_changes) <= 20
            else f"\n  ... and {len(source_changes) - 20} more"
        )
        print(
            "side-lane: the coordinator checkout changed during the run "
            "(detected changes; attribution unknown):\n"
            f"  {listed}{more}\n"
            "Nothing was removed. Inspect the paths above before accepting "
            "the lane's delivery.",
            file=sys.stderr,
        )
        return LANE_SOURCE_MUTATED
    if verification_changes:
        # The message above already names what the command changed. Last of the
        # failure guards on purpose: a verification that dirtied the
        # coordinator checkout is a source mutation (exit 6, above), and one
        # that failed is an exit 5 — this is the remaining case, where the
        # command exited 0 and the lane is still not the state that was judged.
        return LANE_NOT_DELIVERED
    if workspace_after_error is not None:
        # Fail closed, before every verdict below — including the
        # `--allow-no-commit` exit 0 — because a delivery is only as good as the
        # comparison under it, and here there is no comparison: the workspace
        # was not read again. Not a "changed" delta either: the run says what it
        # could not look at, not what it thinks happened.
        print(
            "side-lane: the existing workspace could not be read again after "
            "the worker ran, so this run cannot say what changed in it and "
            f"claims no delivery (nothing about it was measured): "
            f"{workspace_after_error}\n"
            "Nothing was removed, and the workspace is exactly as the worker "
            "left it. Read it by hand, or make it readable and re-run, before "
            "accepting anything from this lane.",
            file=sys.stderr,
        )
        return LANE_DELIVERY_UNVERIFIED
    if workspace_unmeasured:
        # Same rule for a path that could not be measured on either side,
        # whichever of the two ways it could not be: an unreadable path and a
        # path of a kind this walk never opens are equally unmeasured, and a
        # run that read two sentinels as equal would be claiming a comparison
        # it never made.
        listed = "\n  ".join(workspace_unmeasured[:20])
        more = (
            ""
            if len(workspace_unmeasured) <= 20
            else f"\n  ... and {len(workspace_unmeasured) - 20} more"
        )
        print(
            "side-lane: the existing workspace holds paths this run could not "
            "measure — unreadable, or of a kind it never opens, such as a dirty "
            "submodule or an untracked nested repository — so it cannot compare "
            "them and does not claim a delivery (nothing about them was "
            "measured):\n"
            f"  {listed}{more}\n"
            "Nothing was removed; those paths are exactly as they were found. "
            "Inspect them by hand — a change inside one is invisible to this "
            "run, which is why it is reported as unmeasured rather than as "
            "unchanged — before accepting anything from this lane.",
            file=sys.stderr,
        )
        return LANE_DELIVERY_UNVERIFIED
    if delivered:
        return 0
    # --allow-no-commit covers a lane whose intended outcome is no commit. It
    # does NOT excuse a lane that committed and then abandoned the rest: that
    # is partial delivery, and the abandoned half is lost either way. It is
    # also what a report-only lane no longer needs: that outcome is judged on
    # the report, not on a commit the worker was told not to make.
    if existing_workspace_mode:
        # A git write this mode forbids is refused before anything else, and
        # `--allow-no-commit` does not excuse it: that flag covers a lane whose
        # intended outcome is no commit, not a lane that restaged, recommitted
        # or rebranched the owner's checkout. It is checked ahead of the flag
        # because the flag's own condition — no deltas at all — is satisfied by
        # a branch switch, which moves no path in this measurement, so the
        # escape would otherwise reach the one outcome it must not cover.
        if workspace_git_state_mutated:
            audit = workspace_audit or {}
            moved: list[str] = []
            if workspace_head_moved:
                moved.append("HEAD moved past a commit this run never asked for")
            if workspace_branch_moved:
                moved.append(
                    "the workspace is on a branch this run did not find it on"
                )
            if workspace_git_state_paths:
                named = ", ".join(workspace_git_state_paths[:20])
                if len(workspace_git_state_paths) > 20:
                    named += f", and {len(workspace_git_state_paths) - 20} more"
                moved.append(
                    f"{len(workspace_git_state_paths)} path(s) were restaged or "
                    f"had their index flags written in the owner's index: {named}"
                )
            # The parts of git's state that are not paths at all, named by
            # component and then by object: a ref moved, the local config
            # rewritten, a worktree registered. These leave every path in the
            # workspace byte-for-byte identical, which is why the deny list
            # alone could not have stopped them and why the refusal has to say
            # what it found rather than only that the guard fired.
            for component in workspace_git_state_components:
                phrase = _git_state_phrase(component, audit)
                if phrase is not None:
                    moved.append(phrase)
            print(
                "side-lane: the worker made a git write in the existing "
                f"workspace {lane.worktree} (branch {lane.branch}), which this "
                "mode forbids: " + "; ".join(moved) + ". This run does not "
                "claim a delivery from it. Nothing was removed and nothing was "
                "undone — no path was reset, restored or cleaned — so the "
                "workspace is exactly as the worker left it, and undoing the "
                "write is the owner's decision, not this runner's.",
                file=sys.stderr,
            )
            return LANE_NOT_DELIVERED
        # Judged on the deltas, so the lane-tree failure reason would name the
        # wrong thing — a commit this mode never required, over preexisting
        # work that was not the worker's. `--allow-no-commit` is already
        # satisfied the moment there is a delta (an existing-workspace lane
        # never commits on the worker's behalf), so the remaining case is the
        # plain one: the worker changed nothing.
        if getattr(args, "allow_no_commit", False) and not workspace_delta_records:
            return 0
        print(
            "side-lane: the worker left no change in the existing workspace "
            f"{lane.worktree} (branch {lane.branch}): no path it already held "
            "changed — content, mode, index or its index flags, or status — "
            "none was added or removed, HEAD did not move, the workspace "
            "stayed on the branch this run found it on, and its refs, local "
            "config and worktree registrations are as this run found them. "
            "Nothing was removed; the workspace is exactly as the worker left "
            "it.",
            file=sys.stderr,
        )
        return LANE_NOT_DELIVERED
    if getattr(args, "allow_no_commit", False) and not delivery.uncommitted:
        return 0
    print(f"side-lane: {delivery.failure_reason()}", file=sys.stderr)
    return LANE_NOT_DELIVERED


def run(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    config = load_config()
    if args.command == "list":
        for provider, item in config["providers"].items():
            for mode, hosts in item["routes"].items():
                for host, route in hosts.items():
                    for model in route["models"]:
                        effective, _ = select_route(config, host, mode, provider, model)
                        print(
                            f"{host}\t{mode}\t{provider}\t{item['gateway']}\t{model}\t{item['auth_method']}\t{'billable' if effective['billable'] else 'subscription'}"
                        )
        return 0
    if args.command == "candidates":
        candidates = routing.list_catalog_candidates(routing.load_catalog())
        if args.json:
            print(json.dumps(candidates, indent=2, sort_keys=True))
        else:
            for candidate in candidates:
                print(
                    "\t".join(
                        (
                            candidate["id"],
                            candidate["provider"],
                            candidate["requested_model"],
                            candidate["execution_location"],
                            candidate["qualification_state"],
                            "disabled",
                        )
                    )
                )
        return 0
    if args.command == "credentials":
        states = {
            provider: (
                "not-used-oauth"
                if item["auth_method"] == "oauth"
                else (
                    "present"
                    if credential_present(item["credential_service"])
                    else "absent"
                )
            )
            for provider, item in config["providers"].items()
        }
        print(
            json.dumps(states, sort_keys=True)
            if args.json
            else "\n".join(f"{key}\t{value}" for key, value in states.items())
        )
        return 0
    if args.command == "auth-status":
        status = auth_status(args.host, executable=_host_executable(args.host))
        payload = status.as_dict()
        print(
            json.dumps(payload, sort_keys=True)
            if args.json
            else "\n".join(f"{key}\t{value}" for key, value in payload.items())
        )
        return 0 if status.ready else 1
    if args.command == "check-capabilities":
        repo = validate_governance(args.repo) if args.repo else None
        report = _capability_report(
            config, args.host, args.mode, args.provider, args.model, repo
        )
        print(
            json.dumps(report, sort_keys=True)
            if args.json
            else "\n".join(f"{key}\t{value}" for key, value in report.items())
        )
        return 0
    if args.command == "recommend":
        return _recommend(args, config)
    if args.command == "evaluate":
        return _evaluate(args.input)
    repo = validate_governance(args.repo)
    # Validated before any worktree, credential or host executable is touched,
    # so an unsafe or unusable read root fails the run before it can leave a
    # lane behind. `--read-root` is declared by the `run` subparser, so it is
    # always present here.
    read_roots = parse_read_roots(args.read_root)
    # Same fail-closed ordering for the documentation-domain grants: every
    # hostname is validated here, so a malformed, wildcard, IP, private or
    # reserved domain stops the run before a worktree, a credential or a host
    # process exists.
    web_domains = parse_web_domains(args.web_domain)
    if web_domains and args.mode != "execute":
        # Review mode is the strict read-only form and has no fetch tool on any
        # host, so a documentation-domain grant there is authority the worker
        # cannot be given. Refused here rather than in the adapters so the run
        # stops before the prompt gate, a worktree, or any host process; each
        # adapter carries its own guard for direct callers.
        raise SideLaneError("--web-domain is supported only in execute mode")
    if web_domains and args.host == "codex":
        # The one host that cannot express the grant. An execute Codex lane
        # runs `danger-full-access`, so it already reaches every destination
        # and exposes no per-destination rule to narrow it with; accepting the
        # flag would describe, and audit, a scope nothing enforces. The Codex
        # adapter carries the same refusal as the authoritative guard for
        # direct callers. Refused here, before any worktree, credential or
        # host process exists.
        raise SideLaneError(
            "--web-domain is not supported on the Codex host: an execute Codex "
            "lane runs with danger-full-access and Codex CLI has no "
            "per-destination web-fetch permission rule, so the grant could be "
            "neither enforced nor honestly recorded"
        )
    # Same fail-closed ordering for the per-run MCP registration file: its
    # structure, capability narrowing and env references are all checked here
    # (side_lane.mcp_run_config) before anything is created or started.
    run_mcp_servers = load_run_mcp_config(args.mcp_config) if args.mcp_config else None
    # And the same for the optional measurement file: its size, field set and
    # vocabulary are all checked here, so a malformed or over-large record
    # stops the run before a worktree, a credential, or a host process exists.
    measurement = (
        load_measurement(args.measurement_file) if args.measurement_file else None
    )
    return _launch(
        args, config, repo, load_prompt(args.prompt, args.prompt_file, args.mode),
        read_roots=read_roots,
        web_domains=web_domains,
        run_mcp_servers=run_mcp_servers,
        measurement=measurement,
    )


def main() -> None:
    try:
        raise SystemExit(run())
    except (
        SideLaneError,
        AuthError,
        CredentialError,
        GovernanceError,
        ReadRootError,
        WebDomainError,
        SkillBundleError,
        McpRunConfigError,
        WorktreeError,
        ClaudeAdapterError,
        CodexAdapterError,
        DevinAdapterError,
        evaluation.EvaluationError,
        routing.RoutingError,
    ) as exc:
        print(f"side-lane: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
