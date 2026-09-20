from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping, Sequence

from side_lane import evaluation, report_stop_hook, routing, selector_policy
from side_lane.auth import AuthError, auth_status, require_native_oauth
from side_lane.credentials import CredentialError, credential_present, read_credential
from side_lane.connector_metadata import json_mcp_name_scopes, toml_mcp_names
from side_lane.governance import GovernanceError, validate_repository
from side_lane.hosts import (
    HostExecutableError,
    host_support_dir,
    require_host_executable,
    resolve_host_executable,
)
from side_lane.mcp_run_config import (
    CAPABILITY_MCP_SERVERS as RUN_MCP_CAPABILITY_SERVERS,
    McpRunConfigError,
    audit_names,
    load_run_mcp_config,
    registration_paths,
    require_env_references,
    validate_against_capabilities,
)
from side_lane.read_roots import ReadRootError, parse_read_roots
from side_lane.skill_bundle import SkillBundleError, catalog_note, deliver_skills
from side_lane.adapters.claude import ClaudeAdapterError, require_report_only_budget
from side_lane.adapters.codex import CodexAdapterError
from side_lane.adapters.devin import DevinAdapterError
from side_lane.worktrees import (
    ASSIGNMENT_SCHEMA_VERSION,
    AssignmentRecord,
    WorktreeError,
    create_worktree,
    dispose_clean_worktree,
    git_status,
    lane_delivery,
    publish_lane_branch,
    snapshot_source,
    source_mutations,
    verify_lane,
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
        "--mcp-config",
        metavar="PATH",
        default=None,
        help="deliver one coordinator-supplied per-run MCP server registration "
        "file (a JSON object with exactly key mcpServers, remote streamable-HTTP "
        "entries only, credentials referenced by env name, never as values). "
        "Every declared server name must map from a granted --capability "
        "(aws-read registers the server named aws), and each referenced env "
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
        "symlink when the worker exits. Requires a finite positive "
        "max_budget_usd on the route so the USD cap and the hook travel in one "
        "command. Ordinary execute and review lanes are unchanged",
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
        "remote-contained instead of stranded on the launching machine",
    )
    run.add_argument(
        "--verify",
        metavar="CMD",
        help="after an execute lane delivers, run this shell command in the lane "
        "worktree (e.g. its test suite); a non-zero exit fails the run with exit "
        "code 5 even though the lane delivered. Verification does not suppress "
        "the normal publication attempt, which --no-publish and a failed push "
        "can still leave without a remote branch",
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


def _recommend(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    repo = validate_governance(args.repo)
    profile = load_recommendation_profile(args.profile)
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
    snapshot_args = getattr(args, "routed_policy_snapshot", None)
    if isinstance(snapshot_args, str):
        snapshot_args = [snapshot_args]
    if not isinstance(snapshot_args, (list, tuple)):
        snapshot_args = []
    policy_snapshots = [
        load_routed_policy_snapshot(snapshot_path)
        for snapshot_path in snapshot_args
    ]
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
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _recommendation_host_snapshot(
    report: Mapping[str, Any], mode: str
) -> dict[str, Any]:
    """Translate presence evidence narrowly for offline route staffing.

    A configured Playwright server is enough for an execute recommendation
    because dispatch performs a live readiness check and the route catalog
    still requires its own local evaluation and connector evidence. Other
    merely-present capabilities remain unavailable because their authority or
    authentication has not been verified. Review lanes expose no MCP servers.
    """

    connectors = report.get("mcp_connectors", [])
    capabilities = report.get("capabilities", {})
    evidence = report.get("capability_evidence", {})
    if not isinstance(connectors, list) or not isinstance(capabilities, Mapping):
        raise SideLaneError("capability report is malformed")
    available = {name for name, state in capabilities.items() if state}
    if mode != "execute":
        available.difference_update({"playwright", "gitnexus", "codegraph"})
    if (
        mode == "execute"
        and "playwright" in connectors
        and isinstance(evidence, Mapping)
        and isinstance(evidence.get("playwright"), Mapping)
        and evidence["playwright"].get("state") == "present"
    ):
        available.add("playwright")
    return {
        "available_connectors": sorted(connectors) if mode == "execute" else [],
        "available_capabilities": sorted(available),
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

    ``asana-read`` and ``drive-read`` both grant exact
    ``mcp__cm-services__<tool>`` IDs, so the tool IDs embed the server name
    ``cm-services`` exactly on every host — like ``slack-read`` the check
    demands the exact name (a near-miss is ``name-mismatch``). The server is
    a fixed local stdio registration the coordinator provisions into the
    worker host's user-global config under the same account the capability
    reads; a ``present`` registration is presence evidence only — same-account
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
    on every host.
    """

    if host == "codex" and not require_exact:
        if any(capability in name.lower() for name in mcp_names):
            return {
                "state": "present",
                "basis": "connector-name metadata only; Codex lanes inherit configured MCP servers directly",
            }
        return {
            "state": "unknown",
            "basis": "connector-name metadata only; see mcp_registration_sources for which registration files were consulted and what each contributed",
        }
    if capability in mcp_names:
        return {
            "state": "present",
            "basis": f"connector registered under the exact name {capability!r}; tool access not tested",
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
    """

    sources: list[dict[str, str]] = []
    for scope, path in _mcp_registration_paths(host, repo):
        source = {"scope": scope, "path": str(path)}
        if not path.is_file():
            sources.append({**source, "state": "missing"})
            continue
        try:
            if path.suffix == ".toml":
                # Flat TOML tables carry no scope of their own.
                in_scope, declared = toml_mcp_names(path), set()
            else:
                scopes = json_mcp_name_scopes(path)
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
    Its user config also carries per-project entries (``projects.<path>.
    mcpServers``) keyed by the directory Claude was started in; a lane runs in a
    fresh worktree, so no such entry applies to it. Those names are reported
    separately as out of scope instead of being unioned into the inventory.
    Codex reads flat TOML tables and has no per-project layer in its user config.
    Devin reads its own three registration files, which ``devin mcp add --help``
    documents as user, project and local scope.
    """

    names: set[str] = set()
    out_of_scope: set[str] = set()
    for _scope, path in _mcp_registration_paths(host, repo):
        if not path.is_file():
            continue
        try:
            if path.suffix == ".toml":
                names.update(toml_mcp_names(path))
                continue
            for name, scopes in json_mcp_name_scopes(path).items():
                if () in scopes:
                    names.add(name)
                else:
                    out_of_scope.add(name)
        except (OSError, ValueError):
            pass
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


def _launch(
    args: argparse.Namespace, config: Mapping[str, Any], repo: Path, prompt: str,
    *, read_roots: Sequence[Path] = (),
    run_mcp_servers: "Mapping[str, Any] | None" = None,
    measurement: "Mapping[str, Any] | None" = None,
) -> int:
    provider_config, model_config = select_route(
        config, args.host, args.mode, args.provider, args.model
    )
    # The report-only opt-in is the same-invocation repair for a worker that
    # ended its turn with exit 0 and reported a report it never wrote. It is
    # deliberately narrow: execute mode only, the Claude host only (the
    # mechanism is a Claude Code Stop hook), and gated on an explicit spend cap
    # so the cap and the hook are part of one command. Everything else about an
    # execute lane — its argv, tools, permissions, MCP handling, timeout — is
    # untouched by this flag, and review lanes never see it.
    # Exact-boolean read: an argparse Namespace always carries the declared
    # flag, and anything other than an explicit True means the opt-in was not
    # given, so no lane can be steered into report-only mode by accident.
    report_only = getattr(args, "report_only", False) is True
    report_path: Path | None = None
    if report_only:
        if args.mode != "execute":
            raise SideLaneError("--report-only is supported only in execute mode")
        if args.host != "claude":
            raise SideLaneError(
                "--report-only is supported only on the claude host: the repair "
                "is a Claude Code Stop hook inside the same invocation"
            )
        try:
            require_report_only_budget(model_config)
        except ClaudeAdapterError as exc:
            raise SideLaneError(str(exc)) from exc
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
        # Launch needs evidence that the capability exists on this host
        # ("verified" or "present"); "unknown"/"unavailable" fail closed. The
        # stricter boolean `capabilities` map (verified only) is what
        # `recommend` uses to rank routes, not what gates a launch.
        evidence = _capability_report(
            config, args.host, args.mode, args.provider, args.model, repo
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
    lane = create_worktree(
        repo, args.lane_name, worktree_root=getattr(args, "worktree_root", None)
    )
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
    try:
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
                run_mcp_servers=run_mcp_servers,
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
                run_mcp_servers=run_mcp_servers,
                report_only=report_only,
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
                run_mcp_servers=run_mcp_servers,
            )
    except Exception:
        dispose_clean_worktree(lane)
        raise
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    summary = result.as_dict()
    status = git_status(lane)
    # Compare the coordinator checkout against its pre-dispatch baseline
    # BEFORE writing the audit so the audit itself records what changed. A
    # failed comparison is not silently clean: the run reports the check as
    # unverified and refuses to claim a clean delivery below.
    source_changes: tuple[str, ...] | None = ()
    source_check_error: str | None = None
    if args.mode == "execute":
        try:
            source_changes = source_mutations(repo, source_baseline)
        except WorktreeError as exc:
            source_changes = None
            source_check_error = str(exc)
    # Preserve the provider's outcome separately from this runner's check.
    # Machine readers must see the same source-check failure as the exit code.
    source_exit_status = result.returncode or (
        LANE_DELIVERY_UNVERIFIED if source_check_error is not None
        else LANE_SOURCE_MUTATED if source_changes else 0
    )
    summary["provider_exit_status"] = result.returncode
    summary["exit_status"] = source_exit_status
    # The terminal audit links the assignment on every outcome that reaches it:
    # a run that failed, or verified nothing, still points at what it was
    # assigned. `None` is the explicit unmeasured case — no measurement was
    # requested for this run. It never means "measurement was lost"; a sidecar
    # that could not be published aborted the run before an adapter started,
    # and nothing downstream could accept that worker's result either.
    assignment_link = (
        None
        if assignment is None
        else {
            "path": str(assignment.path),
            "sha256": assignment.sha256,
            "task_id": measurement["task_id"] if measurement else None,
            "schema_version": ASSIGNMENT_SCHEMA_VERSION,
            "reused": assignment.reused,
        }
    )
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
        assignment=assignment_link,
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
            "run_mcp_servers": list(audit_names(run_mcp_servers)) if run_mcp_servers else [],
            # None means the comparison itself failed — never report that as
            # a clean checkout, same contract as delivered/verified above.
            "source_mutated": None if source_changes is None else bool(source_changes),
            "source_changes": list(source_changes or ()),
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
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(
            f"side-lane: could not verify lane delivery, so it is not claimed: {exc}",
            file=sys.stderr,
        )
        return result.returncode or LANE_DELIVERY_UNVERIFIED

    summary["committed"] = delivery.committed
    summary["uncommitted"] = list(delivery.uncommitted)
    # The runner's own look at the report, on the same rule the in-loop Stop
    # hook applies. A report-only lane is not delivered until this acceptance
    # precondition passes; this decision must precede both publication and the
    # summary so a committed branch cannot be mistaken for an accepted lane.
    report_present: bool | None = None
    if report_only:
        report_path = lane.worktree / report_stop_hook.REPORT_NAME
        report_present = report_stop_hook.report_is_valid(report_path)
        summary["report_present"] = report_present
        summary["report_path"] = str(report_path)
    summary["delivered"] = delivery.delivered and (
        not report_only or report_present is True
    )
    # 2026-09-17: a lane told to run the test suite ran it 18 times, saw
    # JSONDecodeError nine times, committed the failing tests anyway, and
    # exited 0 — caught only because a human re-ran the suite by hand. A
    # lane's claim about tests is prose in a transcript; the runner must
    # check. Verification runs only for a delivered lane (an undelivered one
    # already fails), and it runs BEFORE publication on purpose: publication
    # must still happen either way, because preserving the branch is what
    # stops work being stranded on one machine, and failing work is exactly
    # the work someone needs to be able to look at.
    verify = None
    if getattr(args, "verify", None) and delivery.delivered:
        verify = verify_lane(lane, args.verify)
        summary["verified"] = verify.passed
        summary["verify_command"] = verify.command
        summary["verify_exit"] = verify.exit_code
        summary["verify_output"] = verify.output
    else:
        # None, not False: nobody rendered a verdict, and the summary must
        # not imply one was reached and lost.
        summary["verified"] = None
    publish_warning = None
    if not result.returncode and summary["delivered"]:
        # A delivered lane's commits live on one machine until they are pushed:
        # measured 2026-09-17, 40 commits across 36 worktrees were on no remote,
        # and nothing ever reclaimed the trees. Pushing makes the commits
        # remote-contained, which is what lets worktree_doctor's PRUNE path
        # reclaim the worktree later with its guards intact. A failed push is
        # reported, never fatal: the work is committed with or without the
        # remote, and failing here would be worse than today's behavior.
        if getattr(args, "no_publish", False):
            summary["published"] = None
        else:
            try:
                summary["published_ref"] = publish_lane_branch(lane)
                summary["published"] = True
            except WorktreeError as exc:
                summary["published"] = False
                summary["publish_error"] = str(exc)
                publish_warning = (
                    f"side-lane: lane delivered but could not publish the branch: {exc}"
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
    if result.returncode:
        return result.returncode
    if report_only and not report_present:
        # One feedback round was already spent inside the invocation; the
        # report is still not there. A lane that wrote its work but no report
        # is not an accepted delivery, and the exit code says so rather than
        # leaving an operator to read the prose. Nothing was removed: the
        # lane's commit, worktree, and audit are all intact. The outer GCF
        # consumer's own report collection stays authoritative for source
        # changes, containment, sizes, and scrubbing.
        print(
            f"side-lane: --report-only lane produced no usable "
            f"{report_stop_hook.REPORT_NAME} at {report_path}; the worker's "
            "completion prose is not the report. The lane's commit and audit "
            "are intact.",
            file=sys.stderr,
        )
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
    if delivery.delivered:
        return 0
    # --allow-no-commit covers a lane whose intended outcome is no commit. It
    # does NOT excuse a lane that committed and then abandoned the rest: that
    # is partial delivery, and the abandoned half is lost either way.
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
