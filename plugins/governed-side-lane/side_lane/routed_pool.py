"""Routed-pool (OmniRoute-style combo) catalog admission helpers.

A ``routed_pool`` catalog route stays one opaque selector: the router — not
this package — owns member ranking, quotas, and in-call retries. Admission
here only proves that *every* possible upstream/fallback member carries
fresh, reviewed router-path task evidence bound to the member's canonical
``provider``/``model`` identity, that the runtime-configured routing policy
contract binds the selector, pinned policy revision/fingerprint, and member
set exactly, and that a fresh operator-supplied server policy snapshot binds
the selector, canonical member set, pinned revision/fingerprint, nonsecret
caps, fail-closed context policy, and evidence provenance.

Trust boundary: the snapshot is a receipt-consistency and freshness check
only. A hashed file is not a signature; offline comparison cannot prove the
live server currently enforces the policy. A control-plane collector must
authenticate to the router and refresh the snapshot before any ordinary
recommendation depends on it; this slice deliberately does not fetch
credentials or pretend a live check happened.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

SNAPSHOT_MAX_AGE_SECONDS = 900

# Approved OmniRoute pool route identity: a pool is only ever the reviewed
# local omniroute-router gateway serving claude execute traffic over the
# anthropic-compatible protocol. Other providers/hosts/modes are not pools.
POOL_PROVIDER = "omniroute"
POOL_GATEWAY = "omniroute-router"
POOL_HOST = "claude"
POOL_MODE = "execute"
POOL_PROTOCOL = "anthropic-compatible"

# Narrow *field-name* denylist for expected_caps: only well-known secret
# field names are rejected. This is a hygiene check, not a secret detector —
# a regex cannot prove a value is not a credential. Values are separately
# required to be safe scalars so structured secret blobs cannot hide
# inside caps. Plain numeric token/context caps (``max_output_tokens``,
# ``context_tokens``) are legitimate and allowed.
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|auth(?:orization)?[_-]?(?:token|header)|bearer|password"
    r"|credentials?|secret|private[_-]?key)",
    re.I,
)

# Numeric cap leaves must survive the cross-language canonical fingerprint:
# the TypeScript peer serializes through JS numbers, so only integers inside
# the IEEE-754 safe range can round-trip identically. Decimals, non-finite
# values, and integers outside +/-2**53-1 are rejected at validation.
_JS_SAFE_INTEGER_MAX = 9007199254740991

_VERIFIED_SETTINGS_PRECEDENCE = "verified"

COMPOSITE_MODE = "controlled-member-plus-auto"

_HEX_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$", re.I)


def _fresh_on(record: Mapping[str, Any], key: str, now: date, max_days: int) -> bool:
    """Return whether ``record[key]`` is a fresh ISO-8601 date."""

    try:
        recorded = date.fromisoformat(record.get(key))
    except (ValueError, TypeError):
        return False
    return 0 <= (now - recorded).days <= max_days


class RoutedPoolError(ValueError):
    """A routed-pool declaration or snapshot is malformed or unverifiable."""


def _policy_canonical(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Policy-only canonical dict for the server routing-policy fingerprint.

    Excludes admission-only blocks like ``composite_evidence`` so the
    server routing-policy fingerprint binds only the live router policy
    (selector, revision, caps, members, evalRouting). The composite
    admission block is canonicalized separately into the catalog
    declaration fingerprint via :func:`_canonical`.
    """

    members = spec.get("members", [])
    bound = {
        "selector": spec.get("selector"),
        "host": spec.get("host"),
        "protocol": spec.get("protocol"),
        "policy_revision": spec.get("policy_revision"),
        "expected_caps": spec.get("expected_caps", {}),
        "evidence_provenance": spec.get("evidence_provenance"),
        "context_fit_fail_closed": spec.get("context_fit_fail_closed"),
        "members": sorted(
            (
                {
                    "provider": member.get("provider"),
                    "model_vendor": member.get("model_vendor"),
                    "model": member.get("model"),
                    "attested_model": member.get("attested_model"),
                }
                for member in members
            ),
            key=lambda item: (str(item["provider"]), str(item["model"])),
        ),
    }
    # Optional evalRouting binding: absent entirely means the legacy
    # fingerprint is preserved byte-for-byte; when declared, the normalized
    # form joins the canonical policy so the snapshot/contract pin it.
    if spec.get("eval_routing") is not None:
        bound["eval_routing"] = _normalize_eval_routing(spec["eval_routing"])
    return bound


