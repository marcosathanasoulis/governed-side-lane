"""Offline, evidence-gated routing recommendations for Prompt it.

The catalog is deliberately a reviewed snapshot rather than a discovery or
provider client.  A recommendation is advisory: it neither verifies a local
credential nor activates, dispatches, or substitutes a model.
"""

from __future__ import annotations

from datetime import date
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = PACKAGE_ROOT / "config" / "routing-catalog.json"
ROUTING_CATALOG_ENV = "SIDE_LANE_ROUTING_CATALOG_PATH"
SUPPORTED_POLICIES = frozenset({"best-fit", "cost-optimized"})
SUPPORTED_HOST_COST_STATES = frozenset({"included-oauth", "extra-usage", "unknown"})
SUPPORTED_GLM_AVAILABILITY = frozenset({"available", "unknown", "temporarily-unavailable"})
EXECUTION_LOCATIONS = frozenset({"local-user-workspace", "cloud-only", "unknown"})
SUPPORTED_PROTOCOLS = {
    "codex": frozenset({"native-codex", "native-codex-readonly"}),
    "claude": frozenset({"native-claude", "native-claude-readonly", "anthropic-compatible", "anthropic-compatible-readonly"}),
    "devin": frozenset({"native-devin"}),
}


class RoutingError(ValueError):
    """A malformed profile or catalog cannot produce a recommendation."""


