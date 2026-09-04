from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping, Sequence

from side_lane import evaluation, routing
from side_lane.auth import AuthError, auth_status, require_native_oauth
from side_lane.credentials import CredentialError, credential_present, read_credential
from side_lane.connector_metadata import json_mcp_names, toml_mcp_names
from side_lane.governance import GovernanceError, validate_repository
from side_lane.hosts import HostExecutableError, require_host_executable, resolve_host_executable
from side_lane.adapters.claude import ClaudeAdapterError
from side_lane.adapters.codex import CodexAdapterError
from side_lane.worktrees import (
    WorktreeError,
    create_worktree,
    dispose_clean_worktree,
    git_status,
    write_audit,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "models.json"
MAX_PROMPT_CHARS = 100_000
MAX_PROFILE_CHARS = 100_000
REVIEW_UNSAFE = tuple(re.compile(p, re.I) for p in (
    r"\b(?:edit|modify|write|delete|create)\s+(?:the\s+|a\s+)?(?:files?|code|repo)",
    r"(?:^|[.!?]\s+)(?:please\s+)?(?:fix|implement|refactor|update|add|remove|rename|replace|change)\b",
    r"\b(?:fix|implement|refactor|update|add|remove|rename|replace|change)\s+(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+\b",
    r"\b(?:apply|produce)\s+(?:a\s+)?(?:patch|diff)", r"\b(?:commit|push|merge|deploy)\b",
    r"\b(?:bypass|disable|skip)\s+(?:permissions?|sandbox|guardrails?)\b",
))
EXECUTE_UNSAFE = tuple(re.compile(p, re.I) for p in (
    r"--(?:dangerously-skip-permissions|allow-dangerously-skip-permissions|ignore-user-config)",
    r"\b(?:force[- ]?push|deploy|merge\s+(?:the\s+)?(?:pr|branch))\b",
    r"\b(?:insert|update|delete|drop|alter|truncate|create)\s+(?:into\s+|table\s+|database\s+)",
    r"\b(?:change|grant|revoke|rotate|delete)\s+(?:iam|credentials?|secrets?|cloud resources?)\b",
))


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
    if config.get("schema_version") != 3 or not isinstance(config.get("providers"), dict):
        raise SideLaneError("model allowlist requires schema_version 3")
    if not isinstance(config.get("capabilities"), list):
        raise SideLaneError("capability allowlist is invalid")
    for provider, item in config["providers"].items():
        if not isinstance(item, dict) or item.get("auth_method") not in {"oauth", "provider-key"} or not item.get("gateway"):
            raise SideLaneError(f"provider {provider!r} has invalid auth/gateway metadata")
        if item["auth_method"] == "oauth" and item.get("billable") is not False:
            raise SideLaneError(f"native provider {provider!r} must be non-billable OAuth")
        if item["auth_method"] == "provider-key" and (
            item.get("billable") is not True or not item.get("credential_service") or item.get("explicit_only") is not True
        ):
            raise SideLaneError(f"key provider {provider!r} must be billable and explicit-only")
        for mode, hosts in item.get("routes", {}).items():
            if mode not in {"review", "execute"} or not isinstance(hosts, dict):
                raise SideLaneError(f"provider {provider!r} has an invalid route")
            for host, route in hosts.items():
                models = route.get("models") if isinstance(route, dict) else None
                if host not in {"codex", "claude"} or not route.get("protocol") or not isinstance(models, list) or not models:
                    raise SideLaneError(f"provider {provider!r} has an invalid host route")
                if len(set(models)) != len(models) or not all(isinstance(model, str) and model for model in models):
                    raise SideLaneError(f"provider {provider!r} has invalid models")
    return config


def select_route(config: Mapping[str, Any], host: str, mode: str, provider: str, model: str) -> tuple[Mapping[str, Any], dict[str, Any]]:
    provider_config = config["providers"].get(provider)
    if not isinstance(provider_config, Mapping):
        raise SideLaneError(f"unknown provider: {provider}")
    route = provider_config.get("routes", {}).get(mode, {}).get(host)
    if route is None:
        raise SideLaneError(f"unsupported route: {host}/{mode}/{provider}")
    if model not in route["models"]:
        raise SideLaneError(f"model {model!r} is not allowed for {host}/{mode}/{provider}")
    return provider_config, {
        "runtime_model": model, "protocol": str(route["protocol"]), "wire_api": str(route["protocol"]),
        "gateway": provider_config["gateway"], "auth_method": provider_config["auth_method"],
        "billable": provider_config["billable"],
    }


def validate_selection(config: Mapping[str, Any], provider: str, model: str, *, host: str = "claude", mode: str = "review") -> Mapping[str, Any]:
    return select_route(config, host, mode, provider, model)[1]


def validate_governance(repo_argument: str) -> Path:
    try:
        return validate_repository(repo_argument)
    except GovernanceError as exc:
        raise SideLaneError(str(exc)) from exc


def load_prompt(prompt: str | None, prompt_file: str | None, mode: str = "review") -> str:
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.is_file():
            raise SideLaneError(f"prompt file is not readable: {path}")
        value = path.read_text(encoding="utf-8")
    else:
        value = prompt or ""
    if not value.strip() or len(value) > MAX_PROMPT_CHARS:
        raise SideLaneError("prompt is empty or too long")
    if any(pattern.search(value) for pattern in (REVIEW_UNSAFE if mode == "review" else EXECUTE_UNSAFE)):
        raise SideLaneError(f"prompt requests an action prohibited in {mode} mode")
    return value


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="side-lane", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    credentials = sub.add_parser("credentials", allow_abbrev=False)
    credentials.add_argument("--json", action="store_true")
    auth = sub.add_parser("auth-status", allow_abbrev=False)
    auth.add_argument("--host", choices=("codex", "claude"), required=True)
    auth.add_argument("--json", action="store_true")
    check = sub.add_parser("check-capabilities", allow_abbrev=False)
    check.add_argument("--host", choices=("codex", "claude"), required=True)
    check.add_argument("--mode", choices=("review", "execute"), default="execute")
    check.add_argument("--provider")
    check.add_argument("--model")
    check.add_argument("--repo")
    check.add_argument("--json", action="store_true")
    recommend = sub.add_parser("recommend", allow_abbrev=False)
    recommend.add_argument("--repo", required=True)
    recommend.add_argument("--profile", required=True)
    evaluate = sub.add_parser("evaluate", allow_abbrev=False)
    evaluate.add_argument("--input", required=True)
    run = sub.add_parser("run", allow_abbrev=False)
    run.add_argument("--host", choices=("codex", "claude"), required=True)
    run.add_argument("--mode", choices=("review", "execute"), default="review")
    run.add_argument("--provider", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--repo", required=True)
    run.add_argument("--lane-name")
    run.add_argument("--capability", action="append", default=[])
    run.add_argument("--approve-billable-route", action="store_true")
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    return parser


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
    forbidden = re.compile(r"(?:api[_-]?key|credential|secret|quota|billing|usage)", re.I)
    pending: list[object] = [profile]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if forbidden.search(str(key)):
                    raise SideLaneError(f"recommendation profile contains prohibited field: {key}")
                pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    return profile


def _ready_routes(config: Mapping[str, Any]) -> frozenset[tuple[str, str, str, str]]:
    present: set[tuple[str, str, str, str]] = set()
    statuses = {host: auth_status(host, executable=_host_executable(host)) for host in ("codex", "claude")}
    for provider, item in config["providers"].items():
        if item.get("auth_method") == "oauth":
            ready_hosts = {host for host, status in statuses.items() if status.ready}
        else:
            ready_hosts = (
                {"codex", "claude"}
                if credential_present(item["credential_service"])
                else set()
            )
        for mode, hosts in item.get("routes", {}).items():
            for host, route in hosts.items():
                if host in ready_hosts:
                    present.update((provider, host, mode, model) for model in route.get("models", []))
    return frozenset(present)


def _recommend(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    repo = validate_governance(args.repo)
    profile = load_recommendation_profile(args.profile)
    host = profile.get("coordinator_host", profile.get("originating_host"))
    mode = profile.get("mode")
    if host not in {"codex", "claude"} or mode not in {"review", "execute"}:
        raise SideLaneError("recommendation profile requires coordinator_host and mode")
    required_connectors, required_capabilities = profile.get("required_connectors", []), profile.get("required_capabilities", [])
    for label, value in (("required_connectors", required_connectors), ("required_capabilities", required_capabilities)):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise SideLaneError(f"{label} must be a list of non-empty strings")
    unknown = sorted(set(required_capabilities) - set(config["capabilities"]))
    if unknown:
        raise SideLaneError(f"unknown capabilities: {', '.join(unknown)}")
    snapshots = {
        candidate_host: _capability_report(
            config, candidate_host, mode, None, None, repo
        )
        for candidate_host in ("codex", "claude")
    }
    normalized = dict(profile)
    normalized.update({"coordinator_host": host,
        "required_connectors": sorted(set(required_connectors)),
        "required_capabilities": sorted(set(required_capabilities)),
        "host_capabilities": {
            candidate_host: {
                "available_connectors": report["mcp_connectors"],
                "available_capabilities": sorted(
                    name for name, state in report["capabilities"].items() if state
                ),
            }
            for candidate_host, report in snapshots.items()
        }})
    result = routing.recommend(routing.load_catalog(), normalized,
        runtime_allowlist=routing.allowlist_from_models(config), credential_present_routes=_ready_routes(config))
    result.update({"presence_only": True, "originating_host_unchanged": True,
                   "required_capabilities": sorted(required_capabilities)})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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


def _capability_report(config: Mapping[str, Any], host: str, mode: str, provider: str | None, model: str | None, repo: Path | None = None) -> dict[str, Any]:
    runtime = _host_executable(host)
    mcp_names = _discover_mcp_names(host, repo)
    lowered = {name.lower() for name in mcp_names}
    evidence = {
        "workspace-write": {"state": "verified" if mode == "execute" else "unavailable", "basis": "active lane mode"},
        "shell": {"state": "verified" if runtime else "unavailable", "basis": "selected host executable"},
        "git-push": {"state": "present" if shutil.which("git") else "unavailable", "basis": "git executable; remote write authority not tested"},
        "gitnexus": {"state": "present" if any("gitnexus" in name for name in lowered) else "unknown", "basis": "connector-name metadata only"},
        "codegraph": {"state": "present" if any("codegraph" in name for name in lowered) else "unknown", "basis": "connector-name metadata only"},
        "gcloud-read": {"state": "present" if shutil.which("gcloud") else "unavailable", "basis": "gcloud executable; account/project access not tested"},
        "secret-use": {"state": "unknown", "basis": "credential values and access are never tested during preflight"},
        "database-read": {"state": "present" if shutil.which("psql") else "unavailable", "basis": "psql executable; database access not tested"},
        "workflow-write": {"state": "present" if any(marker in name for name in lowered for marker in ("asana", "slack", "teams", "github")) else "unknown", "basis": "connector-name metadata only; write authority not tested"},
    }
    report: dict[str, Any] = {"host": host, "mode": mode, "runtime": runtime, "route": "not-requested", "mcp_connectors": sorted(mcp_names)}
    if bool(provider) != bool(model):
        raise SideLaneError("provider and model must be supplied together")
    if provider and model:
        provider_config, route = select_route(config, host, mode, provider, model)
        report.update({"route": "configured", **{key: route[key] for key in ("gateway", "auth_method", "billable")}})
        if route["auth_method"] == "oauth":
            report["auth"] = auth_status(host, executable=runtime).as_dict()
        else:
            report["configured_override"] = "present" if credential_present(provider_config["credential_service"]) else "absent"
            report["requires_one_run_approval"] = True
    report["capability_evidence"] = {name: evidence.get(name, {"state": "unknown", "basis": "no evidence"}) for name in config["capabilities"]}
    report["capabilities"] = {
        name: report["capability_evidence"][name]["state"] == "verified"
        for name in config["capabilities"]
    }
    return report


def _discover_mcp_names(host: str, repo: Path | None = None) -> set[str]:
    names: set[str] = set()
    paths = ([Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"] if host == "codex"
             else [Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json"])
    if repo is not None:
        paths += [repo / ".mcp.json", repo / ".codex" / "config.toml"]
    for path in paths:
        if not path.is_file():
            continue
        try:
            names.update(toml_mcp_names(path) if path.suffix == ".toml" else json_mcp_names(path))
        except (OSError, ValueError):
            pass
    return names


def _launch(args: argparse.Namespace, config: Mapping[str, Any], repo: Path, prompt: str) -> int:
    provider_config, model_config = select_route(config, args.host, args.mode, args.provider, args.model)
    if args.capability and args.mode != "execute":
        raise SideLaneError("--capability is supported only in execute mode")
    unknown = sorted(set(args.capability) - set(config["capabilities"]))
    if unknown:
        raise SideLaneError(f"unknown capabilities: {', '.join(unknown)}")
    if args.capability:
        readiness = _capability_report(
            config, args.host, args.mode, args.provider, args.model, repo
        )["capabilities"]
        missing = [name for name in args.capability if not readiness.get(name)]
        if missing:
            raise SideLaneError(
                f"required capabilities unavailable: {', '.join(missing)}"
            )
    if not args.lane_name:
        raise SideLaneError(f"{args.mode} mode requires --lane-name")
    if provider_config["auth_method"] == "oauth" and args.approve_billable_route:
        raise SideLaneError("--approve-billable-route is invalid for native OAuth routes")
    if provider_config["auth_method"] == "provider-key" and not args.approve_billable_route:
        raise SideLaneError("billable route requires explicit --approve-billable-route for this run")
    executable = _require_host_executable(args.host)
    lane = create_worktree(repo, args.lane_name)
    secret: str | None = None
    try:
        if provider_config["auth_method"] == "oauth":
            require_native_oauth(args.host, executable=executable)
        else:
            secret = read_credential(provider_config["credential_service"])
        if args.host == "codex":
            from side_lane.adapters.codex import run_codex
            result = run_codex(executable=executable, repo=repo, worktree=lane.worktree, provider=args.provider,
                model=args.model, provider_config=provider_config, model_config=model_config, prompt=prompt, mode=args.mode)
        else:
            from side_lane.adapters.claude import launch
            result = launch(executable=executable, repo=repo, worktree=lane.worktree, provider=args.provider,
                model=args.model, provider_config=provider_config, model_config=model_config, prompt=prompt,
                mode=args.mode, secret=secret)
    except Exception:
        dispose_clean_worktree(lane)
        raise
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    summary = result.as_dict()
    status = git_status(lane)
    audit = write_audit(lane, host=result.host, mode=args.mode, provider=result.provider,
        gateway=result.gateway, auth_method=result.auth_method, billable=result.billable, model=result.model,
        prompt=prompt, exit_status=result.returncode, status=status,
        stdout=result.stdout, stderr=result.stderr)
    summary.update({"branch": lane.branch, "worktree": str(lane.worktree), "git_status": status,
                    "audit": str(audit), "result_artifact": str(audit)})
    if args.mode == "review":
        dispose_clean_worktree(lane)
        summary["worktree_disposed"] = True
    print(json.dumps(summary, indent=2, sort_keys=True))
    return result.returncode


def run(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    config = load_config()
    if args.command == "list":
        for provider, item in config["providers"].items():
            for mode, hosts in item["routes"].items():
                for host, route in hosts.items():
                    for model in route["models"]:
                        print(f"{host}\t{mode}\t{provider}\t{item['gateway']}\t{model}\t{item['auth_method']}\t{'billable' if item['billable'] else 'subscription'}")
        return 0
    if args.command == "credentials":
        states = {provider: ("not-used-oauth" if item["auth_method"] == "oauth" else ("present" if credential_present(item["credential_service"]) else "absent")) for provider, item in config["providers"].items()}
        print(json.dumps(states, sort_keys=True) if args.json else "\n".join(f"{key}\t{value}" for key, value in states.items()))
        return 0
    if args.command == "auth-status":
        status = auth_status(args.host, executable=_host_executable(args.host))
        payload = status.as_dict()
        print(json.dumps(payload, sort_keys=True) if args.json else "\n".join(f"{key}\t{value}" for key, value in payload.items()))
        return 0 if status.ready else 1
    if args.command == "check-capabilities":
        repo = validate_governance(args.repo) if args.repo else None
        report = _capability_report(config, args.host, args.mode, args.provider, args.model, repo)
        print(json.dumps(report, sort_keys=True) if args.json else "\n".join(f"{key}\t{value}" for key, value in report.items()))
        return 0
    if args.command == "recommend":
        return _recommend(args, config)
    if args.command == "evaluate":
        return _evaluate(args.input)
    repo = validate_governance(args.repo)
    return _launch(args, config, repo, load_prompt(args.prompt, args.prompt_file, args.mode))


def main() -> None:
    try:
        raise SystemExit(run())
    except (SideLaneError, AuthError, CredentialError, GovernanceError, WorktreeError,
            ClaudeAdapterError, CodexAdapterError, evaluation.EvaluationError,
            routing.RoutingError) as exc:
        print(f"side-lane: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
