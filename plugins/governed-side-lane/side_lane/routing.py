"""Offline, evidence-gated routing recommendations for Prompt it.

The catalog is deliberately a reviewed snapshot rather than a discovery or
provider client.  A recommendation is advisory: it neither verifies a local
credential nor activates, dispatches, or substitutes a model.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = PACKAGE_ROOT / "config" / "routing-catalog.json"
SUPPORTED_POLICIES = frozenset({"best-fit", "cost-optimized"})
SUPPORTED_HOST_COST_STATES = frozenset({"included-oauth", "extra-usage", "unknown"})
SUPPORTED_GLM_AVAILABILITY = frozenset({"available", "unknown", "temporarily-unavailable"})
SUPPORTED_PROTOCOLS = {
    "codex": frozenset({"native-codex", "native-codex-readonly"}),
    "claude": frozenset({"native-claude", "native-claude-readonly", "anthropic-compatible", "anthropic-compatible-readonly"}),
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


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    """Load and minimally validate a reviewed routing catalog."""

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
    include_glm = profile.get("include_glm", False)
    if not isinstance(include_glm, bool):
        raise RoutingError("include_glm must be boolean")
    glm_availability = profile.get("glm_availability", "unknown")
    if glm_availability not in SUPPORTED_GLM_AVAILABILITY:
        raise RoutingError("glm_availability is invalid")
    preference = profile.get("prefer")
    vendor_aliases = {"codex": "openai"}
    if preference is not None and preference not in {"claude", "openai", "codex", "glm"}:
        raise RoutingError("prefer must be claude, openai, codex, or glm")
    avoided = profile.get("avoid", [])
    if not all(item in {"claude", "openai", "codex", "glm"} for item in avoided):
        raise RoutingError("avoid must contain claude, openai, codex, or glm")
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
        "include_glm": include_glm,
        "glm_availability": glm_availability,
        "input_tokens": _positive_int(profile.get("input_tokens", 0), "input_tokens"),
        "cached_input_tokens": _positive_int(
            profile.get("cached_input_tokens", 0), "cached_input_tokens"
        ),
        "output_tokens": _positive_int(profile.get("output_tokens", 0), "output_tokens"),
        "privacy_class": profile.get("privacy_class", "ordinary"),
        "prefer": vendor_aliases.get(preference, preference),
        "declared_prefer": preference,
        "avoid": frozenset(vendor_aliases.get(item, item) for item in avoided),
        "declared_avoid": tuple(avoided),
    }


def _estimate_cost(
    route: Mapping[str, Any], profile: Mapping[str, Any], now: date, max_days: int
) -> dict[str, Any] | None:
    cost_model = route.get("cost_model")
    if not isinstance(cost_model, Mapping) or not _fresh_on(
        cost_model, "verified_on", now, max_days
    ):
        return None
    basis = cost_model.get("basis")
    host_state = profile["host_cost_state"].get(route.get("host"), "unknown")
    if basis == "prepaid-flat-rate":
        return {
            "value": 0.0,
            "unit": "incremental-usd",
            "basis": basis,
            "incremental_zero": True,
            "host_cost_state": "prepaid-flat-rate",
        }
    if basis == "native-oauth" and host_state == "included-oauth":
        return {
            "value": 0.0,
            "unit": "incremental-usd",
            "basis": basis,
            "incremental_zero": True,
            "host_cost_state": host_state,
        }
    if host_state == "unknown":
        return None
    rates = cost_model.get("rates")
    if not isinstance(rates, Mapping):
        return None
    try:
        input_price = float(rates["input_per_million"])
        cached_price = float(rates.get("cached_input_per_million", input_price))
        output_price = float(rates["output_per_million"])
        unit = str(rates["unit"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(input_price, cached_price, output_price) < 0 or not unit:
        return None
    value = round(
        (
            profile["input_tokens"] * input_price
            + profile["cached_input_tokens"] * cached_price
            + profile["output_tokens"] * output_price
        )
        / 1_000_000,
        8,
    )
    return {
        "value": value,
        "unit": unit,
        "basis": basis,
        "incremental_zero": value == 0,
        "host_cost_state": host_state,
    }


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
    if profile["policy"] == "cost-optimized" and cost is None:
        reasons.append("cost-basis-missing-or-stale")
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
        "host": route["host"],
        "coordinator_host": profile["coordinator_host"],
        "connector_identity_changed": route["host"] != profile["coordinator_host"],
        "mode": route["mode"],
        "protocol": route["protocol"],
        "quality_score": score,
        "estimated_cost": cost,
        "estimated_cost_usd": (
            cost["value"] if cost is not None and cost["unit"] in {"usd", "incremental-usd"} else None
        ),
        "expected_cost_per_accepted_result": (
            None
            if cost is None or not isinstance(evidence.get("acceptance_rate"), (int, float))
            else round(float(cost["value"]) / float(evidence["acceptance_rate"]), 8)
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
    if normalized["prefer"]:
        preferred = [item for item in candidates if item["model_vendor"] == normalized["prefer"]]
        rejected = [item for item in candidates if item["model_vendor"] != normalized["prefer"]]
        exclusions.extend(
            {"route_id": item["route_id"], "reasons": ["explicit-provider-preference"]}
            for item in rejected
        )
        candidates = preferred
        preference_applied = bool(preferred)

    if normalized["policy"] == "best-fit":
        ranked = sorted(
            candidates,
            key=lambda item: (-int(item["quality_score"]), item["route_id"]),
        )
    else:
        zero_cost = [
            item for item in candidates if item["estimated_cost"]["incremental_zero"]
        ]
        pool = zero_cost or candidates
        units = {item["estimated_cost"]["unit"] for item in pool}
        if len(units) > 1:
            exclusions.extend(
                {"route_id": item["route_id"], "reasons": ["incommensurable-cost-unit"]}
                for item in pool
            )
            ranked = []
        else:
            ranked = sorted(
                pool,
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
            "include_glm": normalized["include_glm"],
            "glm_availability": normalized["glm_availability"],
            "declared_avoid": list(normalized["declared_avoid"]),
            "today": now.isoformat(),
        },
        "reason_codes": (
            ["explicit-provider-preference"] if preference_applied else []
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