def _canonical(spec: Mapping[str, Any]) -> bytes:
    """Canonical byte form of the full catalog declaration.

    Members are sorted by (provider, model) so member ordering and JSON
    object key order never change the fingerprint. Everything else is
    serialized with sorted keys and fixed separators, UTF-8 encoded.
    When ``composite_evidence`` is present it is canonicalized into the
    declaration; when it is absent the policy-only canonical form is
    returned and the legacy fingerprint is preserved byte-for-byte.
    """

    bound = _policy_canonical(spec)
    if spec.get("composite_evidence") is not None:
        bound["composite_evidence"] = _normalize_composite_evidence(
            spec["composite_evidence"], spec
        )
    return json.dumps(
        bound, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def pool_fingerprint(spec: Mapping[str, Any]) -> str:
    """Deterministic sha256 fingerprint of a routed_pool catalog declaration."""

    return hashlib.sha256(_canonical(spec)).hexdigest()


def pool_policy_fingerprint(spec: Mapping[str, Any]) -> str:
    """Deterministic sha256 fingerprint of the server routing policy."""

    return hashlib.sha256(
        json.dumps(
            _policy_canonical(spec),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RoutedPoolError(f"{label} must be a non-empty string")
    return value


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RoutedPoolError(f"{label} must be a positive integer")
    return value


def _canonical_number(value: int | float) -> int | float:
    """Match JSON.stringify's spelling for whole-valued finite numbers."""

    return int(value) if isinstance(value, float) and value.is_integer() else value


def _normalize_eval_routing(value: Any) -> dict[str, Any]:
    """Validate and normalize an optional OmniRoute ``evalRouting`` binding.

    The declaration binds the operator route/runtime policy's evaluation
    suites and thresholds — it is never caller-profile input and never a
    ranking mechanism here; OmniRoute's native ``evalRouting`` remains the
    only member ordering mechanism. Normalization is deterministic: suite
    ids are sorted and must be unique non-empty strings, ``enabled`` must
    be exactly true, ``max_age_hours`` a finite positive number,
    ``min_cases`` a positive integer, and the quality/latency weights
    finite numbers in [0, 1]. Anything else fails closed.
    """

    label = "routed_pool.eval_routing"
    if not isinstance(value, Mapping):
        raise RoutedPoolError(f"{label} must be an object")
    if value.get("enabled") is not True:
        raise RoutedPoolError(f"{label}.enabled must be true")
    suite_ids = value.get("suite_ids")
    if (
        isinstance(suite_ids, (str, bytes))
        or not isinstance(suite_ids, (list, tuple))
        or not suite_ids
        or not all(isinstance(entry, str) and entry.strip() for entry in suite_ids)
    ):
        raise RoutedPoolError(
            f"{label}.suite_ids must be a non-empty array of non-empty strings"
        )
    normalized_ids = [entry.strip() for entry in suite_ids]
    unique_ids = sorted(set(normalized_ids))
    if len(unique_ids) != len(suite_ids):
        raise RoutedPoolError(f"{label}.suite_ids must be unique")
    max_age_hours = value.get("max_age_hours")
    if (
        isinstance(max_age_hours, bool)
        or not isinstance(max_age_hours, (int, float))
        or not math.isfinite(max_age_hours)
        or max_age_hours <= 0
    ):
        raise RoutedPoolError(
            f"{label}.max_age_hours must be a finite positive number"
        )
    min_cases = value.get("min_cases")
    if (
        isinstance(min_cases, bool)
        or not isinstance(min_cases, int)
        or min_cases <= 0
    ):
        raise RoutedPoolError(f"{label}.min_cases must be a positive integer")
    quality_weight = value.get("quality_weight")
    latency_weight = value.get("latency_weight")
    for field, weight in (
        ("quality_weight", quality_weight),
        ("latency_weight", latency_weight),
    ):
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or not 0 <= weight <= 1
        ):
            raise RoutedPoolError(
                f"{label}.{field} must be a finite number in [0, 1]"
            )
    weight_total = quality_weight + latency_weight
    effective_quality = quality_weight / weight_total if weight_total > 0 else 1
    effective_latency = latency_weight / weight_total if weight_total > 0 else 0
    normalized = {
        "enabled": True,
        "suite_ids": unique_ids,
        "max_age_hours": _canonical_number(max_age_hours),
        "min_cases": min_cases,
        "quality_weight": _canonical_number(effective_quality),
        "latency_weight": _canonical_number(effective_latency),
    }
    return normalized


def _normalize_composite_evidence(value: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize a composite evidence admission block.

    The block is admission metadata: it is canonicalized into the catalog
    declaration fingerprint, but the controlled selector fingerprints and
    mappings stay outside the server routing-policy fingerprint.
    """

    label = "routed_pool.composite_evidence"
    if not isinstance(value, Mapping):
        raise RoutedPoolError(f"{label} must be an object")
    if value.get("mode") != COMPOSITE_MODE:
        raise RoutedPoolError(f"{label}.mode must be {COMPOSITE_MODE!r}")
    auto = value.get("auto_selection")
    if not isinstance(auto, Mapping):
        raise RoutedPoolError(f"{label}.auto_selection must be an object")
    for field in ("selector", "policy_revision", "policy_fingerprint", "attested_member"):
        _nonempty(auto.get(field), f"{label}.auto_selection.{field}")
    if auto["selector"] != spec.get("selector"):
        raise RoutedPoolError(
            f"{label}.auto_selection.selector must equal the route selector "
            f"{spec.get('selector')!r}"
        )
    for field, expected in (
        ("gateway", POOL_GATEWAY),
        ("host", POOL_HOST),
        ("protocol", POOL_PROTOCOL),
    ):
        _nonempty(auto.get(field), f"{label}.auto_selection.{field}")
        if auto[field] != expected:
            raise RoutedPoolError(
                f"{label}.auto_selection.{field} must be {expected!r}"
            )
    if not _HEX_FINGERPRINT.match(auto["policy_fingerprint"]):
        raise RoutedPoolError(
            f"{label}.auto_selection.policy_fingerprint must be a 64-character hex string"
        )
    auto_receipt = auto.get("receipt")
    if not isinstance(auto_receipt, Mapping) or auto_receipt.get("verified") is not True:
        raise RoutedPoolError(f"{label}.auto_selection.receipt is not reviewed")
    _nonempty(auto_receipt.get("source"), f"{label}.auto_selection.receipt.source")
    _nonempty(auto_receipt.get("receipt_ref"), f"{label}.auto_selection.receipt.receipt_ref")
    _nonempty(auto_receipt.get("verified_on"), f"{label}.auto_selection.receipt.verified_on")

    members_by_attested = {
        str(member.get("attested_model")): member
        for member in spec.get("members", [])
        if isinstance(member, Mapping)
    }
    if auto["attested_member"] not in members_by_attested:
        raise RoutedPoolError(
            f"{label}.auto_selection.attested_member is not a declared member"
        )

    qualifications = value.get("controlled_qualification")
    if (
        isinstance(qualifications, (str, bytes))
        or not isinstance(qualifications, (list, tuple))
        or not qualifications
    ):
        raise RoutedPoolError(
            f"{label}.controlled_qualification must be a non-empty array"
        )
    seen: set[str] = set()
    canonical_qualifications: list[dict[str, Any]] = []
    for q in qualifications:
        if not isinstance(q, Mapping):
            raise RoutedPoolError(f"{label}.controlled_qualification entries must be objects")
        attested = _nonempty(q.get("attested_member"), f"{label}.controlled_qualification.attested_member")
        member = members_by_attested.get(attested)
        if member is None:
            raise RoutedPoolError(
                f"{label}.controlled_qualification attested_member is not a declared member"
            )
        if attested in seen:
            raise RoutedPoolError(
                f"{label}.controlled_qualification attested_member duplicates or collides"
            )
        seen.add(attested)
        for field in ("provider", "model_vendor", "model"):
            if q.get(field) != member.get(field):
                raise RoutedPoolError(
                    f"{label}.controlled_qualification.{field} does not match the declared member"
                )
        for field, expected in (
            ("gateway", POOL_GATEWAY),
            ("host", POOL_HOST),
            ("protocol", POOL_PROTOCOL),
        ):
            _nonempty(q.get(field), f"{label}.controlled_qualification.{field}")
            if q[field] != expected:
                raise RoutedPoolError(
                    f"{label}.controlled_qualification.{field} must be {expected!r}"
                )
        for field in ("selector", "policy_revision", "policy_fingerprint"):
            _nonempty(q.get(field), f"{label}.controlled_qualification.{field}")
        if not _HEX_FINGERPRINT.match(q["policy_fingerprint"]):
            raise RoutedPoolError(
                f"{label}.controlled_qualification.policy_fingerprint must be a 64-character hex string"
            )
        receipt = q.get("receipt")
        if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
            raise RoutedPoolError(
                f"{label}.controlled_qualification.receipt is not reviewed"
            )
        _nonempty(receipt.get("source"), f"{label}.controlled_qualification.receipt.source")
        _nonempty(receipt.get("receipt_ref"), f"{label}.controlled_qualification.receipt.receipt_ref")
        _nonempty(receipt.get("verified_on"), f"{label}.controlled_qualification.receipt.verified_on")
        canonical_qualifications.append({
            "attested_member": attested,
            "provider": q["provider"],
            "model_vendor": q["model_vendor"],
            "model": q["model"],
            "gateway": q["gateway"],
            "host": q["host"],
            "protocol": q["protocol"],
            "selector": q["selector"],
            "policy_revision": q["policy_revision"],
            "policy_fingerprint": q["policy_fingerprint"],
            "receipt": {
                "source": receipt["source"],
                "receipt_ref": receipt["receipt_ref"],
            },
        })
    if set(seen) != set(members_by_attested):
        raise RoutedPoolError(
            f"{label}.controlled_qualification must cover every declared member"
        )

    return {
        "mode": COMPOSITE_MODE,
        "auto_selection": {
            "selector": auto["selector"],
            "gateway": auto["gateway"],
            "host": auto["host"],
            "protocol": auto["protocol"],
            "policy_revision": auto["policy_revision"],
            "policy_fingerprint": auto["policy_fingerprint"],
            "attested_member": auto["attested_member"],
            "receipt": {
                "source": auto_receipt["source"],
                "receipt_ref": auto_receipt["receipt_ref"],
            },
        },
        "controlled_qualification": sorted(
            canonical_qualifications,
            key=lambda item: (str(item["provider"]), str(item["model"])),
        ),
    }


def _validate_caps(caps: Any, label: str = "routed_pool.expected_caps") -> None:
    """Require a mapping of nonsecret scalar cap data.

    Keys must be non-secret-shaped strings; leaves must be scalars
    (str/int/bool/None) so malformed data cannot crash hashing and a
    structured credential blob cannot hide inside the caps. Numeric leaves
    are integer-only within the JS safe range: the fingerprint is shared
    with a TypeScript peer that cannot represent decimals or integers
    outside +/-2**53-1. Nested objects and arrays of the same scalar leaves
    are allowed.
    """

    if not isinstance(caps, Mapping):
        raise RoutedPoolError(f"{label} must be an object")
    pending: list[Any] = [caps]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or not key:
                    raise RoutedPoolError(f"{label} keys must be non-empty strings")
                if _SECRET_FIELD.search(key):
                    raise RoutedPoolError(
                        f"{label} must not contain secret-shaped fields"
                    )
                pending.append(child)
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, str):
            if _SECRET_FIELD.search(value):
                raise RoutedPoolError(
                    f"{label} must not contain secret-shaped values"
                )
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, int):
            if abs(value) > _JS_SAFE_INTEGER_MAX:
                raise RoutedPoolError(
                    f"{label} values must be safe JSON integers"
                )
        else:
            raise RoutedPoolError(f"{label} values must be safe scalars")


def validate_pool_spec(route: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate a route's ``routed_pool`` declaration; return it on success.

    A pool route is only ever the approved OmniRoute identity: provider
    ``omniroute`` behind gateway ``omniroute-router``, host ``claude``, mode
    ``execute``, protocol ``anthropic-compatible``.

    Members are explicit canonical ``provider``/``model`` pairs plus the
    bare ``attested_model`` stream id the router can actually attest — a
    bare SSE id is never a substitute for the canonical pair. Duplicate
    canonical pairs are rejected, and duplicate attested ids are rejected
    even across providers because a bare SSE id cannot distinguish them
    (collision = unverifiable attestation).
    """

    if route.get("model_vendor") != "routed-pool":
        raise RoutedPoolError(
            "routed_pool routes require model_vendor 'routed-pool'"
        )
    for field, expected in (
        ("provider", POOL_PROVIDER),
        ("gateway", POOL_GATEWAY),
        ("host", POOL_HOST),
        ("mode", POOL_MODE),
        ("protocol", POOL_PROTOCOL),
    ):
        if route.get(field) != expected:
            raise RoutedPoolError(
                f"routed_pool routes require {field} '{expected}'"
            )
    if route.get("resolved_model") is not None:
        raise RoutedPoolError(
            "routed_pool selector must not claim a resolved_model"
        )
    spec = route.get("routed_pool")
    if not isinstance(spec, Mapping):
        raise RoutedPoolError("routed_pool route lacks a pool declaration")
    if spec.get("selector") != route.get("model"):
        raise RoutedPoolError("routed_pool selector must equal the route model")
    if spec.get("host") != route.get("host"):
        raise RoutedPoolError("routed_pool host must equal the route host")
    if spec.get("protocol") != route.get("protocol"):
        raise RoutedPoolError("routed_pool protocol must equal the route protocol")
    _nonempty(spec.get("policy_revision"), "routed_pool.policy_revision")
    # Reviewed provenance is required, not optional: it is the router audit
    # reference that attributes the member evaluations and binds the
    # operator snapshot's evidence.source.
    _nonempty(spec.get("evidence_provenance"), "routed_pool.evidence_provenance")
    # Pool eligibility requires a reviewed fail-closed context-fit policy,
    # echoed exactly by the operator snapshot. Missing or false is malformed.
    if spec.get("context_fit_fail_closed") is not True:
        raise RoutedPoolError(
            "routed_pool.context_fit_fail_closed must be reviewed and true"
        )
    caps = spec.get("expected_caps")
    if caps is not None:
        _validate_caps(caps)
    # Optional evalRouting contract: when declared it is normalized and
    # bound into the canonical fingerprint and snapshot match; when absent
    # the legacy fingerprint and snapshot semantics are unchanged.
    if spec.get("eval_routing") is not None:
        _normalize_eval_routing(spec["eval_routing"])
    # Optional composite evidence admission block: when declared it is
    # canonicalized into the catalog declaration fingerprint, but the
    # controlled selector fingerprints and mappings stay outside the
    # server routing-policy fingerprint and live snapshot contract.
    if spec.get("composite_evidence") is not None:
        _normalize_composite_evidence(spec["composite_evidence"], spec)
    members = spec.get("members")
    if not isinstance(members, list) or not members:
        raise RoutedPoolError("routed_pool requires a non-empty members array")
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(members):
        label = f"routed_pool.members[{index}]"
        if not isinstance(item, Mapping):
            raise RoutedPoolError(f"{label} must be an object")
        provider = _nonempty(item.get("provider"), f"{label}.provider")
        _nonempty(item.get("model_vendor"), f"{label}.model_vendor")
        model = _nonempty(item.get("model"), f"{label}.model")
        attested = _nonempty(item.get("attested_model"), f"{label}.attested_model")
        if attested in seen_ids:
            raise RoutedPoolError(
                f"{label}.attested_model duplicates or collides with another member"
            )
        seen_ids.add(attested)
        if (provider, model) in seen_pairs:
            raise RoutedPoolError(f"{label} is a duplicate member")
        seen_pairs.add((provider, model))
        evaluation = item.get("evaluation")
        if not isinstance(evaluation, Mapping) or evaluation.get("verified") is not True:
            raise RoutedPoolError(f"{label}.evaluation is not reviewed")
        _nonempty(evaluation.get("verified_on"), f"{label}.evaluation.verified_on")
        _nonempty(evaluation.get("source"), f"{label}.evaluation.source")
        identity = evaluation.get("identity")
        if not isinstance(identity, Mapping):
            raise RoutedPoolError(f"{label}.evaluation.identity is required")
        for field in ("gateway", "selector", "provider", "model", "host", "protocol", "policy_revision"):
            _nonempty(identity.get(field), f"{label}.evaluation.identity.{field}")
        scores = evaluation.get("task_scores")
        if not isinstance(scores, Mapping) or not scores or not all(
            isinstance(band, str) and band
            and not isinstance(score, bool)
            and isinstance(score, int)
            and 0 <= score <= 100
            for band, score in scores.items()
        ):
            raise RoutedPoolError(f"{label}.evaluation.task_scores is invalid")
        acceptance = evaluation.get("acceptance_rate")
        if (
            isinstance(acceptance, bool)
            or not isinstance(acceptance, (int, float))
            or not 0 < acceptance <= 1
        ):
            raise RoutedPoolError(f"{label}.evaluation.acceptance_rate is invalid")
        capabilities = item.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise RoutedPoolError(f"{label}.capabilities is required")
        for field in ("supported", "connectors", "privacy_classes"):
            values = capabilities.get(field)
            if not isinstance(values, list) or not all(
                isinstance(entry, str) and entry for entry in values
            ):
                raise RoutedPoolError(f"{label}.capabilities.{field} is invalid")
        behavioral = item.get("behavioral_capabilities", {})
        if not isinstance(behavioral, Mapping):
            raise RoutedPoolError(f"{label}.behavioral_capabilities is invalid")
        for name, record in behavioral.items():
            if not isinstance(record, Mapping):
                raise RoutedPoolError(f"{label}.behavioral_capabilities.{name} is invalid")
            score = record.get("score")
            types = record.get("evidence_types")
            if (
                isinstance(score, bool)
                or not isinstance(score, int)
                or not 0 <= score <= 100
                or not isinstance(types, list)
                or "local-evaluation" not in types
            ):
                raise RoutedPoolError(
                    f"{label}.behavioral_capabilities.{name} lacks local evidence"
                )
        # Context/output limits are runtime admission gates (approximate
        # evidence, not a tokenizer guarantee). When present they must be
        # positive integers; a missing value fails closed at recommend time.
        for field in ("approx_context_tokens", "max_output_tokens"):
            if item.get(field) is not None:
                _positive(item.get(field), f"{label}.{field}")
    return spec


def member_attested_set(spec: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(member["attested_model"] for member in spec["members"])


def member_member_set(spec: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """Canonical member identities: exact ``(provider, model)`` pairs."""

    return frozenset(
        (member["provider"], member["model"]) for member in spec["members"]
    )


def routed_contracts_from_models(
    config: Mapping[str, Any],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Extract configured routing policy contracts from the model allowlist.

    Returns ``{(provider, host, mode, selector): contract}`` where contract
    carries ``allowed_upstream_models`` (frozenset of bare attested ids —
    the router's stream contract), ``policy_revision``, and
    ``policy_fingerprint``. Only entries that are fully bound are returned:
    the selector must be allowlisted in the route's ``models``, and the
    contract must declare verified ``settings_precedence``, a non-empty
    pinned ``policy_revision``, and a non-empty ``policy_fingerprint``.
    Anything less produces no entry so a catalog pool route fails closed on
    ``routed-runtime-contract-missing``. A model_configs entry without this
    admission proof is transport configuration only, never pool evidence.
    """

    contracts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return contracts
    for provider, item in providers.items():
        if not isinstance(provider, str) or not isinstance(item, Mapping):
            continue
        routes = item.get("routes")
        if not isinstance(routes, Mapping):
            continue
        for mode, hosts in routes.items():
            if not isinstance(hosts, Mapping):
                continue
            for host, route in hosts.items():
                if not isinstance(route, Mapping):
                    continue
                allowlisted = route.get("models")
                if (
                    isinstance(allowlisted, (str, bytes))
                    or not isinstance(allowlisted, (list, tuple))
                ):
                    continue
                allowlisted = {
                    entry for entry in allowlisted
                    if isinstance(entry, str) and entry
                }
                model_configs = route.get("model_configs")
                if not isinstance(model_configs, Mapping):
                    continue
                for model, model_config in model_configs.items():
                    if not isinstance(model_config, Mapping):
                        continue
                    if model not in allowlisted:
                        continue
                    policy = model_config.get("routing_policy_contract")
                    if not isinstance(policy, Mapping):
                        continue
                    if policy.get("requested_selector") != model:
                        continue
                    if policy.get("settings_precedence") != (
                        _VERIFIED_SETTINGS_PRECEDENCE
                    ):
                        continue
                    revision = policy.get("policy_revision")
                    fingerprint = policy.get("policy_fingerprint")
                    if (
                        not isinstance(revision, str)
                        or not revision
                        or not isinstance(fingerprint, str)
                        or not fingerprint
                    ):
                        continue
                    upstream = policy.get("allowed_upstream_models")
                    if (
                        isinstance(upstream, (str, bytes))
                        or not isinstance(upstream, (list, tuple))
                        or not upstream
                        or not all(
                            isinstance(entry, str) and entry.strip()
                            for entry in upstream
                        )
                    ):
                        continue
                    allowed = frozenset(entry.strip() for entry in upstream)
                    if len(allowed) != len(upstream):
                        continue
                    contracts[(provider, host, mode, model)] = {
                        "allowed_upstream_models": allowed,
                        "policy_revision": revision,
                        "policy_fingerprint": fingerprint,
                        "settings_precedence": _VERIFIED_SETTINGS_PRECEDENCE,
                    }
    return contracts


def match_snapshot(
    snapshots: Sequence[Mapping[str, Any]] | None,
    route: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    now_utc: datetime,
    today: date | None = None,
    evidence_days: int | None = None,
) -> str | None:
    """Return a failure reason, or ``None`` when a snapshot binds the pool.

    The snapshot is operator-supplied receipt data: this comparison proves
    consistency and freshness of what was recorded, never live server
    authenticity. More than one candidate for the same provider+selector is
    ambiguous — admission never picks an arbitrary first entry.
    """

    candidates = [
        item for item in (snapshots or [])
        if isinstance(item, Mapping)
        and item.get("provider") == route.get("provider")
        and item.get("selector") == spec.get("selector")
    ]
    if not candidates:
        if any(
            isinstance(item, Mapping)
            and item.get("provider") == route.get("provider")
            for item in (snapshots or [])
        ):
            return "routed-policy-snapshot-mismatch"
        return "routed-policy-snapshot-absent"
    if len(candidates) > 1:
        return "routed-policy-snapshot-ambiguous"
    snapshot = candidates[0]
    observed_raw = snapshot.get("observed_at")
    if not isinstance(observed_raw, str) or not observed_raw:
        return "routed-policy-snapshot-invalid"
    try:
        observed = datetime.fromisoformat(observed_raw)
    except ValueError:
        return "routed-policy-snapshot-invalid"
    if observed.tzinfo is None:
        return "routed-policy-snapshot-invalid"
    observed = observed.astimezone(timezone.utc)
    if observed > now_utc:
        return "routed-policy-snapshot-future"
    if now_utc - observed > timedelta(seconds=SNAPSHOT_MAX_AGE_SECONDS):
        return "routed-policy-snapshot-stale"
    if snapshot.get("policy_revision") != spec.get("policy_revision"):
        return "routed-policy-snapshot-mismatch"
    # The server routing-policy fingerprint binds only the live router
    # policy; the optional composite evidence block is not mixed into it.
    if snapshot.get("policy_fingerprint") != pool_policy_fingerprint(spec):
        return "routed-policy-snapshot-mismatch"
    upstream = snapshot.get("allowed_upstream_models")
    if (
        isinstance(upstream, (str, bytes))
        or not isinstance(upstream, (list, tuple))
        or not all(isinstance(entry, str) for entry in upstream)
        or frozenset(upstream) != member_attested_set(spec)
    ):
        return "routed-policy-snapshot-mismatch"
    # The snapshot also binds the exact canonical member identities, not
    # just the bare upstream ids the router streams.
    bound_members = snapshot.get("members")
    fields = ("provider", "model", "model_vendor", "attested_model")
    if (
        not isinstance(bound_members, (list, tuple))
        or len(bound_members) != len(spec["members"])
        or not all(
            isinstance(entry, Mapping)
            and all(isinstance(entry.get(field), str) and entry[field] for field in fields)
            for entry in bound_members
        )
        or {tuple(entry[field] for field in fields) for entry in bound_members}
        != {tuple(entry[field] for field in fields) for entry in spec["members"]}
    ):
        return "routed-policy-snapshot-mismatch"
    if snapshot.get("caps") != spec.get("expected_caps"):
        return "routed-policy-snapshot-mismatch"
    if snapshot.get("context_fit_fail_closed") != spec.get(
        "context_fit_fail_closed"
    ):
        return "routed-policy-snapshot-mismatch"
    if spec.get("eval_routing") is not None:
        try:
            observed_eval = _normalize_eval_routing(snapshot.get("eval_routing"))
        except RoutedPoolError:
            return "routed-policy-snapshot-mismatch"
        if observed_eval != _normalize_eval_routing(spec["eval_routing"]):
            return "routed-policy-snapshot-mismatch"
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, Mapping):
        return "routed-policy-snapshot-mismatch"
    if evidence.get("source") != spec.get("evidence_provenance"):
        return "routed-policy-snapshot-mismatch"
    if not isinstance(evidence.get("receipt_ref"), str) or not evidence["receipt_ref"]:
        return "routed-policy-snapshot-mismatch"
    if spec.get("composite_evidence") is not None:
        try:
            _normalize_composite_evidence(spec["composite_evidence"], spec)
        except RoutedPoolError:
            return "routed-composite-evidence-malformed"
        auto = spec["composite_evidence"]["auto_selection"]
        if (
            snapshot.get("selector") != auto.get("selector")
            or snapshot.get("policy_revision") != auto.get("policy_revision")
            or snapshot.get("policy_fingerprint") != auto.get("policy_fingerprint")
            or auto.get("attested_member") not in (upstream or [])
        ):
            return "routed-composite-auto-proof-mismatch"
        auto_receipt = auto.get("receipt", {})
        if (
            not isinstance(auto_receipt, Mapping)
            or auto_receipt.get("verified") is not True
            or not isinstance(auto_receipt.get("source"), str)
            or not auto_receipt["source"]
            or not isinstance(auto_receipt.get("receipt_ref"), str)
            or not auto_receipt["receipt_ref"]
        ):
            return "routed-composite-auto-proof-mismatch"
        if today is not None and evidence_days is not None:
            if not _fresh_on(auto_receipt, "verified_on", today, evidence_days):
                return "routed-composite-auto-receipt-stale"
    return None
