"""The shared selector: tier first, then spread across comparable included routes.

Both Prompt it and Side Lane call :func:`select` with the same request and get
the same answer. There is no confidence threshold, no model self-acceptance and
no approval step here: tiers are selection preferences, never limits on work.
No candidate is silently substituted and no tier is silently changed.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any, Mapping, Protocol, Sequence

from . import availability as av
from . import openrouter as orr
from .contract import Candidate, Owner, Request, Selection, tier_for_task
from .rotation import MemoryState, NoTx, SpreadState, SpreadStrategy, Tx
from .tiers import TierTable

UNUSABLE = ("exhausted", "auth_failed", "transient")


class Strategy(Protocol):
    name: str

    def order(self, request: Request, pool: Sequence[Candidate], counts: Mapping[str, int]) -> tuple[list[Candidate], str]: ...


def _plan(c: Candidate, request: Request) -> str:
    """The catalog plan, except that an owner-declarable metered route (GLM) is included for an owner the
    caller says is entitled. Nothing else can be promoted: Gemini stays unverified and API-key routes stay metered."""
    if c.plan == "metered" and c.declarable_included and c.provider in request.included_plans:
        return "included"
    return c.plan


def _binding(request: Request, provider: str, owner: Owner | None, *, shared_ok: bool = False):
    """A binding owned by ``owner``, or (when ``shared_ok``) an explicitly shared/service one."""
    for b in request.bindings:
        if b.provider != provider:
            continue
        if owner is not None and b.owner_id == owner.id:
            return b
        if shared_ok and b.owner_id in (None, "service"):
            return b
    return None


def _evidence(request: Request, c: Candidate, owner: Owner | None, now: float) -> tuple[str, dict[str, Any]]:
    obs = av.lookup(request.availability, c.provider, c.model, owner.id if owner else None, now=now)
    state, note = av.effective_state(obs, now)
    return state, {"state": state, "checked_at": obs.checked_at if obs else None,
                   "source": obs.source if obs else None, "note": note, "live": bool(obs and obs.live)}


def _sel_id(thread: str, provider: str | None, model: str | None, tier: str | None, version: str, key: str) -> str:
    """Stable for one logical request (its key), so a delayed retry keeps the same id for receipt correlation."""
    raw = "|".join(str(x) for x in (thread, provider, model, tier, version, key))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _metered(request: Request, table: TierTable, tier: str, owner: Owner | None,
             now: float) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """``(openrouter info, why-not)`` for the metered route at the same tier.

    Only a binding owned by the resolved plan owner, or an explicitly shared/service one,
    may be used: another person's credential reference is never handed out."""
    na: dict[str, Any] = {"state": "n/a", "note": "metered route; not subscription-bound"}
    if not request.allow_metered:
        return None, "metered_not_allowed", na
    binding = _binding(request, "openrouter", owner, shared_ok=True)
    if binding is None:
        return None, "no_openrouter_binding", na
    obs = av.lookup(request.availability, "openrouter", orr.AUTO_MODEL, owner.id if owner else None, now=now)
    state, note = av.effective_state(obs, now)
    evidence = {"state": state, "checked_at": obs.checked_at if obs else None,
                "source": obs.source if obs else None, "note": note}
    if state in UNUSABLE:
        return None, f"openrouter_{state}", evidence
    allowed = orr.allowed_for_tier(table.for_tier(tier))
    if not allowed:  # never widen to the tier band alone: an empty reviewed set is reported
        return None, "no_eligible_models", evidence
    return {"model": orr.AUTO_MODEL, "plugin": orr.PLUGIN_ID[orr.AUTO_MODEL], "cost_tier": tier,
            "allowed_models": allowed, "billed": True, "binding_ref": binding.ref,
            "note": "metered inference; the served model is reported in the execution receipt"}, None, evidence


def _resolve_owner(request: Request, prev: Mapping[str, Any] | None) -> tuple[Owner | None, str, str | None]:
    prev_owner = Owner.parse(prev.get("owner")) if prev and prev.get("owner") is not None else None
    if prev_owner and prev_owner.is_human:
        note = None
        if request.owner and request.owner.id != prev_owner.id:
            note = "request owner differs from the thread's plan owner; plan owner retained"
        return prev_owner, "thread", note
    # A service/bot identity is never a plan owner, so it is never carried in the result.
    return (request.owner if request.owner and request.owner.is_human else None), "request", None


def _trusted_previous(request: Request) -> Mapping[str, Any] | None:
    """Prior state is only trusted for the same caller continuing the same thread."""
    p = request.previous_selection
    if p and p.get("thread_id") == request.thread_id and p.get("caller") == request.caller:
        return p
    return None


def _thread_key(request: Request) -> str:
    """Spread reservations are scoped to caller and thread: one thread is counted once however its
    inputs change; a changed request replaces its reservation instead of adding another."""
    return hashlib.sha256(f"{request.thread_id}|{request.caller}".encode()).hexdigest()[:24]


