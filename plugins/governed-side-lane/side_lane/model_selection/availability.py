"""Availability state with per-state freshness. Distinguishes five states.

``unknown`` is never reported as exhausted, and ``auth_failed`` never triggers
credential replacement or a new grant: it is surfaced and left to the human.
A fresh ``available`` is evidence of current usability, not a quota guarantee.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from .contract import Availability

#: Seconds an observation stays current. ``auth_failed`` has no expiry: only a
#: new observation after the owner re-authenticates clears it.
FUTURE_SKEW_SECONDS = 5.0  # tolerated clock skew; anything later is not evidence

TTL_SECONDS: Mapping[str, float | None] = {
    "available": 900.0, "exhausted": 1800.0, "transient": 120.0, "unknown": None, "auth_failed": None,
}


def effective_state(obs: Availability | None, now: float) -> tuple[str, str]:
    """``(state, note)`` after applying freshness. A stale observation becomes unknown."""
    if obs is None:
        return "unknown", "no observation"
    if not math.isfinite(obs.checked_at):
        return "unknown", "non-finite observation time"
    if obs.checked_at > now + FUTURE_SKEW_SECONDS:
        return "unknown", "future-dated observation"
    ttl = TTL_SECONDS[obs.state]
    if obs.state == "exhausted" and obs.reset_at is not None:
        if now >= obs.reset_at:
            return "unknown", "exhaustion reset time has passed; recheck"
        return "exhausted", "until reset"
    if ttl is not None and now - obs.checked_at > ttl:
        return "unknown", f"stale {obs.state} observation"
    return obs.state, obs.source


def lookup(observations: Sequence[Availability], provider: str, model: str, owner_id: str | None,
           now: float | None = None) -> Availability | None:
    """The newest applicable observation wins, so a later provider-wide exhaustion is never hidden
    by an older model-specific ``available``. Ties go to the exact model, then the owner-scoped one."""
    best: tuple[tuple[float, int, int], Availability] | None = None
    for o in observations:
        if o.provider != provider or (o.model not in (None, model)):
            continue
        if o.owner_id is not None and o.owner_id != owner_id:
            continue
        if not math.isfinite(o.checked_at) or (now is not None and o.checked_at > now + FUTURE_SKEW_SECONDS):
            continue
        rank = (o.checked_at, 1 if o.model == model else 0, 1 if o.owner_id is not None else 0)
        if best is None or rank > best[0]:
            best = (rank, o)
    return best[1] if best else None
