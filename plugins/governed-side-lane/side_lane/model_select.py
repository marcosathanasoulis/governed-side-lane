"""Public, host-agnostic model-select scorer.

This is a pure scorer over a frozen, versioned public snapshot of derived
model-selection knowledge (``config/model-select-snapshot.json``). It never
learns or writes back; a new snapshot ships only with a new published
package version.

Inputs:
  - the user's available routes: native hosts present (detected) plus
    OpenRouter routes if a key is present (presence only, never the value),
  - a task profile (task family, required capabilities, quality floor,
    token forecast),
  - declared host usage (included-oauth | extra-usage | unknown --
    user-declared; this module never detects it),
  - optional live price rows (fetched only by an injectable transport,
    never automatically and never in tests).

Output is a ranked list of candidates with an expected cost per success
(or "unknown" when a needed figure cannot be derived -- unknown is never
treated as free), the basis for each number, exclusions with reasons, and
the snapshot_id the ranking was produced from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from side_lane import credentials as _credentials
from side_lane import hosts as _hosts

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_PATH = PACKAGE_ROOT / "config" / "model-select-snapshot.json"
SNAPSHOT_PATH_ENV = "SIDE_LANE_MODEL_SELECT_SNAPSHOT_PATH"

OPENROUTER_CREDENTIAL_SERVICE = "governed-side-lane-openrouter"

SUPPORTED_USAGE_STATES = frozenset({"included-oauth", "extra-usage", "unknown"})

# Re-derived from GCF common/utils/model_selection/costs.py at the pinned
# selector commit (ab30e5f274f2c91061ed028eb659600553a74f14): a conservative,
# per-provider fraction of a comparable metered list price used as the
# planning value of consuming included subscription capacity. These are
# planning priors, not provider-published subscription token prices, and are
# numeric constants only -- no GCF source code is reproduced here.
INCLUDED_PRICE_FRACTIONS = {"claude": Decimal("0.35"), "codex": Decimal("0.50")}
MIN_INCLUDED_PRICE_FRACTION = Decimal("0.05")


class ModelSelectError(ValueError):
    """A malformed snapshot, profile, or ranking request."""


# --------------------------------------------------------------------------
# Snapshot loading
# --------------------------------------------------------------------------


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        override = os.environ.get(SNAPSHOT_PATH_ENV, "").strip()
        path = Path(override).expanduser() if override else DEFAULT_SNAPSHOT_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelSelectError(f"cannot load model-select snapshot: {exc}") from exc
    validate_snapshot(data)
    return data


def validate_snapshot(data: Any) -> None:
    if not isinstance(data, Mapping):
        raise ModelSelectError("snapshot must be an object")
    if data.get("schema_version") != 1:
        raise ModelSelectError("unsupported snapshot schema_version")
    if not isinstance(data.get("snapshot_id"), str) or not data["snapshot_id"]:
        raise ModelSelectError("snapshot requires snapshot_id")
    families = data.get("task_families")
    if not isinstance(families, Mapping):
        raise ModelSelectError("snapshot requires task_families")
    for family_id, family in families.items():
        if not isinstance(family, Mapping) or not isinstance(family.get("models"), Mapping):
            raise ModelSelectError(f"task family {family_id!r} is malformed")


def _family_prior(snapshot: Mapping[str, Any], task_family: str, model_id: str) -> dict[str, Any] | None:
    family = snapshot["task_families"].get(task_family)
    if not family:
        return None
    return family["models"].get(model_id)


# --------------------------------------------------------------------------
# Host / route discovery (presence only; never a provider call)
# --------------------------------------------------------------------------


def detect_available_hosts(
    *,
    env: Mapping[str, str] | None = None,
    which: _hosts.Which | None = None,
) -> dict[str, bool]:
    """Report which native hosts (claude, codex) are present. Presence only."""

    kwargs: dict[str, Any] = {}
    if env is not None:
        kwargs["env"] = env
    if which is not None:
        kwargs["which"] = which
    return {
        host: _hosts.resolve_host_executable(host, **kwargs) is not None
        for host in ("claude", "codex")
    }


def openrouter_present(system: str | None = None) -> bool:
    """Report whether an OpenRouter credential is present. Presence only."""

    return _credentials.credential_present(OPENROUTER_CREDENTIAL_SERVICE, system=system)


# --------------------------------------------------------------------------
# Task profile / ranking
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskProfile:
    task_family: str
    required_capabilities: frozenset[str] = frozenset()
    quality_floor: str = "low"  # "low" | "medium" | "high"
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_family, str) or not self.task_family:
            raise ModelSelectError("task_family is required")
        if self.quality_floor not in {"low", "medium", "high"}:
            raise ModelSelectError("quality_floor must be low, medium, or high")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ModelSelectError("token counts must be non-negative")


_QUALITY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Candidate:
    route_id: str
    model_id: str
    host: str  # "claude" | "codex" | "openrouter"
    provider: str  # "native" | "openrouter"


@dataclass(frozen=True)
class RankedCandidate:
    route_id: str
    model_id: str
    host: str
    expected_cost_per_success_usd: Decimal | None
    basis: str  # "snapshot_prior+live_price" | "snapshot_prior+opportunity_cost" | "unknown"
    quality_tier: str | None


@dataclass(frozen=True)
class RankingResult:
    snapshot_id: str
    ranked: list[RankedCandidate]
    excluded: list[tuple[str, str]]


def native_candidates(available_hosts: Mapping[str, bool]) -> list[Candidate]:
    candidates = []
    if available_hosts.get("claude"):
        for model_id in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5-1"):
            candidates.append(Candidate(f"claude:{model_id}", model_id, "claude", "native"))
    if available_hosts.get("codex"):
        for model_id in (
            "gpt-6-sol", "gpt-6-astra", "gpt-6-luna",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
        ):
            candidates.append(Candidate(f"codex:{model_id}", model_id, "codex", "native"))
    return candidates


def openrouter_candidates(snapshot: Mapping[str, Any], task_family: str) -> list[Candidate]:
    family = snapshot["task_families"].get(task_family, {})
    candidates = []
    for model_id in family.get("models", {}):
        if "/" in model_id:  # OpenRouter-style slug, e.g. "openai/gpt-4o-mini"
            candidates.append(Candidate(f"openrouter:{model_id}", model_id, "openrouter", "openrouter"))
    return candidates


def _list_equivalent_usd(
    price_rows: Mapping[str, Mapping[str, Any]] | None, model_id: str, tokens: TaskProfile
) -> Decimal | None:
    if not price_rows or model_id not in price_rows:
        return None
    row = price_rows[model_id]
    try:
        input_price = Decimal(str(row["input_usd_per_million"]))
        output_price = Decimal(str(row["output_usd_per_million"]))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    if input_price < 0 or output_price < 0:
        return None
    return (input_price * tokens.input_tokens + output_price * tokens.output_tokens) / Decimal(1_000_000)


def rank_candidates(
    *,
    snapshot: Mapping[str, Any],
    available_hosts: Mapping[str, bool],
    openrouter_available: bool,
    profile: TaskProfile,
    host_usage: Mapping[str, str] | None = None,
    price_rows: Mapping[str, Mapping[str, Any]] | None = None,
) -> RankingResult:
    """Rank every reachable candidate by expected cost per success.

    ``host_usage`` declares, per native host, "included-oauth",
    "extra-usage", or "unknown" (default "unknown" per host, never auto
    detected). Extra-usage hosts are priced as metered (excluded here if no
    live price is supplied for them, since this package has no per-token
    native-host price list) or excluded if no price is available; included
    hosts get a nonzero opportunity cost once a comparable live reference
    price is known, else their cost stays unknown.
    """

    host_usage = dict(host_usage or {})
    for host, state in host_usage.items():
        if state not in SUPPORTED_USAGE_STATES:
            raise ModelSelectError(f"unsupported host_cost_state for {host}: {state!r}")

    candidates = native_candidates(available_hosts)
    if openrouter_available:
        candidates += openrouter_candidates(snapshot, profile.task_family)

    ranked: list[RankedCandidate] = []
    excluded: list[tuple[str, str]] = []

    for candidate in candidates:
        prior = _family_prior(snapshot, profile.task_family, candidate.model_id)
        capabilities = (prior or {}).get("capabilities", {})
        quality_tier = (prior or {}).get("quality_tier")

        missing_capabilities = {
            cap for cap in profile.required_capabilities
            if capabilities.get(cap) is not True
        }
        if missing_capabilities:
            excluded.append((candidate.route_id, f"missing_capabilities:{','.join(sorted(missing_capabilities))}"))
            continue
        if quality_tier is not None and _QUALITY_RANK.get(quality_tier, 0) < _QUALITY_RANK[profile.quality_floor]:
            excluded.append((candidate.route_id, "below_quality_floor"))
            continue

        if candidate.provider == "openrouter":
            list_equivalent = _list_equivalent_usd(price_rows, candidate.model_id, profile)
            if list_equivalent is None:
                ranked.append(
                    RankedCandidate(candidate.route_id, candidate.model_id, candidate.host, None, "unknown", quality_tier)
                )
            else:
                ranked.append(
                    RankedCandidate(
                        candidate.route_id, candidate.model_id, candidate.host,
                        list_equivalent, "live_price", quality_tier,
                    )
                )
            continue

        # Native host candidate.
        usage_state = host_usage.get(candidate.host, "unknown")
        if usage_state == "extra-usage":
            list_equivalent = _list_equivalent_usd(price_rows, candidate.model_id, profile)
            if list_equivalent is None:
                excluded.append((candidate.route_id, "extra_usage_unknown_metered_price"))
                continue
            ranked.append(
                RankedCandidate(candidate.route_id, candidate.model_id, candidate.host, list_equivalent, "live_price", quality_tier)
            )
            continue

        # included-oauth or unknown: price as included capacity. Unknown
        # usage defaults to treating the route as included for the user's
        # own host (per the public package's documented default), but the
        # opportunity cost still requires a comparable reference price;
        # without one the cost stays unknown rather than free.
        reference = _list_equivalent_usd(price_rows, candidate.model_id, profile)
        if reference is None:
            ranked.append(
                RankedCandidate(candidate.route_id, candidate.model_id, candidate.host, None, "unknown", quality_tier)
            )
            continue
        fraction = INCLUDED_PRICE_FRACTIONS.get(candidate.host, Decimal(1))
        opportunity = max(MIN_INCLUDED_PRICE_FRACTION * reference, fraction * reference)
        ranked.append(
            RankedCandidate(
                candidate.route_id, candidate.model_id, candidate.host,
                opportunity, "snapshot_prior+opportunity_cost", quality_tier,
            )
        )

    def _sort_key(item: RankedCandidate) -> tuple[int, Decimal, str]:
        if item.expected_cost_per_success_usd is None:
            return (1, Decimal(0), item.route_id)
        return (0, item.expected_cost_per_success_usd, item.route_id)

    ranked.sort(key=_sort_key)
    return RankingResult(snapshot_id=str(snapshot["snapshot_id"]), ranked=ranked, excluded=excluded)


def ranking_to_json(result: RankingResult) -> dict[str, Any]:
    return {
        "snapshot_id": result.snapshot_id,
        "ranked": [
            {
                "route_id": item.route_id,
                "model_id": item.model_id,
                "host": item.host,
                "expected_cost_per_success_usd": (
                    None if item.expected_cost_per_success_usd is None else str(item.expected_cost_per_success_usd)
                ),
                "basis": item.basis,
                "quality_tier": item.quality_tier,
            }
            for item in result.ranked
        ],
        "excluded": [{"route_id": route_id, "reason": reason} for route_id, reason in result.excluded],
    }


# --------------------------------------------------------------------------
# Optional Jev advisory (OpenRouter-judge second opinion), user-paid,
# explicit consent only, deterministic recheck.
# --------------------------------------------------------------------------


Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def jev_request(result: RankingResult, profile: TaskProfile) -> dict[str, Any]:
    """Build a de-identified advisory payload: model ids and task shape only.

    Never includes the user's task text unless the user explicitly opts in
    by adding it to the returned payload themselves before sending it.
    """

    return {
        "task_family": profile.task_family,
        "required_capabilities": sorted(profile.required_capabilities),
        "quality_floor": profile.quality_floor,
        "candidates": [
            {"route_id": item.route_id, "model_id": item.model_id, "host": item.host}
            for item in result.ranked
        ],
    }


def call_jev(
    payload: Mapping[str, Any],
    *,
    transport: Transport,
    consent: bool,
) -> Mapping[str, Any]:
    """Call the OpenRouter Jev judge through an injectable transport.

    Requires explicit ``consent=True`` (the user is paying for this call);
    never invoked implicitly, and never invoked by any test in this package.
    """

    if not consent:
        raise ModelSelectError("Jev advisory call requires explicit consent=True")
    return transport(payload)


def recheck_jev_choice(
    choice: str,
    result: RankingResult,
    *,
    tolerance_fraction: Decimal = Decimal("0.05"),
    tolerance_floor_usd: Decimal = Decimal("1"),
) -> tuple[str, str]:
    """Accept Jev's pick only if eligible and within tolerance of the best.

    Returns ``(accepted_route_id, reason)``. Mirrors the GCF selector's
    near-tie admission band (the smaller of a flat floor or a fraction of
    the minimum cost) so a judge's pick can only displace the scorer's own
    best pick when they are practically indistinguishable on cost.
    """

    eligible = {item.route_id: item for item in result.ranked}
    if choice not in eligible:
        best = result.ranked[0] if result.ranked else None
        return (best.route_id if best else "", "jev_choice_not_eligible") if best else ("", "no_candidates")
    if not result.ranked:
        return "", "no_candidates"
    best = result.ranked[0]
    if best.expected_cost_per_success_usd is None:
        # Nothing has a known cost; accept Jev's eligible pick outright.
        return choice, "no_known_cost_baseline"
    chosen = eligible[choice]
    if chosen.expected_cost_per_success_usd is None:
        return best.route_id, "jev_choice_unknown_cost"
    tolerance = min(tolerance_floor_usd, best.expected_cost_per_success_usd * tolerance_fraction)
    if chosen.expected_cost_per_success_usd <= best.expected_cost_per_success_usd + tolerance:
        return choice, "within_tolerance_of_best"
    return best.route_id, "outside_tolerance_kept_scorer_pick"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _profile_from_json(data: Mapping[str, Any]) -> TaskProfile:
    return TaskProfile(
        task_family=data["task_family"],
        required_capabilities=frozenset(data.get("required_capabilities", [])),
        quality_floor=data.get("quality_floor", "low"),
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m side_lane.model_select")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--prices", type=Path, default=None)
    parser.add_argument("--jev-consent", action="store_true")
    parser.add_argument("--host-usage", type=Path, default=None, help="JSON map of host -> declared usage state")
    args = parser.parse_args(argv)

    snapshot = load_snapshot()
    profile_data = json.loads(args.profile.read_text(encoding="utf-8"))
    profile = _profile_from_json(profile_data)

    price_rows = None
    if args.prices is not None:
        price_rows = json.loads(args.prices.read_text(encoding="utf-8"))

    host_usage = None
    if args.host_usage is not None:
        host_usage = json.loads(args.host_usage.read_text(encoding="utf-8"))

    available_hosts = detect_available_hosts()
    openrouter_available = openrouter_present()

    result = rank_candidates(
        snapshot=snapshot,
        available_hosts=available_hosts,
        openrouter_available=openrouter_available,
        profile=profile,
        host_usage=host_usage,
        price_rows=price_rows,
    )
    output = ranking_to_json(result)
    output["jev_consent_requested"] = bool(args.jev_consent)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
