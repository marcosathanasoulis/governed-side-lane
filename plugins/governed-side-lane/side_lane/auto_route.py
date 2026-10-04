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
#: also picks a native model, and spending Opus-class included capacity on
#: every task wastes it, so the local default is ``medium``. Request words
#: ("use the best model", "use the cheapest model") still override it.
LOCAL_DEFAULT_COST_TIER = "medium"

#: Native model ladder per host and cost tier (ids match ``model_select``).
NATIVE_LADDER: Mapping[str, Mapping[str, str]] = {
    "claude": {
        "low": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-5",
        "high": "claude-opus-5",
        "xhigh": "claude-opus-5",
        "max": "claude-opus-5",
    },
    "codex": {
        "low": "gpt-6-luna",
        "medium": "gpt-6-astra",
        "high": "gpt-6-sol",
        "xhigh": "gpt-6-sol",
        "max": "gpt-6-sol",
    },
}
#: OpenRouter model patterns that belong to each host's plan.
HOST_MODEL_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "claude": ("anthropic/*",),
    "codex": ("openai/*",),
}
#: Planning notes the coordinating model reads when OpenRouter is not set up.
#: These are coarse priors for choosing staffing, not benchmarks; the user's
#: own experience and the task win over them.
MODEL_PROFILES: Mapping[str, Mapping[str, Any]] = {
    "claude-haiku-4-5-20251001": {
        "host": "claude", "tier": "low", "relative_cost": "lowest",
        "strengths": "fast, cheap; good for mechanical edits, lookups, summaries, simple tests",
        "weaknesses": "weaker on multi-file design, subtle bugs, long-horizon agentic work",
    },
    "claude-sonnet-5": {
        "host": "claude", "tier": "medium", "relative_cost": "moderate",
        "strengths": "strong everyday coding, tool use and review; best default balance",
        "weaknesses": "can miss deep architectural trade-offs that the top model catches",
    },
    "claude-opus-5": {
        "host": "claude", "tier": "high", "relative_cost": "highest",
        "strengths": "best for hard design, risky refactors, security-sensitive or ambiguous work",
        "weaknesses": "uses included allowance fastest; overkill for routine tasks",
    },
    "gpt-6-luna": {
        "host": "codex", "tier": "low", "relative_cost": "lowest",
        "strengths": "fast and cheap; good for small scripted changes and boilerplate",
        "weaknesses": "limited depth on complex reasoning and unfamiliar codebases",
    },
    "gpt-6-astra": {
        "host": "codex", "tier": "medium", "relative_cost": "moderate",
        "strengths": "solid general coding and repo-wide edits; good test-writing",
        "weaknesses": "less careful than the top model on ambiguous requirements",
    },
    "gpt-6-sol": {
        "host": "codex", "tier": "high", "relative_cost": "highest",
        "strengths": "strongest Codex model for hard implementation and debugging",
        "weaknesses": "highest burn of the included plan; overkill for routine work",
    },
}
HOST_ORDER = ("claude", "codex")
USAGE_STATES = ("included-oauth", "extra-usage", "unknown")


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
        hosts = model_select.detect_available_hosts()
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

    ``included-oauth`` is an included plan. ``extra-usage`` means the included
    allowance is spent, so the plan is exhausted (not runnable, excluded from
    Auto). ``unknown`` follows ``unknown_usage``: the public default treats a
    host the user is signed in to as included; the private package passes
    ``excluded`` so an unattested host is never used silently.
    """
    if unknown_usage not in {"included", "excluded"}:
        raise AutoRouteError("unknown_usage must be 'included' or 'excluded'")
    plans = []
    for host in HOST_ORDER:
        if not inventory.hosts.get(host):
            continue
        state = inventory.host_usage.get(host, "unknown")
        if state == "included-oauth":
            quota = "declared"
        elif state == "extra-usage":
            quota = "exhausted"
        else:
            quota = "unknown" if unknown_usage == "included" else "exhausted"
        plans.append(
            policy.DeclaredPlan(
                provider=host,
                mode="included",
                quota_status=quota,
                model_patterns=HOST_MODEL_PATTERNS[host],
                route_ids=(f"native-{host}",),
            )
        )
    return tuple(plans)


def _host_for_model(model: str) -> str | None:
    if model.startswith("claude"):
        return "claude"
    if model.startswith("gpt-"):
        return "codex"
    return None


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
) -> dict[str, Any]:
    """Return the routing decision as a JSON-safe dict.

    ``action`` is one of ``native`` (run through that host's CLI, included
    usage), ``auto`` (ask the Auto Router; metered), ``pinned`` or
    ``blocked`` (nothing can run; ``next_steps`` says what to set up).
    """
    current = time.time() if now is None else now
    settings = settings or policy.AutoRouterSettings(default_cost_tier=LOCAL_DEFAULT_COST_TIER)
    plan_list = tuple(plans) if plans is not None else plans_for(inventory, unknown_usage=unknown_usage)
    authorized = set(authorize_extra_hosts)
    tier = policy.cost_tier_for(
        query, default=settings.default_cost_tier, complexity=settings.complexity_tiers
    )
    out_providers = policy.providers_out(plan_list, {}, (), now=current)
    receipt: dict[str, Any] = {
        "policy": POLICY_ID,
        "cost_tier": tier,
        "inventory": {
            "hosts": {h: bool(inventory.hosts.get(h)) for h in HOST_ORDER},
            "host_usage": dict(inventory.host_usage),
            "openrouter_key_present": inventory.openrouter,
        },
        "plans": policy.plans_summary(plan_list, current),
        "providers_out": list(out_providers),
        "authorized_extra_hosts": sorted(authorized),
    }

    def native(host: str, reason: str, extra: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": "native",
            "host": host,
            "model": NATIVE_LADDER[host][tier],
            "metered": extra,
            "reason": reason,
            "receipt": receipt,
        }
        if not extra:
            result["staffing"] = {
                "mode": "coordinator-choice",
                "default": result["model"],
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
            usable = any(p.provider == host and p.usable_included(current) for p in plan_list)
            if not usable and host not in authorized:
                return _blocked(
                    receipt,
                    [f"{host} has no included usage; authorize extra usage for this run to use {pinned_model}"],
                    inventory,
                )
            return {
                "action": "pinned", "host": host, "model": pinned_model,
                "metered": not usable, "reason": "explicit pin", "receipt": receipt,
            }
        if not inventory.openrouter:
            return _blocked(receipt, ["pinned OpenRouter model needs an OpenRouter key"], inventory)
        if not policy.SERVED_MODEL_RE.fullmatch(pinned_model):
            raise AutoRouteError("invalid pinned model id")
        return {
            "action": "pinned", "host": "openrouter", "model": pinned_model,
            "metered": True, "reason": "explicit pin", "receipt": receipt,
        }

    # 2. Included OAuth usage first.
    for host in HOST_ORDER:
        if inventory.hosts.get(host) and any(
            p.provider == host and p.usable_included(current) for p in plan_list
        ):
            return native(host, "included usage available; no metered call needed")

    # 3. Auto Router, metered. Providers whose included usage is out are excluded.
    if inventory.openrouter:
        turn = policy.build_turn_settings(
            settings,
            thread=("local", session_key),
            query=query,
            granted_capabilities=(),
            service_names=service_names,
            plans=plan_list,
            excluded_providers=out_providers,
        )
        return {
            "action": "auto",
            "metered": True,
            "reason": "no included usage can run this task; the Auto Router chooses (metered)",
            "turn_settings": turn.to_context(),
            "receipt": receipt,
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

    Returns ``{"served_model", "cost_usd", "route"}`` where ``route`` is a
    native hand-off (the served model belongs to a host with included usage)
    or an exact OpenRouter model. Raises :class:`AutoRouteError` on a refused
    answer; never retries or substitutes another route.
    """
    if decision.get("action") != "auto":
        raise AutoRouteError("probe requires an 'auto' decision")
    turn_context = decision["turn_settings"]
    body = build_probe_body(turn_context, task_digest)
    headers = {"X-OpenRouter-Metadata": "enabled"}
    send = transport or _urllib_transport(read_key)
    response = send(body, headers)
    served = response.get("model")
    turn = policy.parse_turn_settings(dict(turn_context))
    if not policy.model_allowed(served, turn.allowed_models, turn.excluded_models):
        raise AutoRouteError("auto router served a model outside the requested pool")
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    cost = usage.get("cost")
    route: dict[str, Any] = {"action": "openrouter-exact", "model": served}
    handoff = policy.native_handoff_route(turn_context, served, list((turn_context.get("included_native") or {})))
    if handoff:
        route = {"action": "native-handoff", "route_id": handoff, "model": served}
    return {"served_model": served, "cost_usd": cost if isinstance(cost, (int, float)) else None, "route": route}


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
# CLI
# --------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", required=True, help="task description (used for cost tier only; never sent anywhere)")
    parser.add_argument("--pin", default=None, help="explicit model id; wins over everything")
    parser.add_argument("--authorize-extra", action="append", default=[], choices=HOST_ORDER,
                        help="allow extra (metered) usage on this host for this run")
    parser.add_argument("--unknown-usage", choices=("included", "excluded"), default="included")
    parser.add_argument("--plans", type=Path, default=None, help="declared-plans JSON (see config/examples)")
    parser.add_argument("--choose", default=None, metavar="MODEL",
                        help="validate the coordinator's staffing choice against the listed candidates")


def run(args: argparse.Namespace, inventory: Inventory | None = None) -> int:
    inventory = inventory or build_inventory()
    plans = policy.load_declared_plans(args.plans) if args.plans else None
    decision = decide(
        inventory, args.task, pinned_model=args.pin,
        authorize_extra_hosts=args.authorize_extra, plans=plans or None,
        unknown_usage=args.unknown_usage,
    )
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
