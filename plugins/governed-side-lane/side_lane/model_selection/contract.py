"""Shared request/result types for model selection (Prompt it and Side Lane).

Pure data and validation: no I/O, no credentials. A *selection* is a decision;
an :class:`ExecutionReceipt` records what actually ran. They are separate types
on purpose: a selection is never proof of execution, and a successful
authentication is never proof of included billing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

CONTRACT_VERSION = "model-selection/v1"
#: An OpenRouter model id as the router serves it: ``vendor/name``. Auto's own slugs are never a served model.
#: Kept identical to the execution adapter's contract (``adapters.claude.is_openrouter_model_id``; a parity test
#: enforces it): lowercase vendor slug, ``/``, model slug, at most 128 characters, no ``.``/``..`` path segment.
OPENROUTER_MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*/[A-Za-z0-9._:-]+")
OPENROUTER_MODEL_ID_MAX_CHARS = 128
AUTO_MODELS = ("openrouter/auto", "openrouter/auto-beta")


def is_served_model_id(model: object) -> bool:
    """Whether ``model`` satisfies the OpenRouter model-id contract shared with the execution adapter."""
    if not isinstance(model, str) or len(model) > OPENROUTER_MODEL_ID_MAX_CHARS:
        return False
    if OPENROUTER_MODEL_ID_RE.fullmatch(model) is None:
        return False
    return not any(part in {".", ".."} for part in model.split("/"))
TIERS = ("low", "medium", "high", "xhigh")
AVAILABILITY_STATES = ("available", "exhausted", "auth_failed", "transient", "unknown")
ROUTES = ("native_cli", "included_endpoint", "metered_openrouter")
OWNER_KINDS = ("human", "service")
STATUSES = ("selected", "no_eligible", "owner_unresolved")
BILLING_EVIDENCE = ("included_confirmed", "metered_confirmed", "unverified")

#: Caller-supplied task kinds the shared selector maps to a tier when the caller
#: gives none. These are preferences, never limits on what work may be done.
KIND_TO_TIER = {
    "mechanical": "low",
    "trivial": "low",
    "service_ops": "medium",
    "routine": "medium",
    "coding": "high",
    "debugging": "high",
    "cross_system": "xhigh",
}


class ContractError(ValueError):
    """A request or receipt does not satisfy the contract."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{name} must be a finite number")
    return float(value)


def _str_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{key} must be a list of strings")
    if not all(isinstance(v, str) and v for v in value):
        raise ContractError(f"{key} must contain only non-empty strings")
    return tuple(value)


