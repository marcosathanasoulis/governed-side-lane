"""OpenRouter Auto Router request building and response checking (no network, no key).

Documented shape (openrouter.ai/docs/guides/routing/routers/auto-router): model
``openrouter/auto`` with plugin id ``auto-router`` carrying ``allowed_models``
and ``cost_tier``. ``openrouter/auto-beta`` reads only the ``auto-beta-router``
plugin id. Tiers are bands, not price ceilings, so a narrow list can leave no
eligible model; that is reported (HTTP 404), never widened silently.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any, Mapping, Sequence

from .contract import TIERS, Candidate, ContractError

AUTO_MODEL = "openrouter/auto"
BETA_MODEL = "openrouter/auto-beta"
PLUGIN_ID = {AUTO_MODEL: "auto-router", BETA_MODEL: "auto-beta-router"}
NO_MATCH_STATUS = 404


def build_auto_request(tier: str, allowed_models: Sequence[str] | None, messages: Sequence[Mapping[str, Any]],
                       *, beta: bool = False, max_tokens: int | None = None) -> dict[str, Any]:
    if tier not in TIERS:
        raise ContractError(f"tier must be one of {TIERS}")
    if allowed_models is not None and not list(allowed_models):
        raise ContractError("no_eligible_models: allowed_models is empty; the list is not widened")
    model = BETA_MODEL if beta else AUTO_MODEL
    plugin: dict[str, Any] = {"id": PLUGIN_ID[model], "cost_tier": tier}
    if allowed_models is not None:  # no list means no restriction beyond the tier band
        plugin["allowed_models"] = list(dict.fromkeys(allowed_models))
    body: dict[str, Any] = {"model": model, "messages": [dict(m) for m in messages], "plugins": [plugin]}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


def allowed_for_tier(pool: Sequence[Candidate]) -> list[str]:
    """OpenRouter slugs for the reviewed same-tier candidates that have an exact mapping."""
    return list(dict.fromkeys(c.openrouter_slug for c in pool if c.openrouter_slug))


def check_response(body: Mapping[str, Any], sent: Mapping[str, Any], pool: Sequence[Candidate]) -> dict[str, Any]:
    """Verify the served model is inside what we allowed and has an exact native mapping.

    Returns ``{served, in_allowed, native}``; ``native`` is the exact reviewed
    candidate or ``None`` (reported as ``no_native_mapping``, never substituted).
    """
    served = body.get("model")
    if not isinstance(served, str):
        raise ContractError("response has no model field")
    plugin = sent["plugins"][0]
    in_allowed = any(fnmatchcase(served, pat) for pat in plugin["allowed_models"]) if "allowed_models" in plugin else True
    matches = [c for c in pool if c.openrouter_slug == served]
    native = matches[0] if len(matches) == 1 else None
    note = None
    if not matches:
        note = "no_native_mapping: served model has no exact reviewed native route"
    elif len(matches) > 1:
        note = "no_native_mapping: served model maps to several reviewed routes; not attributing it to one"
    return {"served": served, "in_allowed": in_allowed, "cost_tier": plugin["cost_tier"],
            "native": native.key() if native else None, "note": note}