def _as_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise RoutingError(f"{label} must be an ISO-8601 date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RoutingError(f"{label} must be an ISO-8601 date") from exc


def _positive_int(value: Any, label: str, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (
        value < 0 if allow_zero else value <= 0
    ):
        raise RoutingError(f"{label} must be {'non-negative' if allow_zero else 'positive'}")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingError(f"{label} must be a non-negative finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise RoutingError(f"{label} must be a non-negative finite number")
    return number


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """Load and minimally validate a reviewed routing catalog."""

    if path is None:
        override = os.environ.get(ROUTING_CATALOG_ENV, "").strip()
        path = Path(override).expanduser() if override else DEFAULT_CATALOG_PATH
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"cannot load routing catalog: {exc}") from exc
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Mapping[str, Any]) -> None:
    if not isinstance(catalog, Mapping):
        raise RoutingError("routing catalog must be an object")
    if not isinstance(catalog.get("catalog_version"), str):
        raise RoutingError("routing catalog requires catalog_version")
    _as_date(catalog.get("catalog_verified_on"), "catalog_verified_on")
    limits = catalog.get("freshness_days")
    if not isinstance(limits, Mapping):
        raise RoutingError("routing catalog requires freshness_days")
    _positive_int(limits.get("price"), "freshness_days.price", allow_zero=False)
    _positive_int(limits.get("evidence"), "freshness_days.evidence", allow_zero=False)
    cost_contexts = catalog.get("cost_contexts")
    if cost_contexts is not None:
        if not isinstance(cost_contexts, Mapping) or cost_contexts.get("native_default") != "included-oauth" or cost_contexts.get("native_override") != "extra-usage" or cost_contexts.get("glm") != "prepaid-flat-rate":
            raise RoutingError("routing catalog cost_contexts are invalid")
    evidence_policy = catalog.get("evidence_policy")
    if evidence_policy is not None:
        if not isinstance(evidence_policy, Mapping) or evidence_policy.get("community_role") != "evaluation-prior-only" or "local-evaluation" not in evidence_policy.get("activation_requires", []):
            raise RoutingError("routing catalog evidence_policy is invalid")
    rate_cards = catalog.get("rate_cards", {})
    if not isinstance(rate_cards, Mapping):
        raise RoutingError("routing catalog rate_cards must be an object")
    for card_id, card in rate_cards.items():
        if not isinstance(card_id, str) or not card_id or not isinstance(card, Mapping):
            raise RoutingError("routing catalog has an invalid rate card")
        _as_date(card.get("verified_on"), f"rate card {card_id} verified_on")
        if not isinstance(card.get("unit"), str) or not card["unit"] or not isinstance(card.get("applicability"), str) or not card["applicability"] or not isinstance(card.get("source"), str) or not card["source"]:
            raise RoutingError(f"rate card {card_id} metadata is invalid")
        models = card.get("models")
        if not isinstance(models, Mapping) or not models:
            raise RoutingError(f"rate card {card_id} has no models")
        for model, rates in models.items():
            if not isinstance(model, str) or not model or not isinstance(rates, Mapping):
                raise RoutingError(f"rate card {card_id} has an invalid model")
            for field in ("input_per_million", "output_per_million"):
                value = rates.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    raise RoutingError(f"rate card {card_id} {model}.{field} is invalid")
    routes = catalog.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RoutingError("routing catalog requires at least one route")
    identifiers: set[str] = set()
    for route in routes:
        if not isinstance(route, Mapping):
            raise RoutingError("every route must be an object")
        route_id = route.get("id")
        if not isinstance(route_id, str) or not route_id:
            raise RoutingError("route id must be a non-empty string")
        if route_id in identifiers:
            raise RoutingError(f"duplicate route id: {route_id}")
        identifiers.add(route_id)
        for name in ("provider", "gateway", "auth_method", "model_vendor", "model", "host", "mode", "state"):
            if not isinstance(route.get(name), str) or not route[name]:
                raise RoutingError(f"route {route_id} requires {name}")
        if route["state"] not in {"discoverable", "executable"}:
            raise RoutingError(f"route {route_id} has unknown state")
        if route["host"] not in SUPPORTED_PROTOCOLS:
            raise RoutingError(f"route {route_id} has unsupported host")
        protocol = route.get("protocol")
        if protocol not in SUPPORTED_PROTOCOLS[route["host"]]:
            raise RoutingError(f"route {route_id} has incompatible protocol")
        if route["state"] == "executable":
            if route.get("execution_allowlisted") is not True:
                raise RoutingError(f"route {route_id} is executable but not allowlisted")
            _validate_executable_route(route, route_id)
    candidates = catalog.get("candidates", [])
    if not isinstance(candidates, list):
        raise RoutingError("routing catalog candidates must be an array")
    candidate_ids: set[str] = set(identifiers)
    for candidate in candidates:
        _validate_candidate(candidate, candidate_ids)


def _validate_candidate(candidate: object, identifiers: set[str]) -> None:
    """Validate a research candidate without treating it as a runtime route."""

    if not isinstance(candidate, Mapping):
        raise RoutingError("every candidate must be an object")
    identifier = candidate.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise RoutingError("candidate id must be a non-empty string")
    if identifier in identifiers:
        raise RoutingError(f"duplicate candidate id: {identifier}")
    identifiers.add(identifier)
    for name in ("provider", "model_vendor", "requested_model", "execution_location", "qualification_state"):
        if not isinstance(candidate.get(name), str) or not candidate[name]:
            raise RoutingError(f"candidate {identifier} requires {name}")
    if candidate.get("state") != "candidate" or candidate.get("default_enabled") is not False:
        raise RoutingError(f"candidate {identifier} must remain disabled research metadata")
    if candidate["execution_location"] not in EXECUTION_LOCATIONS:
        raise RoutingError(f"candidate {identifier} has an invalid execution_location")
    for name in ("gateway", "endpoint", "auth_method", "host", "harness", "mode", "protocol", "reasoning_setting", "identity_stability"):
        value = candidate.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise RoutingError(f"candidate {identifier} {name} is invalid")
    endpoint = candidate.get("endpoint")
    if endpoint is not None and (not endpoint.startswith("https://") or any(char.isspace() for char in endpoint)):
        raise RoutingError(f"candidate {identifier} has an invalid HTTPS endpoint")
    if candidate.get("resolved_model") is not None and not isinstance(candidate["resolved_model"], str):
        raise RoutingError(f"candidate {identifier} resolved_model is invalid")
    model_evidence = candidate.get("model_evidence")
    _validate_reviewed_evidence(model_evidence, f"candidate {identifier} model evidence")
    endpoint_evidence = candidate.get("endpoint_evidence")
    if endpoint_evidence is not None:
        _validate_reviewed_evidence(endpoint_evidence, f"candidate {identifier} endpoint evidence")


def _validate_reviewed_evidence(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("verified") is not True:
        raise RoutingError(f"{label} is not reviewed")
    _as_date(value.get("verified_on"), f"{label}.verified_on")
    if not isinstance(value.get("source"), str) or not value["source"]:
        raise RoutingError(f"{label}.source is required")
    return value


def _validate_executable_route(route: Mapping[str, Any], route_id: str) -> None:
    if route.get("connector_retention") not in {"origin-host", "worker-host"}:
        raise RoutingError(f"route {route_id} does not identify connector ownership")
    _validate_reviewed_evidence(
        route.get("connector_evidence"), f"route {route_id} connector evidence"
    )
    evaluation = _validate_reviewed_evidence(
        route.get("local_evaluation"), f"route {route_id} local evaluation"
    )
    scores = evaluation.get("task_scores")
    if not isinstance(scores, Mapping) or not scores:
        raise RoutingError(f"route {route_id} lacks task-relative scores")
    if not all(
        isinstance(band, str)
        and band
        and not isinstance(score, bool)
        and isinstance(score, int)
        and 0 <= score <= 100
        for band, score in scores.items()
    ):
        raise RoutingError(f"route {route_id} has invalid task-relative scores")
    acceptance_rate = evaluation.get("acceptance_rate")
    if isinstance(acceptance_rate, bool) or not isinstance(acceptance_rate, (int, float)) or not 0 < acceptance_rate <= 1:
        raise RoutingError(f"route {route_id} lacks a valid acceptance_rate")
    median_duration_ms = evaluation.get("median_duration_ms")
    if median_duration_ms is not None and (
        isinstance(median_duration_ms, bool)
        or not isinstance(median_duration_ms, (int, float))
        or not math.isfinite(float(median_duration_ms))
        or median_duration_ms <= 0
    ):
        raise RoutingError(f"route {route_id} local_evaluation.median_duration_ms is invalid")
    cost_model = route.get("cost_model")
    if not isinstance(cost_model, Mapping):
        raise RoutingError(f"route {route_id} lacks a reviewed cost model")
    _as_date(cost_model.get("verified_on"), f"route {route_id} cost_model.verified_on")
    if not isinstance(cost_model.get("source"), str) or not cost_model["source"]:
        raise RoutingError(f"route {route_id} cost_model.source is required")
    if cost_model.get("basis") not in {"native-oauth", "workspace-credits", "external-billable", "prepaid-flat-rate"}:
        raise RoutingError(f"route {route_id} cost_model.basis is invalid")
    rates = cost_model.get("rates")
    if rates is not None:
        if not isinstance(rates, Mapping) or not isinstance(rates.get("unit"), str) or not rates["unit"]:
            raise RoutingError(f"route {route_id} cost_model.rates is invalid")
        for field in ("input_per_million", "output_per_million"):
            value = rates.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise RoutingError(f"route {route_id} cost_model.rates.{field} is invalid")
    capabilities = route.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise RoutingError(f"route {route_id} lacks reviewed capabilities")
    supported = capabilities.get("supported")
    roles = capabilities.get("authority_roles")
    if not isinstance(supported, list) or not all(
        isinstance(item, str) and item for item in supported
    ):
        raise RoutingError(f"route {route_id} capabilities.supported is invalid")
    if not isinstance(roles, list) or not all(
        item in {"worker", "reviewer", "coordinator"} for item in roles
    ):
        raise RoutingError(f"route {route_id} capabilities.authority_roles is invalid")
    required_role = "worker" if route["mode"] == "execute" else "reviewer"
    if required_role not in roles:
        raise RoutingError(f"route {route_id} lacks the required authority role")
    behavioral = route.get("behavioral_capabilities", {})
    if not isinstance(behavioral, Mapping):
        raise RoutingError(f"route {route_id} behavioral_capabilities is invalid")
    for name, evidence_record in behavioral.items():
        if not isinstance(name, str) or not name or not isinstance(evidence_record, Mapping):
            raise RoutingError(f"route {route_id} behavioral capability is invalid")
        score = evidence_record.get("score")
        types = evidence_record.get("evidence_types")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise RoutingError(f"route {route_id} behavioral capability score is invalid")
        if not isinstance(types, list) or "local-evaluation" not in types:
            raise RoutingError(f"route {route_id} behavioral capability lacks local evidence")


def allowlist_from_models(models: Mapping[str, Any]) -> frozenset[tuple[str, str, str, str]]:
    """Extract exact executable tuples from the separate runtime allowlist.

    The routing catalog's human-readable ``allowlist_ref`` is evidence for a
    reviewer, not authority.  The caller must pass this result to
    :func:`recommend` for an executable route to be selected.
    """

    providers = models.get("providers")
    if not isinstance(providers, Mapping):
        raise RoutingError("runtime allowlist must have a providers object")
    entries: set[tuple[str, str, str, str]] = set()
    for provider, provider_data in providers.items():
        if not isinstance(provider, str) or not isinstance(provider_data, Mapping):
            raise RoutingError("runtime allowlist has an invalid provider")
        routes = provider_data.get("routes")
        if not isinstance(routes, Mapping):
            continue
        for mode, hosts in routes.items():
            if not isinstance(hosts, Mapping):
                continue
            for host, route in hosts.items():
                if not isinstance(route, Mapping):
                    continue
                for model in route.get("models", []):
                    if isinstance(model, str) and model:
                        entries.add((provider, host, mode, model))
    return frozenset(entries)


def _fresh_on(record: Mapping[str, Any], key: str, now: date, max_days: int) -> bool:
    try:
        recorded = _as_date(record.get(key), key)
    except RoutingError:
        return False
    return 0 <= (now - recorded).days <= max_days


def _normalize_attempts(attempts: object, label: str) -> tuple[dict[str, Any], ...]:
    """Validate one observed or hypothetical task-session attempt sequence."""

    if not isinstance(attempts, list) or not attempts:
        raise RoutingError(f"{label} must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            raise RoutingError(f"{label} entries must be objects")
        kind = attempt.get("kind", "attempt")
        if not isinstance(kind, str) or not kind:
            raise RoutingError(f"{label}.kind must be a non-empty string")
        normalized.append({
            "kind": kind,
            "uncached_input_tokens": _positive_int(attempt.get("uncached_input_tokens", 0), f"{label}[{index}].uncached_input_tokens"),
            "cached_read_tokens": _positive_int(attempt.get("cached_read_tokens", 0), f"{label}[{index}].cached_read_tokens"),
            "cache_write_tokens": _positive_int(attempt.get("cache_write_tokens", 0), f"{label}[{index}].cache_write_tokens"),
            "output_tokens": _positive_int(attempt.get("output_tokens", 0), f"{label}[{index}].output_tokens"),
            "reasoning_tokens": _positive_int(attempt.get("reasoning_tokens", 0), f"{label}[{index}].reasoning_tokens"),
            "coordinator_cost_usd": (
                None if attempt.get("coordinator_cost_usd") is None
                else _nonnegative_number(attempt.get("coordinator_cost_usd"), f"{label}[{index}].coordinator_cost_usd")
            ),
            "tool_cost_usd": _nonnegative_number(attempt.get("tool_cost_usd", 0), f"{label}[{index}].tool_cost_usd"),
            "host_cost_usd": _nonnegative_number(attempt.get("host_cost_usd", 0), f"{label}[{index}].host_cost_usd"),
        })
    return tuple(normalized)


def _profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise RoutingError("task profile must be an object")
    host = profile.get("coordinator_host", profile.get("originating_host"))
    if host not in SUPPORTED_PROTOCOLS:
        raise RoutingError("originating_host must be codex or claude")
    mode = profile.get("mode")
    if mode not in {"review", "execute"}:
        raise RoutingError("mode must be review or execute")
    policy = profile.get("policy", "best-fit")
    if policy not in SUPPORTED_POLICIES:
        raise RoutingError("policy must be best-fit or cost-optimized")
    task_band = profile.get("task_band")
    if not isinstance(task_band, str) or not task_band:
        raise RoutingError("task_band is required")
    quality_floor = _positive_int(profile.get("quality_floor"), "quality_floor")
    if quality_floor > 100:
        raise RoutingError("quality_floor must not exceed 100")
    required_connectors = profile.get("required_connectors", [])
    available_connectors = profile.get("available_connectors", [])
    if not all(isinstance(item, str) and item for item in required_connectors):
        raise RoutingError("required_connectors must contain non-empty strings")
    if not all(isinstance(item, str) and item for item in available_connectors):
        raise RoutingError("available_connectors must contain non-empty strings")
    required_capabilities = profile.get("required_capabilities", [])
    available_capabilities = profile.get("available_capabilities", [])
    if not all(isinstance(item, str) and item for item in required_capabilities):
        raise RoutingError("required_capabilities must contain non-empty strings")
    if not all(isinstance(item, str) and item for item in available_capabilities):
        raise RoutingError("available_capabilities must contain non-empty strings")
    required_behavioral = profile.get("required_behavioral_capabilities", [])
    if not isinstance(required_behavioral, list) or not all(
        isinstance(item, str) and item for item in required_behavioral
    ):
        raise RoutingError("required_behavioral_capabilities must contain non-empty strings")
    host_capabilities = profile.get("host_capabilities")
    if host_capabilities is None:
        host_capabilities = {
            host: {
                "available_connectors": available_connectors,
                "available_capabilities": available_capabilities,
            }
        }
    if not isinstance(host_capabilities, Mapping):
        raise RoutingError("host_capabilities must be an object")
    normalized_hosts: dict[str, dict[str, frozenset[str]]] = {}
    for candidate_host, snapshot in host_capabilities.items():
        if candidate_host not in SUPPORTED_PROTOCOLS or not isinstance(snapshot, Mapping):
            raise RoutingError("host_capabilities contains an invalid host snapshot")
        connectors = snapshot.get("available_connectors", [])
        capabilities = snapshot.get("available_capabilities", [])
        if not isinstance(connectors, list) or not all(isinstance(item, str) and item for item in connectors):
            raise RoutingError(f"host_capabilities.{candidate_host}.available_connectors is invalid")
        if not isinstance(capabilities, list) or not all(isinstance(item, str) and item for item in capabilities):
            raise RoutingError(f"host_capabilities.{candidate_host}.available_capabilities is invalid")
        normalized_hosts[str(candidate_host)] = {
            "available_connectors": frozenset(connectors),
            "available_capabilities": frozenset(capabilities),
        }
    host_cost_state = profile.get("host_cost_state", {})
    if not isinstance(host_cost_state, Mapping):
        raise RoutingError("host_cost_state must be an object")
    normalized_cost_state = {name: "included-oauth" for name in SUPPORTED_PROTOCOLS}
    for candidate_host, state in host_cost_state.items():
        if candidate_host not in SUPPORTED_PROTOCOLS or state not in SUPPORTED_HOST_COST_STATES:
            raise RoutingError("host_cost_state contains an invalid host or state")
        normalized_cost_state[str(candidate_host)] = str(state)
    route_spend_state = profile.get("route_spend_state", {})
    if not isinstance(route_spend_state, Mapping) or not all(
        isinstance(route_id, str) and route_id and state in SUPPORTED_HOST_COST_STATES
        for route_id, state in route_spend_state.items()
    ):
        raise RoutingError("route_spend_state contains an invalid route or state")
    declared_plan_state = profile.get("declared_plan_state", {})
    if not isinstance(declared_plan_state, Mapping) or not all(
        isinstance(route_id, str) and route_id and isinstance(state, str) and state
        for route_id, state in declared_plan_state.items()
    ):
        raise RoutingError("declared_plan_state contains an invalid route or state")
    include_glm = profile.get("include_glm", False)
    if not isinstance(include_glm, bool):
        raise RoutingError("include_glm must be boolean")
    glm_availability = profile.get("glm_availability", "unknown")
    if glm_availability not in SUPPORTED_GLM_AVAILABILITY:
        raise RoutingError("glm_availability is invalid")
    preference = profile.get("prefer")
    vendor_aliases = {"codex": "openai"}
    if preference is not None and (not isinstance(preference, str) or not preference):
        raise RoutingError("prefer must be a non-empty provider name")
    preferred_pool = profile.get("preferred_provider_pool", [])
    if not isinstance(preferred_pool, list) or not all(
        isinstance(item, str) and item for item in preferred_pool
    ):
        raise RoutingError("preferred_provider_pool must contain provider or model_vendor names")
    if len(set(preferred_pool)) != len(preferred_pool):
        raise RoutingError("preferred_provider_pool must not contain duplicates")
    avoided = profile.get("avoid", [])
    if not isinstance(avoided, list) or not all(isinstance(item, str) and item for item in avoided):
        raise RoutingError("avoid must contain non-empty provider names")
    attempts = profile.get("session_attempts")
    if attempts is None:
        attempts = [{
            "uncached_input_tokens": profile.get("input_tokens", 0),
            "cached_read_tokens": profile.get("cached_input_tokens", 0),
            "output_tokens": profile.get("output_tokens", 0),
            "kind": "primary",
        }]
    normalized_attempts = _normalize_attempts(attempts, "session_attempts")
    accepted_completions = profile.get("accepted_completions")
    if accepted_completions is not None:
        accepted_completions = _positive_int(accepted_completions, "accepted_completions")
    session_cost_basis = profile.get("session_cost_basis", "hypothetical-workload")
    if session_cost_basis not in {"hypothetical-workload", "route-specific-cohort", "route-specific-cohorts"}:
        raise RoutingError("session_cost_basis is invalid")
    if session_cost_basis == "route-specific-cohort" and accepted_completions is None:
        raise RoutingError("route-specific-cohort requires accepted_completions")
    if session_cost_basis == "hypothetical-workload" and accepted_completions is not None:
        raise RoutingError("hypothetical-workload must not include accepted_completions")
    cohort_route_id = profile.get("cohort_route_id")
    if session_cost_basis == "route-specific-cohort" and (not isinstance(cohort_route_id, str) or not cohort_route_id):
        raise RoutingError("route-specific-cohort requires cohort_route_id")
    if cohort_route_id is not None and (not isinstance(cohort_route_id, str) or not cohort_route_id):
        raise RoutingError("cohort_route_id is invalid")
    raw_route_cohorts = profile.get("route_session_cohorts")
    if session_cost_basis == "route-specific-cohorts":
        if accepted_completions is not None or cohort_route_id is not None:
            raise RoutingError("route-specific-cohorts uses per-route accepted completions")
        if not isinstance(raw_route_cohorts, Mapping) or not raw_route_cohorts:
            raise RoutingError("route-specific-cohorts requires route_session_cohorts")
    elif raw_route_cohorts is not None:
        raise RoutingError("route_session_cohorts requires route-specific-cohorts")
    normalized_route_cohorts: dict[str, dict[str, Any]] = {}
    if isinstance(raw_route_cohorts, Mapping):
        for route_id, cohort in raw_route_cohorts.items():
            if not isinstance(route_id, str) or not route_id or not isinstance(cohort, Mapping):
                raise RoutingError("route_session_cohorts contains an invalid route cohort")
            if "accepted_completions" not in cohort:
                raise RoutingError(f"route_session_cohorts.{route_id} requires accepted_completions")
            normalized_route_cohorts[route_id] = {
                "session_attempts": _normalize_attempts(
                    cohort.get("session_attempts"), f"route_session_cohorts.{route_id}.session_attempts"
                ),
                "accepted_completions": _positive_int(
                    cohort.get("accepted_completions"),
                    f"route_session_cohorts.{route_id}.accepted_completions",
                ),
            }
    max_duration_ms = profile.get("max_duration_ms")
    if max_duration_ms is not None:
        max_duration_ms = _positive_int(max_duration_ms, "max_duration_ms", allow_zero=False)
    normalized_preferred_pool = tuple(vendor_aliases.get(item, item) for item in preferred_pool)
    if len(set(normalized_preferred_pool)) != len(normalized_preferred_pool):
        raise RoutingError("preferred_provider_pool contains duplicate effective providers")
    return {
        "originating_host": host,
        "coordinator_host": host,
        "mode": mode,
        "policy": policy,
        "task_band": task_band,
        "quality_floor": quality_floor,
        "required_connectors": frozenset(required_connectors),
        "available_connectors": frozenset(available_connectors),
        "required_capabilities": frozenset(required_capabilities),
        "available_capabilities": frozenset(available_capabilities),
        "required_behavioral_capabilities": frozenset(required_behavioral),
        "host_capabilities": normalized_hosts,
        "host_cost_state": normalized_cost_state,
        "route_spend_state": dict(route_spend_state),
        "declared_plan_state": dict(declared_plan_state),
        "include_glm": include_glm,
        "glm_availability": glm_availability,
        "input_tokens": _positive_int(profile.get("input_tokens", 0), "input_tokens"),
        "cached_input_tokens": _positive_int(
            profile.get("cached_input_tokens", 0), "cached_input_tokens"
        ),
        "output_tokens": _positive_int(profile.get("output_tokens", 0), "output_tokens"),
        "session_attempts": normalized_attempts,
        "accepted_completions": accepted_completions,
        "session_cost_basis": session_cost_basis,
        "cohort_route_id": cohort_route_id,
        "route_session_cohorts": normalized_route_cohorts,
        "privacy_class": profile.get("privacy_class", "ordinary"),
        "prefer": vendor_aliases.get(preference, preference),
        "declared_prefer": preference,
        "preferred_provider_pool": frozenset(normalized_preferred_pool),
        "declared_preferred_provider_pool": tuple(preferred_pool),
        "avoid": frozenset(vendor_aliases.get(item, item) for item in avoided),
        "declared_avoid": tuple(avoided),
        "max_duration_ms": max_duration_ms,
    }


def estimate_session_cost(
    cost_model: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    *,
    host_cost_state: str,
    now: date,
    max_days: int,
    accepted_completions: int | None = None,
    plan_state: str | None = None,
) -> dict[str, Any] | None:
    """Estimate one complete task session without hiding failed work.

    Attempts may include primary work, retry, correction, review, or coordinator
    handoffs.  Tool and execution-host charges are explicit USD cash overhead;
    they cannot be combined with an unknown or non-USD model unit.  Acquisition
    is deliberately separate from dispatch cash and is never charged to a task.
    """

    if not isinstance(cost_model, Mapping) or not _fresh_on(cost_model, "verified_on", now, max_days):
        return None
    if host_cost_state not in SUPPORTED_HOST_COST_STATES:
        return None
    basis = cost_model.get("basis")
    if basis not in {"native-oauth", "workspace-credits", "external-billable", "prepaid-flat-rate"}:
        return None
    if not attempts:
        return None
    overhead = 0.0
    normalized_attempts: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            return None
        try:
            normalized = {
                "kind": str(attempt.get("kind", "attempt")),
                "uncached_input_tokens": _nonnegative_number(attempt.get("uncached_input_tokens", 0), "uncached_input_tokens"),
                "cached_read_tokens": _nonnegative_number(attempt.get("cached_read_tokens", 0), "cached_read_tokens"),
                "cache_write_tokens": _nonnegative_number(attempt.get("cache_write_tokens", 0), "cache_write_tokens"),
                "output_tokens": _nonnegative_number(attempt.get("output_tokens", 0), "output_tokens"),
                "reasoning_tokens": _nonnegative_number(attempt.get("reasoning_tokens", 0), "reasoning_tokens"),
                "coordinator_cost_usd": (
                    None if attempt.get("coordinator_cost_usd") is None
                    else _nonnegative_number(attempt.get("coordinator_cost_usd"), "coordinator_cost_usd")
                ),
                "tool_cost_usd": _nonnegative_number(attempt.get("tool_cost_usd", 0), "tool_cost_usd"),
                "host_cost_usd": _nonnegative_number(attempt.get("host_cost_usd", 0), "host_cost_usd"),
            }
        except RoutingError:
            return None
        if not normalized["kind"]:
            return None
        normalized_attempts.append(normalized)
        overhead += normalized["tool_cost_usd"] + normalized["host_cost_usd"]
    included = basis == "prepaid-flat-rate" or (
        basis == "native-oauth" and host_cost_state == "included-oauth"
    )
    if host_cost_state == "unknown" and not included:
        return None
    model_value = 0.0
    if included:
        unit = "incremental-usd"
    else:
        rates = cost_model.get("rates")
        bands = cost_model.get("rate_bands")
        rate_sets: list[Mapping[str, Any]]
        if bands is not None:
            if not isinstance(bands, list):
                return None
            rate_sets = []
            for attempt in normalized_attempts:
                attempt_input = attempt["uncached_input_tokens"] + attempt["cached_read_tokens"] + attempt["cache_write_tokens"]
                matches: list[Mapping[str, Any]] = []
                for band in bands:
                    if not isinstance(band, Mapping) or not isinstance(band.get("rates"), Mapping):
                        return None
                    required_plan = band.get("plan_state")
                    if required_plan is not None and required_plan != plan_state:
                        continue
                    try:
                        minimum = _nonnegative_number(band.get("min_input_tokens", 0), "min_input_tokens")
                        maximum_value = band.get("max_input_tokens")
                        maximum = None if maximum_value is None else _nonnegative_number(maximum_value, "max_input_tokens")
                        starts = band.get("starts_on")
                        expires = band.get("expires_on")
                        if starts is not None and now < _as_date(starts, "starts_on"):
                            continue
                        if expires is not None and now > _as_date(expires, "expires_on"):
                            continue
                    except RoutingError:
                        return None
                    if attempt_input >= minimum and (maximum is None or attempt_input <= maximum):
                        matches.append(band["rates"])
                if len(matches) != 1:
                    return None
                rate_sets.append(matches[0])
        elif isinstance(rates, Mapping):
            rate_sets = [rates] * len(normalized_attempts)
        else:
            return None
        unit: str | None = None
        for attempt, attempt_rates in zip(normalized_attempts, rate_sets):
            attempt_unit = attempt_rates.get("unit")
            if not isinstance(attempt_unit, str) or not attempt_unit:
                return None
            if unit is None:
                unit = attempt_unit
            elif unit != attempt_unit:
                return None
            try:
                input_rate = _nonnegative_number(attempt_rates.get("input_per_million"), "input_per_million")
                cached_rate = _nonnegative_number(attempt_rates.get("cached_read_per_million", attempt_rates.get("cached_input_per_million", input_rate)), "cached_read_per_million")
                output_rate = _nonnegative_number(attempt_rates.get("output_per_million"), "output_per_million")
                cache_write_rate = attempt_rates.get("cache_write_per_million")
                reasoning_rate = _nonnegative_number(attempt_rates.get("reasoning_per_million", output_rate), "reasoning_per_million")
                if cache_write_rate is not None:
                    cache_write_rate = _nonnegative_number(cache_write_rate, "cache_write_per_million")
            except RoutingError:
                return None
            if attempt["cache_write_tokens"] and cache_write_rate is None:
                return None
            model_value += (
                attempt["uncached_input_tokens"] * input_rate
                + attempt["cached_read_tokens"] * cached_rate
                + attempt["cache_write_tokens"] * (cache_write_rate or 0.0)
                + attempt["output_tokens"] * output_rate
                + attempt["reasoning_tokens"] * reasoning_rate
            ) / 1_000_000
    for attempt in normalized_attempts:
        if attempt["coordinator_cost_usd"] is not None:
            if unit not in {"usd", "incremental-usd"}:
                return None
            model_value += attempt["coordinator_cost_usd"]
    # Tool/host overhead is cash. A non-cash model unit cannot be compared to it.
    if overhead and unit not in {"usd", "incremental-usd"}:
        return None
    value = round(model_value + overhead, 8)
    acquisition = cost_model.get("acquisition", {})
    if acquisition is not None and not isinstance(acquisition, Mapping):
        return None
    acquisition_summary: dict[str, float] | None = None
    if acquisition:
        try:
            monthly_fee = _nonnegative_number(acquisition.get("monthly_fee_usd", 0), "monthly_fee_usd")
            setup_cost = _nonnegative_number(acquisition.get("setup_cost_usd", 0), "setup_cost_usd")
            expected_overflow = _nonnegative_number(acquisition.get("expected_overflow_usd", 0), "expected_overflow_usd")
        except RoutingError:
            return None
        acquisition_summary = {
            "monthly_fee_usd": monthly_fee,
            "setup_cost_usd": setup_cost,
            "expected_overflow_usd": expected_overflow,
            "total_usd": round(monthly_fee + setup_cost + expected_overflow, 8),
        }
    expected = None
    if accepted_completions is not None:
        if accepted_completions > 0:
            expected = round(value / accepted_completions, 8)
        # Zero successful completions intentionally has no finite cost estimate.
    return {
        "value": value,
        "unit": unit,
        "basis": basis,
        "incremental_zero": value == 0,
        "host_cost_state": "prepaid-flat-rate" if basis == "prepaid-flat-rate" else host_cost_state,
        "attempt_count": len(normalized_attempts),
        "tool_and_host_overhead_usd": round(overhead, 8),
        "expected_cost_per_accepted_result": expected,
        "accepted_completions": accepted_completions,
        "acquisition_cost": acquisition_summary,
    }


def _estimate_cost(
    route: Mapping[str, Any], profile: Mapping[str, Any], now: date, max_days: int
) -> dict[str, Any] | None:
    cost_model = route.get("cost_model")
    if not isinstance(cost_model, Mapping):
        return None
    if profile["session_cost_basis"] == "route-specific-cohort" and route.get("id") != profile["cohort_route_id"]:
        return None
    attempts = profile["session_attempts"]
    accepted_completions = (
        profile["accepted_completions"]
        if profile["session_cost_basis"] == "route-specific-cohort" else None
    )
    if profile["session_cost_basis"] == "route-specific-cohorts":
        cohort = profile["route_session_cohorts"].get(str(route.get("id")))
        if cohort is None:
            return None
        attempts = cohort["session_attempts"]
        accepted_completions = cohort["accepted_completions"]
    return estimate_session_cost(
        cost_model,
        attempts,
        host_cost_state=profile["route_spend_state"].get(
            str(route.get("id")), profile["host_cost_state"].get(route.get("host"), "unknown")
        ),
        now=now,
        max_days=max_days,
        accepted_completions=accepted_completions,
        plan_state=profile["declared_plan_state"].get(str(route.get("id"))),
    )


def _route_execution_location(route: Mapping[str, Any]) -> str:
    """Use a narrow migration inference for pre-location native records only."""

    declared = route.get("execution_location")
    if declared in EXECUTION_LOCATIONS:
        return str(declared)
    if route.get("protocol") in {"native-codex", "native-codex-readonly", "native-claude", "native-claude-readonly"}:
        return "local-user-workspace"
    return "unknown"


def _candidate_or_reasons(
    route: Mapping[str, Any],
    profile: Mapping[str, Any],
    now: date,
    catalog: Mapping[str, Any],
    runtime_allowlist: frozenset[tuple[str, str, str, str]],
    credential_present_routes: frozenset[tuple[str, str, str, str]],
) -> tuple[dict[str, Any] | None, list[str]]:
    route_id = str(route.get("id", "unknown"))
    reasons: list[str] = []
    is_glm = route.get("model_vendor") == "glm"
    if is_glm and not profile["include_glm"]:
        reasons.append("explicit-opt-in-required")
    if is_glm and profile["glm_availability"] == "temporarily-unavailable":
        reasons.append("glm-temporarily-unavailable")
    if route.get("state") != "executable":
        reasons.append("not-executable")
    exact_tuple = (
        route.get("provider"), route.get("host"), route.get("mode"), route.get("model")
    )
    if route.get("execution_allowlisted") is not True:
        reasons.append("not-allowlisted")
    elif exact_tuple not in runtime_allowlist:
        reasons.append("runtime-allowlist-mismatch")
    if exact_tuple not in credential_present_routes:
        reasons.append("credential-absent")
    if route.get("mode") != profile["mode"]:
        reasons.append("mode-mismatch")
    execution_location = _route_execution_location(route)
    if profile["mode"] == "execute" and execution_location != "local-user-workspace":
        reasons.append("not-local-user-workspace")
    host = route.get("host")
    if route.get("protocol") not in SUPPORTED_PROTOCOLS.get(host, frozenset()):
        reasons.append("unsupported-protocol")
    connector_owner = route.get("connector_retention")
    if connector_owner not in {"origin-host", "worker-host"}:
        reasons.append("connector-retention-unverified")
    if route.get("host") != profile["coordinator_host"] and connector_owner != "worker-host":
        reasons.append("cross-host-connector-ownership-unverified")
    connector = route.get("connector_evidence")
    if not isinstance(connector, Mapping) or connector.get("verified") is not True:
        reasons.append("connector-evidence-missing")
    elif not _fresh_on(
        connector, "verified_on", now, int(catalog["freshness_days"]["evidence"])
    ):
        reasons.append("connector-evidence-stale")
    worker_snapshot = profile["host_capabilities"].get(route.get("host"))
    if worker_snapshot is None:
        reasons.append("worker-host-capability-snapshot-missing")
        worker_connectors: frozenset[str] = frozenset()
        worker_capabilities: frozenset[str] = frozenset()
    else:
        worker_connectors = worker_snapshot["available_connectors"]
        worker_capabilities = worker_snapshot["available_capabilities"]
    if not profile["required_connectors"].issubset(worker_connectors):
        reasons.append("required-connector-unavailable-on-worker-host")
    if not profile["required_capabilities"].issubset(worker_capabilities):
        reasons.append("required-capability-unavailable-on-worker-host")
    route_capabilities = route.get("capabilities")
    if not isinstance(route_capabilities, Mapping):
        reasons.append("route-capability-evidence-missing")
    else:
        supported = route_capabilities.get("supported", [])
        roles = route_capabilities.get("authority_roles", [])
        if not isinstance(supported, list) or not profile[
            "required_capabilities"
        ].issubset(supported):
            reasons.append("route-capability-not-supported")
        required_role = "worker" if profile["mode"] == "execute" else "reviewer"
        if not isinstance(roles, list) or required_role not in roles:
            reasons.append("authority-role-not-supported")
    # A GLM model has no connector identity of its own. It uses the selected
    # worker host and must prove that ownership before using any connector.
    if is_glm and profile["required_connectors"]:
        if route.get("connector_retention") != "worker-host":
            reasons.append("glm-connector-hard-gate")
    evidence = route.get("local_evaluation")
    score: int | None = None
    if not isinstance(evidence, Mapping) or evidence.get("verified") is not True:
        reasons.append("local-eval-missing")
    elif not _fresh_on(
        evidence, "verified_on", now, int(catalog["freshness_days"]["evidence"])
    ):
        reasons.append("local-eval-stale")
    else:
        scores = evidence.get("task_scores")
        if not isinstance(scores, Mapping) or not isinstance(scores.get(profile["task_band"]), int):
            reasons.append("task-fit-unverified")
        else:
            score = int(scores[profile["task_band"]])
            if score < profile["quality_floor"]:
                reasons.append("quality-floor-not-met")
    median_duration_ms: float | None = None
    if profile["max_duration_ms"] is not None:
        raw_duration = evidence.get("median_duration_ms") if isinstance(evidence, Mapping) else None
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or raw_duration <= 0:
            reasons.append("duration-unverified")
        else:
            median_duration_ms = float(raw_duration)
            if median_duration_ms > profile["max_duration_ms"]:
                reasons.append("duration-budget-exceeded")
    behavioral = route.get("behavioral_capabilities")
    for required in profile["required_behavioral_capabilities"]:
        record = behavioral.get(required) if isinstance(behavioral, Mapping) else None
        if not isinstance(record, Mapping):
            reasons.append(f"behavioral-capability-unverified:{required}")
            continue
        evidence_types = record.get("evidence_types")
        behavior_score = record.get("score")
        if not isinstance(evidence_types, list) or "local-evaluation" not in evidence_types:
            reasons.append(f"behavioral-capability-lacks-local-evidence:{required}")
        if isinstance(behavior_score, bool) or not isinstance(behavior_score, int) or behavior_score < profile["quality_floor"]:
            reasons.append(f"behavioral-capability-floor-not-met:{required}")
    privacy = route.get("privacy_classes", ["ordinary"])
    if not isinstance(privacy, list) or profile["privacy_class"] not in privacy:
        reasons.append("privacy-boundary-not-met")
    if route.get("model_vendor") in profile["avoid"]:
        reasons.append("provider-avoided")
    cost = _estimate_cost(
        route, profile, now, int(catalog["freshness_days"]["price"])
    )
    if (profile["session_cost_basis"] == "route-specific-cohorts"
            and route_id not in profile["route_session_cohorts"]):
        reasons.append("route-session-cohort-missing")
    if profile["policy"] == "cost-optimized" and cost is None:
        reasons.append("cost-basis-missing-or-stale")
    expected_cost: float | None = None
    if cost is not None:
        expected_cost = cost.get("expected_cost_per_accepted_result")
        if expected_cost is None and profile["session_cost_basis"] == "hypothetical-workload":
            acceptance = evidence.get("acceptance_rate") if isinstance(evidence, Mapping) else None
            if isinstance(acceptance, (int, float)) and not isinstance(acceptance, bool) and acceptance > 0:
                expected_cost = round(float(cost["value"]) / float(acceptance), 8)
        if profile["policy"] == "cost-optimized" and expected_cost is None:
            reasons.append("accepted-completion-cost-unknown")
    if reasons:
        return None, reasons
    return {
        "route_id": route_id,
        "provider": route["provider"],
        "gateway": route["gateway"],
        "auth_method": route["auth_method"],
        "billable": route.get("billable") is True,
        "model_vendor": route["model_vendor"],
        "model": route["model"],
        "requested_model": route["model"],
        "resolved_model": route.get("resolved_model"),
        "host": route["host"],
        "harness": route.get("harness", "codex-cli" if route["host"] == "codex" else "claude-code"),
        "reasoning_effort": route.get("reasoning_effort", "unknown"),
        "coordinator_host": profile["coordinator_host"],
        "connector_identity_changed": route["host"] != profile["coordinator_host"],
        "mode": route["mode"],
        "protocol": route["protocol"],
        "execution_location": execution_location,
        "quality_score": score,
        "median_duration_ms": median_duration_ms,
        "estimated_cost": cost,
        "estimated_cost_usd": (
            cost["value"] if cost is not None and cost["unit"] in {"usd", "incremental-usd"} else None
        ),
        "expected_cost_per_accepted_result": expected_cost,
        "cost_per_accepted_basis": (
            "observed-route-specific-cohort" if profile["session_cost_basis"] == "route-specific-cohort"
            else "observed-route-specific-cohorts" if profile["session_cost_basis"] == "route-specific-cohorts"
            else "local-evaluation-rate-assumption"
        ),
        "acceptance_rate": evidence.get("acceptance_rate"),
        "behavioral_capabilities": sorted(profile["required_behavioral_capabilities"]),
        "community_signal_refs": sorted(route.get("community_signal_refs", [])),
        "glm_availability": profile["glm_availability"] if is_glm else None,
        "local_evaluation_source": evidence.get("source"),
        "local_evaluation_verified_on": evidence.get("verified_on"),
        "pricing_verified_on": (route.get("cost_model") or {}).get("verified_on"),
        "allowlist_ref": route.get("allowlist_ref"),
    }, []


def recommend(
    catalog: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    runtime_allowlist: frozenset[tuple[str, str, str, str]],
    credential_present_routes: frozenset[tuple[str, str, str, str]],
    today: date | None = None,
) -> dict[str, Any]:
    """Return a deterministic, non-dispatching route recommendation.

    Preferred vendors form a documented soft user policy: when at least one
    qualifying route is available for that vendor, it ranks within that set.
    This makes ``prefer=claude`` compare Fable/other premium Claude candidates
    against the same task floor instead of assuming a universal tier mapping.
    """

    validate_catalog(catalog)
    normalized = _profile(profile)
    now = today or date.today()
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for route in catalog["routes"]:
        candidate, reasons = _candidate_or_reasons(
            route,
            normalized,
            now,
            catalog,
            runtime_allowlist,
            credential_present_routes,
        )
        if candidate is None:
            exclusions.append({"route_id": route["id"], "reasons": sorted(set(reasons))})
        else:
            candidates.append(candidate)

    preference_applied = False
    preferred_pool_applied = False
    preferred_pool_fallback = False
    if normalized["prefer"]:
        preferred = [item for item in candidates if item["model_vendor"] == normalized["prefer"]]
        rejected = [item for item in candidates if item["model_vendor"] != normalized["prefer"]]
        exclusions.extend(
            {"route_id": item["route_id"], "reasons": ["explicit-provider-preference"]}
            for item in rejected
        )
        candidates = preferred
        preference_applied = bool(preferred)

    if normalized["preferred_provider_pool"] and not normalized["prefer"]:
        preferred = [
            item for item in candidates
            if item["provider"] in normalized["preferred_provider_pool"]
            or item["model_vendor"] in normalized["preferred_provider_pool"]
        ]
        if preferred:
            rejected = [item for item in candidates if item not in preferred]
            exclusions.extend(
                {"route_id": item["route_id"], "reasons": ["preferred-provider-pool"]}
                for item in rejected
            )
            candidates = preferred
            preferred_pool_applied = True
        else:
            preferred_pool_fallback = True

    if normalized["policy"] == "best-fit":
        ranked = sorted(
            candidates,
            key=lambda item: (-int(item["quality_score"]), item["route_id"]),
        )
    else:
        cash_units = {"usd", "incremental-usd"}
        noncash = [
            item for item in candidates
            if item["estimated_cost"]["unit"] not in cash_units
        ]
        exclusions.extend(
            {"route_id": item["route_id"], "reasons": ["noncash-cost-unit"]}
            for item in noncash
        )
        cash_pool = [
            item for item in candidates
            if item["estimated_cost"]["unit"] in cash_units
        ]
        ranked = sorted(
            cash_pool,
            key=lambda item: (
                float(item["expected_cost_per_accepted_result"]),
                -int(item["quality_score"]),
                item["route_id"],
            ),
        )

    winner = ranked[0] if ranked else None
    return {
        "catalog_version": catalog["catalog_version"],
        "catalog_verified_on": catalog["catalog_verified_on"],
        "recommendation_state": "recommended" if winner else "no-eligible-route",
        "winner": winner,
        "ranked_routes": ranked,
        "exclusions": exclusions,
        "policy": normalized["policy"],
        "preference": normalized["declared_prefer"],
        "effective_model_vendor_preference": normalized["prefer"],
        "preference_applied": preference_applied,
        "preferred_provider_pool": list(normalized["declared_preferred_provider_pool"]),
        "preferred_provider_pool_applied": preferred_pool_applied,
        "preferred_provider_pool_fallback": preferred_pool_fallback,
        "preference_rationale": (
            "qualified-route-in-preferred-provider-pool"
            if preferred_pool_applied else
            "no-qualified-route-in-preferred-provider-pool;-ranked-all-eligible-routes"
            if preferred_pool_fallback else None
        ),
        "assumptions": {
            "originating_host": normalized["originating_host"],
            "coordinator_host": normalized["coordinator_host"],
            "mode": normalized["mode"],
            "task_band": normalized["task_band"],
            "quality_floor": normalized["quality_floor"],
            "token_budget": {
                "input": normalized["input_tokens"],
                "cached_input": normalized["cached_input_tokens"],
                "output": normalized["output_tokens"],
            },
            "session_attempts": list(normalized["session_attempts"]),
            "accepted_completions": normalized["accepted_completions"],
            "session_cost_basis": normalized["session_cost_basis"],
            "cohort_route_id": normalized["cohort_route_id"],
            "route_session_cohorts": {
                route_id: {
                    "session_attempts": list(cohort["session_attempts"]),
                    "accepted_completions": cohort["accepted_completions"],
                }
                for route_id, cohort in sorted(normalized["route_session_cohorts"].items())
            },
            "required_connectors": sorted(normalized["required_connectors"]),
            "available_connectors": sorted(normalized["available_connectors"]),
            "required_capabilities": sorted(normalized["required_capabilities"]),
            "available_capabilities": sorted(normalized["available_capabilities"]),
            "required_behavioral_capabilities": sorted(
                normalized["required_behavioral_capabilities"]
            ),
            "host_capabilities": {
                host: {
                    "available_connectors": sorted(snapshot["available_connectors"]),
                    "available_capabilities": sorted(snapshot["available_capabilities"]),
                }
                for host, snapshot in normalized["host_capabilities"].items()
            },
            "host_cost_state": dict(sorted(normalized["host_cost_state"].items())),
            "route_spend_state": dict(sorted(normalized["route_spend_state"].items())),
            "declared_plan_state": dict(sorted(normalized["declared_plan_state"].items())),
            "include_glm": normalized["include_glm"],
            "glm_availability": normalized["glm_availability"],
            "declared_avoid": list(normalized["declared_avoid"]),
            "max_duration_ms": normalized["max_duration_ms"],
            "today": now.isoformat(),
        },
        "reason_codes": (
            ["explicit-provider-preference"] if preference_applied else []
        ) + (
            ["preferred-provider-pool"] if preferred_pool_applied else
            ["preferred-provider-pool-fallback"] if preferred_pool_fallback else []
        ) + (
            ["cross-host-worker-selected"]
            if winner and winner["connector_identity_changed"]
            else []
        ) + (["exact-route-selected"] if winner else ["no-exact-route-qualifies"]),
        "dispatch_performed": False,
    }


def discovery_review_candidates(
    catalog: Mapping[str, Any], discovered: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """Compare public discovery records without changing catalog or allowlist.

    Callers can persist the returned review queue elsewhere if they choose.
    This function is intentionally pure and never returns an executable route.
    """

    validate_catalog(catalog)
    known = {(route["provider"], route["model"]) for route in catalog["routes"]}
    candidates: list[dict[str, str]] = []
    for item in discovered:
        provider = item.get("provider") if isinstance(item, Mapping) else None
        model = item.get("model") if isinstance(item, Mapping) else None
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            raise RoutingError("discovery record requires provider and model")
        if (provider, model) not in known:
            candidates.append(
                {
                    "provider": provider,
                    "model": model,
                    "state": "review-candidate",
                    "executable": "false",
                    "reason": "discovered-not-activated",
                }
            )
    return sorted(candidates, key=lambda item: (item["provider"], item["model"]))


def list_catalog_candidates(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return read-only research metadata, never provider/credential state.

    The explicit status lets callers show optional services without making them
    look available, configured, or authorized for a task.
    """

    validate_catalog(catalog)
    items: list[dict[str, Any]] = []
    for candidate in catalog.get("candidates", []):
        item = dict(candidate)
        item.update({
            "executable": False,
            "runtime_allowlisted": False,
            "credential_checked": False,
            "authorization_checked": False,
        })
        items.append(item)
    return sorted(items, key=lambda item: str(item["id"]))