def _obj_list(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{key} must be an array of objects")
    if not all(isinstance(v, Mapping) for v in value):
        raise ContractError(f"{key} must contain only objects")
    return list(value)


def _task_obj(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    task = raw.get("task")
    if not isinstance(task, Mapping):
        raise ContractError("task must be an object")
    return task


def _bool(raw: Mapping[str, Any], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ContractError(f"{key} must be a boolean (a string such as \"false\" is not accepted)")
    return value


def _str(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        if required:
            raise ContractError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ContractError(f"{key} must be a string")
    return value


@dataclass(frozen=True)
class Owner:
    """The initiating user (plan owner). A service identity is never an owner."""

    id: str
    kind: str

    @classmethod
    def parse(cls, raw: Any) -> "Owner | None":
        """Only ``None`` means no owner; any other non-object (``[]``, ``""``, ``0``) is invalid."""
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ContractError("owner must be an object")
        kind = _str(raw, "kind")
        if kind not in OWNER_KINDS:
            raise ContractError("owner.kind must be human or service")
        return cls(id=_str(raw, "id") or "", kind=kind or "")

    @property
    def is_human(self) -> bool:
        return self.kind == "human" and bool(self.id)


@dataclass(frozen=True)
class Binding:
    """Opaque reference to a provider credential/OAuth session. Never a secret."""

    ref: str
    provider: str
    owner_id: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Binding":
        return cls(ref=_str(raw, "ref") or "", provider=_str(raw, "provider") or "",
                   owner_id=_str(raw, "owner_id", required=False))


@dataclass(frozen=True)
class Availability:
    """One observation. ``model`` is optional (provider-wide observations)."""

    provider: str
    state: str
    checked_at: float
    source: str = "unspecified"
    model: str | None = None
    owner_id: str | None = None
    reset_at: float | None = None
    live: bool = False  # the observation came from a real check (a probe), not from a declaration or an assumption

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Availability":
        state = _str(raw, "state")
        if state not in AVAILABILITY_STATES:
            raise ContractError(f"availability.state must be one of {AVAILABILITY_STATES}")
        reset = raw.get("reset_at")
        checked = _finite(raw.get("checked_at"), "availability.checked_at")
        if reset is not None:
            reset = _finite(reset, "availability.reset_at")
        return cls(provider=_str(raw, "provider") or "", state=state or "unknown",
                   checked_at=checked, source=_str(raw, "source", required=False) or "unspecified",
                   model=_str(raw, "model", required=False), owner_id=_str(raw, "owner_id", required=False),
                   reset_at=reset, live=_bool(raw, "live"))


@dataclass(frozen=True)
class Task:
    objective: str
    summary: str = ""
    kind: str | None = None
    tools: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    context_size: int | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Task":
        kind = _str(raw, "kind", required=False)
        if kind is not None and kind not in KIND_TO_TIER:
            raise ContractError(f"task.kind must be one of {sorted(KIND_TO_TIER)}")
        size = raw.get("context_size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ContractError("task.context_size must be a non-negative integer")
        return cls(objective=_str(raw, "objective") or "", summary=_str(raw, "summary", required=False) or "",
                   kind=kind, tools=_str_list(raw, "tools"), modalities=_str_list(raw, "modalities"),
                   context_size=size)


def parse_previous(raw: Any) -> Mapping[str, Any] | None:
    """Validate an earlier selection before it can influence routing."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ContractError("previous_selection must be an object")
    if raw.get("contract") != CONTRACT_VERSION:
        raise ContractError(f"previous_selection.contract must be {CONTRACT_VERSION}")
    if raw.get("status") not in STATUSES:
        raise ContractError("previous_selection.status is not a known status")
    for key in ("thread_id", "caller"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ContractError(f"previous_selection.{key} is required")
    if raw.get("tier") is not None and raw["tier"] not in TIERS:
        raise ContractError(f"previous_selection.tier must be one of {TIERS}")
    if raw.get("owner") is not None:
        Owner.parse(raw["owner"])
    if raw["status"] == "selected":
        if raw.get("route") not in ROUTES:
            raise ContractError("previous_selection.route is not a known route")
        for key in ("provider", "model", "tier"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ContractError(f"previous_selection.{key} is required for a selected route")
    return raw


@dataclass(frozen=True)
class Request:
    caller: str
    thread_id: str
    task: Task
    owner: Owner | None = None
    tier: str | None = None
    preference_provider: str | None = None
    preference_model: str | None = None
    bindings: tuple[Binding, ...] = ()
    availability: tuple[Availability, ...] = ()
    allow_metered: bool = False
    previous_selection: Mapping[str, Any] | None = None
    task_changed: bool = False
    completed_effects: tuple[str, ...] = ()
    coordinator: str | None = None
    included_plans: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Request":
        tier = _str(raw, "tier", required=False)
        if tier is not None and tier not in TIERS:
            raise ContractError(f"tier must be one of {TIERS}")
        pref = raw.get("preference")
        if pref is None:
            pref = {}
        elif not isinstance(pref, Mapping):
            raise ContractError("preference must be an object or null")
        return cls(
            caller=_str(raw, "caller") or "",
            thread_id=_str(raw, "thread_id") or "",
            task=Task.parse(_task_obj(raw)),
            owner=Owner.parse(raw.get("owner")),
            tier=tier,
            preference_provider=_str(pref, "provider", required=False),
            preference_model=_str(pref, "model", required=False),
            bindings=tuple(Binding.parse(b) for b in _obj_list(raw, "bindings")),
            availability=tuple(Availability.parse(a) for a in _obj_list(raw, "availability")),
            allow_metered=_bool(raw, "allow_metered"),
            previous_selection=parse_previous(raw.get("previous_selection")),
            task_changed=_bool(raw, "task_changed"),
            completed_effects=_str_list(raw, "completed_effects"),
            coordinator=_str(raw, "coordinator", required=False),
            included_plans=_str_list(raw, "included_plans"),
        )


@dataclass(frozen=True)
class Candidate:
    """One reviewed (provider, model) entry from the tier table."""

    provider: str
    model: str
    tier: str
    group: str
    route: str
    plan: str
    binding_provider: str
    effort: str | None = None
    openrouter_slug: str | None = None
    catalog_provider: str | None = None
    external_catalog: str | None = None
    declarable_included: bool = False
    probe_required: bool = False  # a declared plan is only automatic with a successful live probe for this user

    def key(self) -> str:
        return f"{self.provider}/{self.model}@{self.tier}"


@dataclass(frozen=True)
class Selection:
    """The decision. Never evidence that anything ran."""

    status: str
    thread_id: str
    caller: str
    strategy: str
    reason: str
    tier: str | None
    tier_assigned_by: str
    tier_reason: str
    route: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    binding_ref: str | None = None
    owner: Owner | None = None
    owner_source: str = "request"
    availability_evidence: Mapping[str, Any] = field(default_factory=dict)
    uncertainty: str | None = None
    fallbacks: tuple[Mapping[str, Any], ...] = ()
    exclusions: tuple[Mapping[str, Any], ...] = ()
    affinity: Mapping[str, Any] = field(default_factory=dict)
    openrouter: Mapping[str, Any] | None = None
    completed_effects: tuple[str, ...] = ()
    table_version: str = ""
    selection_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION, "status": self.status, "selection_id": self.selection_id,
            "thread_id": self.thread_id, "caller": self.caller, "strategy": self.strategy, "reason": self.reason,
            "tier": self.tier, "tier_assigned_by": self.tier_assigned_by, "tier_reason": self.tier_reason,
            "route": self.route, "provider": self.provider, "model": self.model, "effort": self.effort,
            "binding_ref": self.binding_ref,
            "owner": {"id": self.owner.id, "kind": self.owner.kind} if self.owner else None,
            "owner_source": self.owner_source, "availability_evidence": dict(self.availability_evidence),
            "uncertainty": self.uncertainty, "fallbacks": [dict(f) for f in self.fallbacks],
            "exclusions": [dict(e) for e in self.exclusions], "affinity": dict(self.affinity),
            "openrouter": dict(self.openrouter) if self.openrouter else None,
            "completed_effects": list(self.completed_effects), "table_version": self.table_version,
        }


@dataclass(frozen=True)
class ExecutionReceipt:
    """What actually ran. Written by the execution adapter, not the selector."""

    selection_id: str
    provider: str
    model: str
    route: str
    success: bool
    billing_evidence: str = "unverified"
    observed_usage: Mapping[str, Any] = field(default_factory=dict)
    switch_reason: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_route: str | None = None
    selected_tier: str | None = None
    selected_effort: str | None = None
    effort: str | None = None
    auto_resolved: bool = False  # the model that ran is what OpenRouter Auto chose inside the allowed set

    def validate(self) -> "ExecutionReceipt":
        for name in ("selection_id", "provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"receipt.{name} must be a non-empty string")
        if not isinstance(self.success, bool):
            raise ContractError("receipt.success must be a boolean")
        if self.route not in ROUTES:
            raise ContractError(f"receipt.route must be one of {ROUTES}")
        if self.billing_evidence not in BILLING_EVIDENCE:
            raise ContractError(f"receipt.billing_evidence must be one of {BILLING_EVIDENCE}")
        if self.route == "metered_openrouter" and self.billing_evidence == "included_confirmed":
            raise ContractError("metered OpenRouter execution cannot claim included billing")
        identity = (self.selected_provider, self.selected_model, self.selected_route)
        supplied = [v is not None for v in identity]
        if (self.selected_tier is not None or self.selected_effort is not None) and not all(supplied):
            raise ContractError("selected_tier and selected_effort require the selected provider, model and route")
        if self.selected_tier is not None and self.selected_tier not in TIERS:
            raise ContractError(f"receipt.selected_tier must be one of {TIERS}")
        if any(supplied):
            if not all(supplied):
                raise ContractError("selected provider, model and route must be given together")
            if not all(isinstance(v, str) and v.strip() for v in identity):
                raise ContractError("selected provider, model and route must be non-empty strings")
            if self.selected_route not in ROUTES:
                raise ContractError(f"receipt.selected_route must be one of {ROUTES}")
        if self.switch_reason is not None and (not isinstance(self.switch_reason, str) or not self.switch_reason.strip()):
            raise ContractError("receipt.switch_reason must be a non-empty string when given")
        if (self.route == "metered_openrouter") != (self.provider == "openrouter"):
            raise ContractError("the openrouter provider and the metered_openrouter route go together")
        if self.route == "metered_openrouter" and self.model in AUTO_MODELS and self.success is True:
            raise ContractError("a successful OpenRouter Auto run must be receipted with the concrete model it served")
        if not isinstance(self.auto_resolved, bool):
            raise ContractError("receipt.auto_resolved must be a boolean")
        if self.auto_resolved:
            if not is_served_model_id(self.model) or self.model in AUTO_MODELS:
                raise ContractError("an Auto-resolved receipt needs a concrete OpenRouter served-model id")
            if self.selected_model not in AUTO_MODELS or self.selected_route != "metered_openrouter":
                raise ContractError("auto_resolved only applies to a metered OpenRouter Auto selection")
            if (self.provider, self.route) != (self.selected_provider, self.selected_route):
                raise ContractError("an Auto-resolved receipt must stay on the selected provider and route")
            ran = identity  # the served model is Auto's resolution of the selected model, not a different route
        else:
            ran = (self.provider, self.model, self.route)
        if all(supplied) and ran != identity and self.switch_reason is None:
            raise ContractError("a route that differs from the selection needs a switch_reason")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION, "selection_id": self.selection_id, "provider": self.provider,
            "model": self.model, "route": self.route, "success": self.success,
            "billing_evidence": self.billing_evidence, "observed_usage": dict(self.observed_usage),
            "switch_reason": self.switch_reason, "effort": self.effort, "auto_resolved": self.auto_resolved,
            "selected": {"provider": self.selected_provider, "model": self.selected_model,
                         "route": self.selected_route, "tier": self.selected_tier, "effort": self.selected_effort},
        }


def tier_for_task(task: Task) -> tuple[str, str]:
    """``(tier, reason)`` when the caller gave none. A preference, not a limit."""
    if task.kind:
        return KIND_TO_TIER[task.kind], f"task kind {task.kind}"
    return "medium", "no task kind given; defaulted to medium (a preference, not a limit)"


def tiers_tuple(values: Sequence[str]) -> tuple[str, ...]:
    bad = [v for v in values if v not in TIERS]
    if bad:
        raise ContractError(f"unsupported tiers: {bad}")
    return tuple(values)
