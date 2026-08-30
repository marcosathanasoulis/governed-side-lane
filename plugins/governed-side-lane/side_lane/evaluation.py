"""Deterministic, offline evidence aggregation for side-lane routing.

This module consumes reviewed fixture/result records.  It never invokes a
provider, inspects credentials, or discovers account usage.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import median
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


class EvaluationError(ValueError):
    """An evidence record is incomplete or internally inconsistent."""


def _date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EvaluationError(f"{label} must be an ISO-8601 date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationError(f"{label} must be an ISO-8601 date") from exc
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"{label} must be a non-empty string")
    return value


def validate_run(record: Mapping[str, Any]) -> None:
    for field in ("route_id", "task_id", "task_band", "fixture_version", "rubric_version"):
        _text(record.get(field), field)
    _date(record.get("observed_on"), "observed_on")
    if not isinstance(record.get("accepted"), bool):
        raise EvaluationError("accepted must be boolean")
    score = record.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise EvaluationError("score must be between 0 and 100")
    for field in ("duration_ms", "retries"):
        value = record.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvaluationError(f"{field} must be a non-negative integer")
    if not isinstance(record.get("refused", False), bool):
        raise EvaluationError("refused must be boolean")
    cost = record.get("cost")
    if cost is not None:
        if not isinstance(cost, Mapping):
            raise EvaluationError("cost must be an object")
        value = cost.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise EvaluationError("cost.value must be non-negative")
        _text(cost.get("unit"), "cost.unit")
        _text(cost.get("basis"), "cost.basis")


def aggregate_runs(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate normalized runs by exact route, task band, and cost unit."""

    if not records:
        raise EvaluationError("at least one evaluation run is required")
    groups: dict[tuple[str, str, str | None, str | None], list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if not isinstance(record, Mapping):
            raise EvaluationError("every evaluation run must be an object")
        validate_run(record)
        cost = record.get("cost")
        unit = str(cost["unit"]) if isinstance(cost, Mapping) else None
        basis = str(cost["basis"]) if isinstance(cost, Mapping) else None
        groups[(str(record["route_id"]), str(record["task_band"]), unit, basis)].append(record)

    output: list[dict[str, Any]] = []
    for (route_id, task_band, unit, basis), runs in sorted(groups.items()):
        accepted = sum(bool(item["accepted"]) for item in runs)
        refused = sum(bool(item.get("refused", False)) for item in runs)
        costs = [float(item["cost"]["value"]) for item in runs if isinstance(item.get("cost"), Mapping)]
        acceptance_rate = accepted / len(runs)
        median_cost = median(costs) if costs else None
        output.append({
            "route_id": route_id,
            "task_band": task_band,
            "fixture_versions": sorted({str(item["fixture_version"]) for item in runs}),
            "rubric_versions": sorted({str(item["rubric_version"]) for item in runs}),
            "sample_count": len(runs),
            "accepted_count": accepted,
            "acceptance_rate": round(acceptance_rate, 6),
            "mean_score": round(sum(float(item["score"]) for item in runs) / len(runs), 4),
            "refusal_rate": round(refused / len(runs), 6),
            "median_duration_ms": median(int(item.get("duration_ms", 0)) for item in runs),
            "median_retries": median(int(item.get("retries", 0)) for item in runs),
            "cost": None if median_cost is None else {
                "median_value": median_cost,
                "unit": unit,
                "basis": basis,
                "expected_per_accepted_result": (
                    None if acceptance_rate == 0 else round(median_cost / acceptance_rate, 8)
                ),
            },
            "observed_from": min(str(item["observed_on"]) for item in runs),
            "observed_through": max(str(item["observed_on"]) for item in runs),
        })
    return output


def validate_community_signal(record: Mapping[str, Any]) -> None:
    for field in ("report_id", "source_url", "model", "task_capability", "host_harness"):
        _text(record.get(field), field)
    parsed = urlparse(str(record["source_url"]))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EvaluationError("source_url must be an HTTP(S) URL")
    _date(record.get("observed_on"), "observed_on")
    if record.get("direction") not in {"supports", "contradicts"}:
        raise EvaluationError("direction must be supports or contradicts")
    if record.get("affiliation") not in {"independent", "vendor", "unknown"}:
        raise EvaluationError("affiliation must be independent, vendor, or unknown")
    if not isinstance(record.get("methodology_disclosed"), bool):
        raise EvaluationError("methodology_disclosed must be boolean")


def summarize_community_signals(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate and summarize reviewed reports without scraping or voting."""

    unique: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise EvaluationError("every community signal must be an object")
        validate_community_signal(record)
        report_id = str(record["report_id"])
        if report_id in unique:
            raise EvaluationError(f"duplicate community report_id: {report_id}")
        unique[report_id] = record

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in unique.values():
        groups[(str(record["model"]), str(record["task_capability"]))].append(record)

    output: list[dict[str, Any]] = []
    for (model, capability), signals in sorted(groups.items()):
        independent = [item for item in signals if item["affiliation"] == "independent"]
        attributable = [item for item in independent if item["methodology_disclosed"]]
        supports = sum(item["direction"] == "supports" for item in independent)
        contradicts = sum(item["direction"] == "contradicts" for item in independent)
        attributable_supports = sum(item["direction"] == "supports" for item in attributable)
        domains = {urlparse(str(item["source_url"])).netloc.lower() for item in attributable}
        ratio = attributable_supports / len(attributable) if attributable else 0.0
        if len(attributable) >= 8 and len(domains) >= 2 and ratio >= 0.75:
            confidence = "high"
        elif len(attributable) >= 4 and len(domains) >= 2 and ratio >= 2 / 3:
            confidence = "moderate"
        elif supports and contradicts:
            confidence = "mixed"
        else:
            confidence = "low"
        output.append({
            "model": model,
            "task_capability": capability,
            "independent_reports": len(independent),
            "methodology_disclosed_reports": len(attributable),
            "vendor_affiliated_reports": sum(item["affiliation"] == "vendor" for item in signals),
            "unknown_affiliation_reports": sum(item["affiliation"] == "unknown" for item in signals),
            "supporting_reports": supports,
            "contrary_reports": contradicts,
            "source_domains": sorted(domains),
            "host_harnesses": sorted({str(item["host_harness"]) for item in signals}),
            "confidence": confidence,
            "activation_authority": False,
            "use": "evaluation-prior",
            "observed_from": min(str(item["observed_on"]) for item in signals),
            "observed_through": max(str(item["observed_on"]) for item in signals),
        })
    return output
