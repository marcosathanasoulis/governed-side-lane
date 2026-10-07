"""Local Auto Router decision for Prompt it and Side Lane.

One function, :func:`decide`, answers "which model should run this task, using
only what this user can reach, and without spending metered money unless that
is genuinely the best option". Prompt it staffing and Side Lane dispatch both
call it, so there is one policy to improve.

The rules come from the shared core in ``auto_route_policy`` (a port of the
hosted Side Lane's OpenRouter Auto Router policy):

1. An explicit pin wins.
2. Included OAuth usage first: a signed-in host (Claude, Codex) whose plan is
   included and not exhausted runs the task through its own CLI. No metered
   call, and no OpenRouter key is needed.
3. Otherwise, with an OpenRouter key, ask the Auto Router to choose. Providers
   whose included usage is out (declared ``extra-usage``) go on its
   ``excluded_models`` so extra usage is never bought by accident; the
   router's pick is metered and is only used when nothing included can run
   the task.
4. With neither, stop and say what to set up. Never silently use extra usage.

This module never reads a credential. The selection probe in
:func:`probe_auto_router` receives the key through an injected reader and an
injected transport; tests never use a real one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from side_lane import auto_route_policy as policy

POLICY_ID = "side-lane-auto-route/v1"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
PROBE_MAX_TOKENS = 16
PROBE_DIGEST_MAX_CHARS = 1500
PROBE_TIMEOUT_SECONDS = 30

#: Local default tier. The hosted policy defaults to ``high``; locally the tier
#: also picks a native model, and spending top-tier capacity on every task wastes
#: it, so the local default is ``medium``. Request words ("use the best model",
#: "use the cheapest model") still override it.
LOCAL_DEFAULT_COST_TIER = "medium"

#: Native model ladder per host and cost tier, ordered by price (OpenRouter
#: list prices per million tokens in/out, 2026-10-05): Claude haiku 1/5, sonnet
#: 2/10, opus 4/20, fable 10/50; Codex luna 0.1/0.5, sol 2/10, astra 10/50.
#: The top model of each host (fable, astra) is only for tasks that really need
#: it, so it sits at ``max`` and nothing below that reaches it. Devin runs on
#: its own subscription and is used for the equivalents below or when it is the
#: only included host.
NATIVE_LADDER: Mapping[str, Mapping[str, str]] = {
    "claude": {
        "low": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-5-5",
        "high": "claude-opus-5-5",
        "xhigh": "claude-opus-5-5",
        "max": "claude-fable-5-1",
    },
    "codex": {
        "low": "gpt-6-luna",
        "medium": "gpt-6-sol",
        "high": "gpt-6-sol",
        "xhigh": "gpt-6-sol",
        "max": "gpt-6-astra",
    },
    "glm": {tier: "glm-5.3" for tier in ("low", "medium", "high", "xhigh", "max")},
    "gemini": {tier: "gemini-default" for tier in ("low", "medium", "high", "xhigh", "max")},
    "devin": {
        "low": "swe-1-7-medium",
        "medium": "swe-2-medium",
        "high": "swe-2-high",
        "xhigh": "swe-2-high",
        "max": "swe-2-max",
    },
}
#: OpenRouter model id -> the native CLI model that is the same model.
OPENROUTER_TO_NATIVE: Mapping[str, tuple[str, str]] = {
    "anthropic/claude-haiku-4.5": ("claude", "claude-haiku-4-5-20251001"),
    "anthropic/claude-sonnet-5.5": ("claude", "claude-sonnet-5-5"),
    "anthropic/claude-opus-5.5": ("claude", "claude-opus-5-5"),
    "anthropic/claude-fable-5.1": ("claude", "claude-fable-5-1"),
    "openai/gpt-6-luna": ("codex", "gpt-6-luna"),
    "openai/gpt-6-sol": ("codex", "gpt-6-sol"),
    "openai/gpt-6-astra": ("codex", "gpt-6-astra"),
}
#: OpenRouter picks that run on a subscription plan the router does not know by name:
#: any ``z-ai/glm-*`` pick runs on the direct GLM route (glm-5.3) and any
#: ``google/gemini-*`` pick on the Gemini CLI's default model, while that plan has usage.
PATTERN_TO_NATIVE: tuple[tuple[str, str, str], ...] = (
    ("z-ai/glm-*", "glm", "glm-5.3"),
    ("google/gemini-*", "gemini", "gemini-default"),
)
#: The lowest cost tier at which a native model may be used. A model is only in
#: the Auto Router's pool when the task's tier reaches it, so fable and astra
#: (the same price) are reserved for ``max`` and opus for ``high`` and up.
NATIVE_MIN_TIER: Mapping[str, str] = {
    "claude-haiku-4-5-20251001": "low", "claude-sonnet-5-5": "medium",
    "claude-opus-5-5": "high", "claude-fable-5-1": "max",
    "gpt-6-luna": "low", "gpt-6-sol": "medium", "gpt-6-astra": "max",
}
#: Very high coding models and the Devin model used in their place when Auto
#: recommends one and Devin is available (Auto does not know Devin). Pattern
#: lists are checked in order. To be confirmed by the maintainers.
DEVIN_EQUIVALENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("anthropic/claude-fable-*", "openai/gpt-6-astra*", "openai/gpt-6.1-sol-pro",
      "openai/gpt-6-sol-pro"), "swe-2-max"),
    (("anthropic/claude-opus-*", "openai/gpt-6.1-sol*"), "swe-2-high"),
    (("openai/gpt-6-sol", "openai/gpt-5.6-sol"), "swe-2-medium"),
)
#: OpenRouter model patterns that belong to each host's plan.
HOST_MODEL_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "claude": ("anthropic/*",),
    "codex": ("openai/*",),
    "glm": ("z-ai/*",),
    "gemini": ("google/*",),
    "devin": (),
}
HOST_ORDER = ("claude", "codex", "glm", "gemini", "devin")
USAGE_STATES = ("included-oauth", "extra-usage", "unknown")


#: Planning notes the coordinating model reads when OpenRouter is not set up.
#: Coarse priors for choosing staffing, not benchmarks; the task wins over them.
MODEL_PROFILES: Mapping[str, Mapping[str, Any]] = {
    "claude-haiku-4-5-20251001": {
        "host": "claude", "tier": "low", "relative_cost": "lowest",
        "strengths": "fast and cheap; mechanical edits, lookups, summaries, simple tests",
        "weaknesses": "small context for large repos; weaker on multi-file design and subtle bugs",
    },
    "claude-sonnet-5-5": {
        "host": "claude", "tier": "medium", "relative_cost": "moderate",
        "strengths": "strong everyday coding, tool use and review; the default balance",
        "weaknesses": "can miss deep architectural trade-offs that the top models catch",
    },
    "claude-opus-5-5": {
        "host": "claude", "tier": "high", "relative_cost": "high",
        "strengths": "hard design, risky refactors, ambiguous or security-sensitive work",
        "weaknesses": "uses included allowance faster; more than routine work needs",
    },
    "claude-fable-5-1": {
        "host": "claude", "tier": "max", "relative_cost": "highest",
        "strengths": "the strongest Claude model; only for work that truly needs it",
        "weaknesses": "same price as the top Codex model; wasteful for anything routine",
    },
    "gpt-6-luna": {
        "host": "codex", "tier": "low", "relative_cost": "lowest",
        "strengths": "very cheap and fast; boilerplate and small scripted changes",
        "weaknesses": "limited depth; asks few clarifying questions; not for unfamiliar code",
    },
    "gpt-6-sol": {
        "host": "codex", "tier": "medium", "relative_cost": "moderate",
        "strengths": "solid general coding, repo-wide edits and test writing; the Codex default",
        "weaknesses": "less careful than astra on ambiguous or high-risk work",
    },
    "gpt-6-astra": {
        "host": "codex", "tier": "max", "relative_cost": "highest",
        "strengths": "the strongest Codex model; best at spotting risks in hard problems",
        "weaknesses": "same price as the top Claude model; only for really high-thinking tasks",
    },
    "glm-5.3": {
        "host": "glm", "tier": "medium", "relative_cost": "subscription",
        "strengths": "included-cost coding model on the GLM plan; good everyday implementation",
        "weaknesses": "less careful than the top Claude and Codex models on hard or ambiguous work",
    },
    "gemini-default": {
        "host": "gemini", "tier": "medium", "relative_cost": "subscription",
        "strengths": "included-cost model on the Google plan; fast, large context",
        "weaknesses": "tool use and long agentic runs are less proven here than Claude or Codex",
    },
    "swe-2-high": {
        "host": "devin", "tier": "high", "relative_cost": "subscription",
        "strengths": "included-cost coding agent for implementation work",
        "weaknesses": "hosted; can burn many steps on large tasks",
    },
    "swe-2-max": {
        "host": "devin", "tier": "max", "relative_cost": "subscription",
        "strengths": "highest-effort Devin coding model",
        "weaknesses": "no evidence of an advantage on routine work",
    },
}


class AutoRouteError(ValueError):
    """A malformed request to the local Auto Router layer."""


@dataclass(frozen=True)
class Inventory:
    """What this user can reach. Booleans and declared states only."""

    hosts: Mapping[str, bool]
    host_usage: Mapping[str, str] = field(default_factory=dict)
    openrouter: bool = False

    def __post_init__(self) -> None:
        for host, state in self.host_usage.items():
            if state not in USAGE_STATES:
                raise AutoRouteError(f"unsupported usage state for {host}: {state!r}")


def devin_ready(host_lookup=None) -> bool:
    """Devin counts only when its CLI is installed *and* signed in (``devin auth status``)."""
    from side_lane import auth
    from side_lane import hosts as _hosts

    host_lookup = host_lookup or _hosts
    executable = host_lookup.resolve_host_executable("devin")
    if executable is None:
        return False
    try:
        return auth.auth_status("devin", executable=str(executable)).state == "ready"
    except Exception:  # noqa: BLE001 - a broken status call just means Devin is not usable
        return False


def build_inventory(
    *,
    hosts: Mapping[str, bool] | None = None,
    host_usage: Mapping[str, str] | None = None,
    openrouter: bool | None = None,
    credential_service: str | None = None,
) -> Inventory:
    """Detect the inventory; every argument overrides its detector (for tests
    and for the private package, which has richer detection)."""
    from side_lane import model_select, preferences

    if hosts is None:
        from side_lane import hosts as host_lookup

        hosts = dict(model_select.detect_available_hosts())
        hosts["devin"] = devin_ready(host_lookup)
    if host_usage is None:
        host_usage = preferences.load_preferences()
    if openrouter is None:
        if credential_service:
            from side_lane import credentials

            openrouter = credentials.credential_present(credential_service)
        else:
            openrouter = model_select.openrouter_present()
    return Inventory(hosts=dict(hosts), host_usage=dict(host_usage), openrouter=bool(openrouter))


def plans_for(inventory: Inventory, *, unknown_usage: str = "included") -> tuple[policy.DeclaredPlan, ...]:
    """Declared plans derived from the per-host usage states.

    ``included-oauth`` is an included plan the host can run on. ``extra-usage``
    means the included allowance is spent: the plan is *extra*, so that host is
    not run natively and its models are not hidden from the Auto Router either,
    where they compete on metered price like any other model. ``unknown``
    follows ``unknown_usage``: the public default treats a host the user is
    signed in to as included; the private package passes ``excluded`` so an
    unattested host is never used silently (it is then treated as extra).
    """
    if unknown_usage not in {"included", "excluded"}:
        raise AutoRouteError("unknown_usage must be 'included' or 'excluded'")
    plans = []
    for host in HOST_ORDER:
        if not inventory.hosts.get(host):
            continue
        state = inventory.host_usage.get(host, "unknown")
        if state == "included-oauth":
            mode, quota = "included", "declared"
        elif state == "extra-usage":
            mode, quota = "extra", "declared"
        elif unknown_usage == "included":
            mode, quota = "included", "unknown"
        else:
            mode, quota = "extra", "declared"
        plans.append(
            policy.DeclaredPlan(
                provider=host, mode=mode, quota_status=quota,
                model_patterns=HOST_MODEL_PATTERNS[host], route_ids=(f"native-{host}",),
            )
        )
    return tuple(plans)


def _host_for_model(model: str) -> str | None:
    if model.startswith("claude"):
        return "claude"
    if model.startswith("gpt-"):
        return "codex"
    if model.startswith("glm-"):
        return "glm"
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith("swe-"):
        return "devin"
    return None


def _usable_hosts(plans: Sequence[policy.DeclaredPlan], inventory: Inventory, now: float) -> list[str]:
    return [
        host for host in HOST_ORDER
        if inventory.hosts.get(host)
        and any(p.provider == host and p.usable_included(now) for p in plans)
    ]


def build_handoff(usable: Sequence[str], tier: str = "max") -> dict[str, Any]:
    """What a served OpenRouter model turns into: a native model on a host with
    included usage (only models the task's tier reaches), or the Devin
    equivalent of a very high coding model."""
    order = list(policy.COST_TIERS)
    native = {
        or_id: {"host": host, "model": model}
        for or_id, (host, model) in OPENROUTER_TO_NATIVE.items()
        if host in usable and order.index(NATIVE_MIN_TIER[model]) <= order.index(tier)
    }
    devin = (
        [{"patterns": list(patterns), "model": model} for patterns, model in DEVIN_EQUIVALENTS]
        if "devin" in usable else []
    )
    patterns = [
        {"pattern": pattern, "host": host, "model": model}
        for pattern, host, model in PATTERN_TO_NATIVE if host in usable
    ]
    return {"native": native, "devin": devin, "patterns": patterns}


def resolve_served(handoff: Mapping[str, Any], served: str) -> dict[str, Any]:
    """Map the model the Auto Router served to the route that runs it."""
    import fnmatch

    hit = (handoff.get("native") or {}).get(served)
    if hit:
        return {"action": "native-handoff", "host": hit["host"], "model": hit["model"],
                "served_model": served, "metered": False}
    for entry in handoff.get("patterns") or []:
        if fnmatch.fnmatchcase(served, entry["pattern"]):
            return {"action": "native-handoff", "host": entry["host"], "model": entry["model"],
                    "served_model": served, "metered": False}
    for entry in handoff.get("devin") or []:
        if any(fnmatch.fnmatchcase(served, pat) for pat in entry["patterns"]):
            return {"action": "devin-equivalent", "host": "devin", "model": entry["model"],
                    "served_model": served, "metered": False}
    if handoff.get("devin_default"):
        # Devin is the only included host: anything that is not a very high coding
        # model still runs on Devin's own tier rather than buying a metered model.
        return {"action": "devin-default", "host": "devin", "model": handoff["devin_default"],
                "served_model": served, "metered": False}
    return {"action": "openrouter-exact", "model": served, "served_model": served, "metered": True}


def decide(
    inventory: Inventory,
    query: str,
    *,
    pinned_model: str | None = None,
    authorize_extra_hosts: Iterable[str] = (),
    plans: Sequence[policy.DeclaredPlan] | None = None,
    unknown_usage: str = "included",
    settings: policy.AutoRouterSettings | None = None,
    service_names: Iterable[str] = (),
    session_key: str = "local",
    now: float | None = None,
    tier_override: str | None = None,
) -> dict[str, Any]:
    """Return the routing decision as a JSON-safe dict.

    ``action`` is one of ``native`` (run through a host's CLI on included
    usage), ``auto`` (ask the Auto Router across everything not excluded;
    metered), ``pinned`` or ``blocked`` (``next_steps`` says what to set up).

    With included usage and an OpenRouter key, a ``native`` decision carries a
    ``selection`` block: run :func:`probe_auto_router` so the Auto Router picks
    the best model *inside* the providers that still have included usage (the
    probe costs well under a cent), then follow :func:`resolve_served`. Without
    a key the tier ladder and a staffing menu are used instead.
    """
    current = time.time() if now is None else now
    settings = settings or policy.AutoRouterSettings(default_cost_tier=LOCAL_DEFAULT_COST_TIER)
    plan_list = tuple(plans) if plans is not None else plans_for(inventory, unknown_usage=unknown_usage)
    authorized = set(authorize_extra_hosts)
    tier = tier_override or policy.cost_tier_for(
        query, default=settings.default_cost_tier, complexity=settings.complexity_tiers
    )
    if tier not in policy.COST_TIERS:
        raise AutoRouteError(f"unsupported cost tier {tier!r}")
    out_providers = policy.providers_out(plan_list, {}, (), now=current)
    usable = _usable_hosts(plan_list, inventory, current)
    receipt: dict[str, Any] = {
        "policy": POLICY_ID,
        "cost_tier": tier,
        "tier_source": "override" if tier_override else "words-or-default",
        "inventory": {
            "hosts": {h: bool(inventory.hosts.get(h)) for h in HOST_ORDER},
            "host_usage": dict(inventory.host_usage),
            "openrouter_key_present": inventory.openrouter,
        },
        "plans": policy.plans_summary(plan_list, current),
        "providers_out": list(out_providers),
        "usable_included_hosts": usable,
        "authorized_extra_hosts": sorted(authorized),
    }

    def turn_for(allowed: Sequence[str] = (), excluded_providers: Sequence[str] = ()) -> dict[str, Any]:
        base = policy.build_turn_settings(
            settings, thread=("local", session_key), query=query, granted_capabilities=(),
            service_names=service_names, plans=plan_list, excluded_providers=excluded_providers,
        )
        base = dataclasses.replace(base, cost_tier=tier)
        if allowed:
            base = dataclasses.replace(base, allowed_models=tuple(allowed))
        return base.to_context()

    def native(host: str, reason: str, extra: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": "native", "host": host, "model": NATIVE_LADDER[host][tier],
            "metered": extra, "reason": reason, "receipt": receipt,
        }
        if not extra:
            result["staffing"] = {
                "mode": "coordinator-choice", "default": result["model"],
                "instructions": STAFFING_INSTRUCTIONS,
                "candidates": staffing_candidates(inventory, plan_list, current),
            }
        return result

    # 1. Pin wins.
    if pinned_model:
        host = _host_for_model(pinned_model)
        if host is not None:
            if not inventory.hosts.get(host):
                return _blocked(receipt, [f"pinned model needs the {host} CLI, which is not installed"], inventory)
            runnable = host in usable
            if not runnable and host not in authorized:
                return _blocked(
                    receipt,
                    [f"{host} has no included usage; authorize extra usage for this run to use {pinned_model}"],
                    inventory,
                )
            return {"action": "pinned", "host": host, "model": pinned_model,
                    "metered": not runnable, "reason": "explicit pin", "receipt": receipt}
        if not inventory.openrouter:
            return _blocked(receipt, ["pinned OpenRouter model needs an OpenRouter key"], inventory)
        if not policy.SERVED_MODEL_RE.fullmatch(pinned_model):
            raise AutoRouteError("invalid pinned model id")
        return {"action": "pinned", "host": "openrouter", "model": pinned_model,
                "metered": True, "reason": "explicit pin", "receipt": receipt}

    # 2. Included usage first. With a key, the Auto Router picks inside the included providers.
    if usable:
        first = usable[0]
        result = native(first, "included usage available; no metered call needed")
        handoff = build_handoff(usable, tier)
        if "devin" in usable and not handoff["native"] and not handoff["patterns"]:
            handoff["devin_default"] = NATIVE_LADDER["devin"][tier]
        if inventory.openrouter and (handoff["native"] or handoff["patterns"] or handoff["devin"]):
            result["selection"] = {
                "mode": "auto-within-included",
                "instructions": "Run probe_auto_router with this decision; it returns the model to use.",
                # Native hosts: Auto picks only inside their models. Devin only: Auto
                # looks across everything and a very high coding pick maps to Devin.
                "turn_settings": turn_for(
                    allowed=list(handoff["native"]) + [entry["pattern"] for entry in handoff["patterns"]]
                ),
                "handoff": handoff,
            }
        return result

    # 3. Auto Router, metered, across everything not excluded.
    if inventory.openrouter:
        handoff = build_handoff(_usable_hosts(plan_list, inventory, current), tier)
        return {
            "action": "auto", "metered": True,
            "reason": "no included usage can run this task; the Auto Router chooses (metered)",
            "turn_settings": turn_for(excluded_providers=out_providers),
            "handoff": handoff, "receipt": receipt,
        }

    # 4. Explicitly authorized extra usage on a host the user is signed in to.
    for host in HOST_ORDER:
        if inventory.hosts.get(host) and host in authorized:
            return native(host, "extra usage authorized for this run", extra=True)

    return _blocked(receipt, [], inventory)


def _blocked(receipt: dict[str, Any], reasons: list[str], inventory: Inventory) -> dict[str, Any]:
    steps = list(reasons)
    if not any(inventory.hosts.values()):
        steps.append("install and sign in to the Claude Code or Codex CLI")
    for host in HOST_ORDER:
        if inventory.hosts.get(host) and inventory.host_usage.get(host) == "extra-usage":
            steps.append(f"{host} is in extra usage; authorize it for this run or wait for the allowance to reset")
    if not inventory.openrouter:
        steps.append("optional: add an OpenRouter key (setup wizard, hidden prompt) to use the Auto Router")
    return {
        "action": "blocked",
        "metered": False,
        "reason": "no included usage and no authorized metered route",
        "next_steps": steps,
        "receipt": receipt,
    }


# --------------------------------------------------------------------------
# Coordinator-led staffing (no OpenRouter): the session's own model chooses
# --------------------------------------------------------------------------

STAFFING_INSTRUCTIONS = (
    "No Auto Router is available, so the coordinating model chooses. Pick the "
    "cheapest candidate that can do this task well: cheap tier for mechanical "
    "work, medium for ordinary coding and review, high only for hard design, "
    "risky or ambiguous work. Prefer the default unless the task clearly calls "
    "for another candidate. State the choice and one-line reason; "
    "validate it with `--choose <model>`. Never choose a model that is not listed."
)


def staffing_candidates(
    inventory: Inventory, plans: Sequence[policy.DeclaredPlan], now: float
) -> list[dict[str, Any]]:
    """Every native model the user can run on included usage, with its notes."""
    usable = {
        host for host in HOST_ORDER
        if inventory.hosts.get(host)
        and any(p.provider == host and p.usable_included(now) for p in plans)
    }
    return [
        {"model": model, **{k: v for k, v in profile.items()}}
        for model, profile in MODEL_PROFILES.items()
        if profile["host"] in usable
    ]


def validate_choice(decision: Mapping[str, Any], model: str) -> dict[str, Any]:
    """Accept the coordinator's staffing choice only if it was a listed candidate."""
    staffing = decision.get("staffing")
    if not isinstance(staffing, Mapping):
        raise AutoRouteError("decision has no coordinator staffing to validate")
    for candidate in staffing["candidates"]:
        if candidate["model"] == model:
            return {
                "action": "native", "host": candidate["host"], "model": model,
                "metered": False, "reason": "coordinator choice among included-usage candidates",
                "receipt": decision["receipt"],
            }
    raise AutoRouteError(f"{model!r} is not an eligible candidate for this user")


# --------------------------------------------------------------------------
# Selection probe (metered, tiny, injectable)
# --------------------------------------------------------------------------

Transport = Callable[[Mapping[str, Any], Mapping[str, str]], Mapping[str, Any]]


def build_probe_body(turn_settings: Mapping[str, Any], task_digest: str) -> dict[str, Any]:
    """Auto Router request body for a tiny selection probe.

    Only the task digest (already de-identified by the caller, truncated here)
    leaves the machine. The probe exists to learn which model Auto would
    serve; the real work then runs through the exact route chosen from that.
    """
    turn = policy.parse_turn_settings(dict(turn_settings))
    plugin: dict[str, Any] = {"id": "auto-router", "cost_tier": turn.cost_tier}
    if turn.allowed_models:
        plugin["allowed_models"] = list(turn.allowed_models)
    if turn.excluded_models:
        plugin["excluded_models"] = list(turn.excluded_models)
    provider: dict[str, Any] = {}
    if turn.require_zdr:
        provider["zdr"] = True
        provider["data_collection"] = "deny"
    if turn.max_price:
        provider["max_price"] = dict(turn.max_price)
    body: dict[str, Any] = {
        "model": policy.AUTO_MODEL,
        "messages": [{"role": "user", "content": task_digest[:PROBE_DIGEST_MAX_CHARS]}],
        "plugins": [plugin],
        "session_id": turn.session_id,
        "max_tokens": PROBE_MAX_TOKENS,
        "usage": {"include": True},
    }
    if provider:
        body["provider"] = provider
    return body


def probe_auto_router(
    decision: Mapping[str, Any],
    task_digest: str,
    *,
    read_key: Callable[[], str],
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Run the selection probe and resolve the served model to a route.

    Works on an ``auto`` decision or on a ``native`` decision that carries a
    ``selection`` block (Auto picking inside the providers with included
    usage). Returns ``{"served_model", "cost_usd", "route"}`` where ``route`` is
    a native hand-off, a Devin equivalent, or an exact OpenRouter model. Raises
    :class:`AutoRouteError` on a refused answer; never retries or substitutes.
    """
    selection = decision.get("selection") if decision.get("action") == "native" else decision
    if not selection or not selection.get("turn_settings"):
        raise AutoRouteError("probe requires an 'auto' decision or a native decision with a selection block")
    turn_context = selection["turn_settings"]
    body = build_probe_body(turn_context, task_digest)
    send = transport or _urllib_transport(read_key)
    response = send(body, {"X-OpenRouter-Metadata": "enabled"})
    served = response.get("model")
    turn = policy.parse_turn_settings(dict(turn_context))
    if not policy.model_allowed(served, turn.allowed_models, turn.excluded_models):
        raise AutoRouteError("auto router served a model outside the requested pool")
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    cost = usage.get("cost")
    return {
        "served_model": served,
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
        "route": resolve_served(selection.get("handoff") or {}, served),
    }


def _urllib_transport(read_key: Callable[[], str]) -> Transport:
    import urllib.request

    def send(body: Mapping[str, Any], headers: Mapping[str, str]) -> Mapping[str, Any]:
        request = urllib.request.Request(
            OPENROUTER_CHAT_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + read_key(),
                "Content-Type": "application/json",
                **headers,
            },
        )
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as handle:
            return json.loads(handle.read(1_000_000))

    return send


# --------------------------------------------------------------------------
# Difficulty rating (Jev typed decision, a few thousandths of a cent)
# --------------------------------------------------------------------------

JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
TIER_CRITERIA = {
    "low": "Mechanical or small and well specified: a rename, a one-file fix, boilerplate, a lookup or summary",
    "medium": "Ordinary engineering: a typical feature or bug across a few files with clear requirements",
    "high": "Hard: multi-file design, real ambiguity, tricky concurrency, security or data-safety risk",
    "max": "Hardest: architecture or safety-critical work that needs the very strongest model's judgment",
}


def tier_from_words(query: str) -> str | None:
    """A tier the request itself asks for ("use the best model", "cheapest"), else None."""
    text = query if isinstance(query, str) else ""
    if policy._MAX_TIER_RE.search(text):
        return "max"
    if policy._CHEAP_TIER_RE.search(text):
        return "low"
    return None


def rate_difficulty(
    task_digest: str,
    *,
    read_key: Callable[[], str],
    transport: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rate how hard the task is so the cheapest model that can do it well is used.

    Returns ``{"tier", "confidence", "cost_usd"}``. Only the (de-identified,
    truncated) digest leaves the machine. Never retried; a refused or malformed
    answer raises :class:`AutoRouteError` and the caller falls back to the default tier.
    """
    payload = {
        "model": JEV_MODEL,
        "state": {"task": task_digest[:PROBE_DIGEST_MAX_CHARS]},
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": "How demanding is this software task? Choose the lowest tier whose model "
                "could still do it well, because a stronger model costs much more.",
                "criteria": TIER_CRITERIA,
            }
        },
    }
    if transport is None:
        import urllib.request

        def transport(body: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: F811
            request = urllib.request.Request(
                JEV_ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + read_key()},
            )
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as handle:
                return json.loads(handle.read(1_000_000))

    response = transport(payload)
    answer = (response.get("answers") or {}).get("tier") or {}
    tier = answer.get("choice")
    if tier not in TIER_CRITERIA:
        raise AutoRouteError("difficulty rating returned no valid tier")
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    return {"tier": tier, "confidence": answer.get("confidence"),
            "cost_usd": usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", default=None, help="task description (used for cost tier only; never sent anywhere)")
    parser.add_argument("--nodes", type=Path, default=None,
                        help="JSON list of {id, task, pin?}: route every node of a task graph on its own "
                        "(own difficulty rating, own pick), instead of one decision for the whole job")
    parser.add_argument("--pin", default=None, help="explicit model id; wins over everything")
    parser.add_argument("--authorize-extra", action="append", default=[], choices=HOST_ORDER,
                        help="allow extra (metered) usage on this host for this run")
    parser.add_argument("--unknown-usage", choices=("included", "excluded"), default="included")
    parser.add_argument("--plans", type=Path, default=None, help="declared-plans JSON (see config/examples)")
    parser.add_argument("--probe", action="store_true",
                        help="rate the task's difficulty and run the Auto Router probe (needs an OpenRouter key; "
                        "sends the task text, truncated, to OpenRouter and Jev: use a de-identified description)")
    parser.add_argument("--choose", default=None, metavar="MODEL",
                        help="validate the coordinator's staffing choice against the listed candidates")


def route_task(
    task: str,
    *,
    inventory: Inventory,
    plans: Sequence[policy.DeclaredPlan] | None,
    read_key: Callable[[], str],
    pin: str | None = None,
    authorize_extra: Iterable[str] = (),
    unknown_usage: str = "included",
    probe: bool = False,
) -> dict[str, Any]:
    """One routed decision for one task (a whole job, or one node of its task graph)."""
    tier = tier_from_words(task)
    rated = None
    if probe and inventory.openrouter and not pin and tier is None:
        try:
            rated = rate_difficulty(task, read_key=read_key)
            tier = rated["tier"]
        except (AutoRouteError, OSError, ValueError) as exc:
            print(f"note: difficulty rating unavailable ({type(exc).__name__}); using the default tier", file=sys.stderr)
    decision = decide(
        inventory, task, pinned_model=pin, authorize_extra_hosts=authorize_extra,
        plans=plans or None, unknown_usage=unknown_usage, tier_override=tier,
    )
    if rated:
        decision["receipt"]["difficulty_rating"] = rated
    if probe and (decision.get("selection") or decision["action"] == "auto"):
        try:
            decision["probe"] = probe_auto_router(decision, task, read_key=read_key)
        except (AutoRouteError, OSError, ValueError) as exc:
            print(f"note: Auto Router probe unavailable ({type(exc).__name__}); using the decision as is", file=sys.stderr)
    return decision


def load_nodes(path: Path) -> list[dict[str, Any]]:
    nodes = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(nodes, list) or not nodes:
        raise AutoRouteError("nodes file must be a non-empty JSON list")
    seen = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not isinstance(node.get("task"), str):
            raise AutoRouteError("each node needs a string id and task")
        if node["id"] in seen:
            raise AutoRouteError(f"duplicate node id {node['id']!r}")
        seen.add(node["id"])
    return nodes


def run(args: argparse.Namespace, inventory: Inventory | None = None,
        read_key: Callable[[], str] | None = None) -> int:
    if bool(args.task) == bool(args.nodes):
        print("error: give exactly one of --task or --nodes", file=sys.stderr)
        return 2
    inventory = inventory or build_inventory()
    plans = policy.load_declared_plans(args.plans) if args.plans else None
    if read_key is None:
        from side_lane import credentials, model_select

        def read_key() -> str:
            return credentials.read_credential(model_select.OPENROUTER_CREDENTIAL_SERVICE)

    common = dict(inventory=inventory, plans=plans, read_key=read_key, authorize_extra=args.authorize_extra,
                  unknown_usage=args.unknown_usage, probe=args.probe)
    if args.nodes:
        try:
            nodes = load_nodes(args.nodes)
        except (AutoRouteError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        routed = [{"id": n["id"], "decision": route_task(n["task"], pin=n.get("pin"), **common)} for n in nodes]
        print(json.dumps({"nodes": routed}, indent=2, sort_keys=True))
        return 3 if any(r["decision"]["action"] == "blocked" for r in routed) else 0
    decision = route_task(args.task, pin=args.pin, **common)
    if args.choose:
        try:
            decision = validate_choice(decision, args.choose)
        except AutoRouteError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0 if decision["action"] != "blocked" else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m side_lane.auto_route")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