def _request_key(request: Request, prev: Mapping[str, Any] | None, *, with_caller: bool = True) -> str:
    """Identity of one logical request, so a retry reuses its spread reservation. Selection ids leave the
    caller out so Prompt it and Side Lane agree on the id for identical input."""
    raw = "|".join(str(x) for x in (
        request.thread_id, request.caller if with_caller else "", request.tier, request.task.objective, request.task.kind,
        request.preference_provider, request.preference_model, request.task_changed,
        (prev or {}).get("selection_id")))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def select(request: Request, table: TierTable, *, now: float, state: SpreadState | None = None,
           strategy: Strategy | None = None, commit: bool = True) -> Selection:
    """One atomic selection: spread counts are read, the choice made and the count recorded inside a
    single state transaction, so concurrent callers cannot choose the same minimum."""
    state = state if state is not None else MemoryState()
    prev = _trusted_previous(request)
    owner, _, _ = _resolve_owner(request, prev)
    ctx = state.transaction(owner.id, commit) if owner is not None and owner.is_human else contextlib.nullcontext(NoTx())
    with ctx as tx:
        return _select(request, table, now=now, tx=tx, strategy=strategy or SpreadStrategy(), prev=prev)


def _select(request: Request, table: TierTable, *, now: float, tx: Tx, strategy: Strategy,
            prev: Mapping[str, Any] | None) -> Selection:
    owner, owner_source, owner_note = _resolve_owner(request, prev)
    req_key = _request_key(request, prev)
    id_key = _request_key(request, prev, with_caller=False)

    if request.tier:
        tier, by, tier_reason = request.tier, "caller", "tier supplied by caller"
    elif prev and prev.get("tier") and not request.task_changed:
        tier = prev["tier"]
        by = prev.get("tier_assigned_by") or "caller"
        tier_reason = prev.get("tier_reason") or "tier kept from the earlier selection in this thread"
    else:
        tier, tier_reason = tier_for_task(request.task)
        by = "selector"
    base = dict(thread_id=request.thread_id, caller=request.caller, strategy=strategy.name, tier=tier,
                tier_assigned_by=by, tier_reason=tier_reason, table_version=table.version,
                completed_effects=request.completed_effects, owner=owner, owner_source=owner_source)
    exclusions: list[dict[str, Any]] = []
    if owner_note:
        exclusions.append({"candidate": "owner", "reason": owner_note})

    def metered_fallbacks() -> list[dict[str, Any]]:
        info, why, _ = _metered(request, table, tier, owner, now)
        if info:
            return [{"route": "metered_openrouter", "provider": "openrouter", "model": orr.AUTO_MODEL,
                     "reason": f"same-tier ({tier}) metered fallback"}]
        exclusions.append({"candidate": "openrouter", "reason": why})
        return []

    def metered_selection(reason: str, uncertainty: str | None = None, sticky: bool = False,
                          selection_id: str | None = None, reselect: str | None = None) -> Selection:
        info, why, evidence = _metered(request, table, tier, owner, now)
        if not info:
            return Selection(status="no_eligible", reason=f"{reason}; metered fallback unavailable ({why})",
                             exclusions=tuple(exclusions), **base)
        unknown_note = ("availability unknown: OpenRouter not confirmed available or exhausted"
                        if evidence["state"] == "unknown" else None)
        if not sticky:  # the thread moved to a route that is not part of the spread: release its reservation
            tx.release()
        aff: dict[str, Any] = {"thread_id": request.thread_id, "sticky": sticky}
        if reselect:
            aff["reselect_reason"] = reselect
        return Selection(status="selected", reason=reason, route="metered_openrouter", provider="openrouter",
                         model=orr.AUTO_MODEL, binding_ref=info["binding_ref"], openrouter=info,
                         uncertainty=uncertainty or unknown_note, exclusions=tuple(exclusions),
                         availability_evidence=evidence, affinity=aff,
                         selection_id=selection_id or _sel_id(request.thread_id, "openrouter", orr.AUTO_MODEL, tier, table.version, id_key),
                         **base)

    # Owner: a bot or missing identity never becomes the plan owner.
    if owner is None or not owner.is_human:
        exclusions.append({"candidate": "included-plans", "reason": "owner_unresolved: no human plan owner; included plans not considered"})
        unpinned_or_auto = (request.preference_provider is None and request.preference_model is None) or (
            request.preference_provider == "openrouter" and request.preference_model in (None, orr.AUTO_MODEL))
        if request.allow_metered and unpinned_or_auto:
            return metered_selection("no human plan owner resolved; metered route does not need one",
                                     uncertainty="owner_unresolved")
        return Selection(status="owner_unresolved", reason="no human plan owner; metered fallback not allowed or pinned away",
                         exclusions=tuple(exclusions), **{**base, "owner": None})

    had_prev_selected = bool(prev and prev.get("status") == "selected")
    tx.begin(_thread_key(request))
    counts = tx.counts()

    def finish(c: Candidate, reason: str, evidence: dict[str, Any], binding_ref: str, *, sticky: bool,
               fallbacks: list[dict[str, Any]], reselect: str | None, tier_by: str | None = None,
               tier_why: str | None = None) -> Selection:
        if not sticky:  # a thread counts once: the reservation moves with a reselection (see Tx.record)
            tx.record(c.provider)
        aff = {"thread_id": request.thread_id, "sticky": sticky}
        if reselect:
            aff["reselect_reason"] = reselect
        unc = "availability unknown: not confirmed available or exhausted" if evidence["state"] == "unknown" else None
        if _plan(c, request) != c.plan:  # say so: this is a declared entitlement, not verified billing
            evidence = {**evidence, "entitlement": "declared included by the caller; billing not verified"}
        merged = {**base, "tier": c.tier}
        if tier_by:
            merged["tier_assigned_by"], merged["tier_reason"] = tier_by, tier_why or base["tier_reason"]
        return Selection(status="selected", reason=reason, route=c.route, provider=c.provider, model=c.model,
                         effort=c.effort, binding_ref=binding_ref, availability_evidence=evidence, uncertainty=unc,
                         fallbacks=tuple(fallbacks), exclusions=tuple(exclusions), affinity=aff,
                         selection_id=_sel_id(request.thread_id, c.provider, c.model, c.tier, table.version, id_key),
                         **merged)

    # Explicit preference: honored when supported; never silently replaced.
    if request.preference_provider or request.preference_model:
        if request.preference_provider == "openrouter":
            if request.preference_model not in (None, orr.AUTO_MODEL):
                exclusions.append({"candidate": f"openrouter/{request.preference_model}",
                                   "reason": "unsupported_preference: exact OpenRouter model pins are not routed through Auto; "
                                             "pin a native route or use openrouter/auto"})
                return Selection(status="no_eligible", reason="exact OpenRouter model pins are not supported; nothing substituted",
                                 exclusions=tuple(exclusions), **base)
            tier_moved = bool(request.tier and prev and prev.get("tier") and request.tier != prev["tier"])
            same_route = bool(prev and prev.get("status") == "selected" and prev.get("route") == "metered_openrouter"
                              and not request.task_changed and not tier_moved)
            pin_reselect = None
            if had_prev_selected and not same_route:
                pin_reselect = ("task materially changed" if request.task_changed
                                else f"caller changed the tier from {prev['tier']} to {request.tier}" if tier_moved
                                else "explicit request changed the route to openrouter/auto")
            return metered_selection("explicit request for OpenRouter", sticky=same_route, reselect=pin_reselect,
                                     selection_id=(str(prev.get("selection_id") or "") or None) if same_route else None)
        matches = (table.find(request.preference_provider, request.preference_model) if request.preference_provider
                   else table.find_model(request.preference_model or ""))
        label = f"{request.preference_provider or '*'}/{request.preference_model or '*'}"
        if not matches:
            exclusions.append({"candidate": label, "reason": "unsupported_preference: not in the reviewed tier table"})
            return Selection(status="no_eligible", reason="explicit provider/model is not supported",
                             exclusions=tuple(exclusions), **base)
        same_tier = [m for m in matches if m.tier == tier]
        tier_by = tier_why = None
        if same_tier:
            candidates = same_tier
        elif by == "caller":  # the caller fixed the tier: do not move it for a pin
            exclusions.append({"candidate": label, "reason": f"pin_tier_mismatch: only in {sorted({m.tier for m in matches})}, "
                                                             f"requested tier is {tier}; tier not changed"})
            return Selection(status="no_eligible", reason="the pinned model is not in the requested tier; nothing substituted",
                             exclusions=tuple(exclusions), **base)
        else:  # tier was only a default: the explicit request defines it, and says so
            first_tier = matches[0].tier
            candidates = [m for m in matches if m.tier == first_tier]
            tier_by, tier_why = "preference", f"explicit request for {label} (tier {first_tier})"
            # the pin defines the effective tier: fallbacks and any no_eligible result report it
            tier = first_tier
            base = {**base, "tier": tier, "tier_assigned_by": tier_by, "tier_reason": tier_why}
        for c in candidates:  # every same-tier match is evaluated; the first usable one wins
            b = _binding(request, c.binding_provider, owner)
            st, ev = _evidence(request, c, owner, now)
            if _plan(c, request) == "metered" and not request.allow_metered:
                exclusions.append({"candidate": c.key(), "reason": "metered_not_allowed: billable route needs allow_metered"})
            elif b is None:
                exclusions.append({"candidate": c.key(), "reason": "no_binding"})
            elif st in UNUSABLE:
                exclusions.append({"candidate": c.key(), "reason": st})
            else:
                same_as_prev = bool(prev and prev.get("status") == "selected" and not request.task_changed
                                    and (prev.get("provider"), prev.get("model"), prev.get("tier")) == (c.provider, c.model, c.tier))
                pin_reselect = None
                if had_prev_selected and not same_as_prev:
                    pin_reselect = ("task materially changed" if request.task_changed
                                    else f"explicit request changed the route to {c.provider}/{c.model}")
                return finish(c, f"explicit request for {c.provider}/{c.model}", ev, b.ref, sticky=same_as_prev,
                              fallbacks=metered_fallbacks(), reselect=pin_reselect, tier_by=tier_by, tier_why=tier_why)
        return Selection(status="no_eligible", reason="the explicitly requested route is not usable; nothing substituted",
                         exclusions=tuple(exclusions), **base)

    # Affinity: keep a working route on follow-ups.
    reselect: str | None = None
    if prev and prev.get("status") == "selected":
        if request.task_changed:
            reselect = "task materially changed"
        elif request.tier and prev.get("tier") and request.tier != prev["tier"]:
            reselect = f"caller changed the tier from {prev['tier']} to {request.tier}"
        elif prev.get("route") == "metered_openrouter":
            if _metered(request, table, tier, owner, now)[0]:
                return metered_selection("follow-up stays on the metered route for continuity", sticky=True,
                                         selection_id=str(prev.get("selection_id") or "") or None)
            reselect = "metered route no longer available"
        else:
            pc = next(iter(table.find(prev["provider"], prev["model"], prev.get("tier") or tier)), None)
            pb = _binding(request, pc.binding_provider, owner) if pc else None
            if pc is None:
                reselect = "previous route no longer in the tier table"
            elif pb is None:
                reselect = "previous route has no binding for the plan owner"
            elif _plan(pc, request) == "metered" and not request.allow_metered:
                reselect = "metered use is no longer allowed"
            elif (prev.get("availability_evidence") or {}).get("entitlement") and _plan(pc, request) != "included":
                reselect = "the included-plan declaration for this route is no longer present"
            else:
                pst, pev = _evidence(request, pc, owner, now)
                if pc.probe_required and _plan(pc, request) != pc.plan and not (pev.get("live") and pst == "available"):
                    reselect = "the previous route no longer has a successful live probe"
                elif pst in UNUSABLE:
                    reselect = f"previous route {pst}"
                else:
                    return finish(pc, "follow-up stays on the selected route for continuity", pev, pb.ref,
                                  sticky=True, fallbacks=metered_fallbacks(), reselect=None)

    pool = table.for_tier(tier)
    avail: list[Candidate] = []
    unknown: list[Candidate] = []
    evid: dict[str, dict[str, Any]] = {}
    binds: dict[str, str] = {}
    for c in pool:
        b = _binding(request, c.binding_provider, owner)
        if b is None:
            exclusions.append({"candidate": c.key(), "reason": "no_binding"})
            continue
        if _plan(c, request) != "included":
            why = ("plan_unverified: billing/auth not yet verified; pin to use" if c.plan == "unverified"
                   else "metered_route: billable and explicit-only; pin to use")
            exclusions.append({"candidate": c.key(), "reason": why})
            continue
        st, ev = _evidence(request, c, owner, now)
        evid[c.key()], binds[c.key()] = ev, b.ref
        if c.probe_required and _plan(c, request) != c.plan and not (ev.get("live") and st == "available"):
            # a declaration alone never selects this route, and neither does a failed or inconclusive probe: it needs a
            # successful live check for this user
            exclusions.append({"candidate": c.key(), "reason": "probe_required: a declared plan needs a successful live probe"})
            continue
        if st == "available":
            avail.append(c)
        elif st == "unknown":
            unknown.append(c)
        else:
            exclusions.append({"candidate": c.key(), "reason": st})
    ordered_a, why_a = strategy.order(request, avail, counts) if avail else ([], "")
    ordered_u, why_u = strategy.order(request, unknown, counts) if unknown else ([], "")
    ordered = ordered_a + [c for c in ordered_u if c.provider not in {x.provider for x in ordered_a}]
    if not ordered:
        return metered_selection(f"no available included {tier} route", reselect=reselect)
    top, rest = ordered[0], ordered[1:]
    reason = why_a if avail else f"{why_u}; availability unknown for all {tier} candidates"
    fallbacks = [{"route": c.route, "provider": c.provider, "model": c.model, "reason": "next in spread order"} for c in rest]
    fallbacks += metered_fallbacks()
    return finish(top, reason, evid[top.key()], binds[top.key()], sticky=False, fallbacks=fallbacks, reselect=reselect)
