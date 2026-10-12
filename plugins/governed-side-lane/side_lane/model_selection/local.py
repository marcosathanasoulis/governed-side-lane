"""Local adapter: build the shared request from this machine and run ``select``.

Used by the public ``side-lane select`` command and by the private wrapper
(``scripts/side_lane_select.py``), which adds live plan probes. It reads no
credential: bindings are opaque references for CLIs that are signed in and for
keys that are present. The routing rules live in ``selector``, not here.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import selector
from .contract import ContractError, Request
from .rotation import JsonFileState, SpreadState
from .tiers import load

PROBE_DETAIL_STATES = (("login needed", "auth_failed"), ("timeout", "transient"), ("http 5", "transient"),
                       ("probe error", "transient"), ("concurrency", "transient"))


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "side-lane" / "spread.json"


def owner_id() -> str:
    return os.environ.get("SIDE_LANE_OWNER") or getpass.getuser()


def bindings_for(hosts: Mapping[str, bool], owner: str, *, openrouter: bool = False,
                 extra_providers: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Opaque references. A host is bound only when its CLI is present and signed in."""
    out = [{"ref": f"local-oauth:{h}", "provider": h, "owner_id": owner} for h, ok in sorted(hosts.items()) if ok]
    out += [{"ref": f"local-key:{p}", "provider": p, "owner_id": owner} for p in extra_providers]
    if openrouter:
        out.append({"ref": "local-key:openrouter", "provider": "openrouter"})
    return out


def availability_from_usage(host_usage: Mapping[str, str], hosts: Mapping[str, bool], now: float,
                            *, unknown_usage: str = "included") -> list[dict[str, Any]]:
    """Declared usage as observations. Public default: a signed-in host with no declaration is included."""
    out = []
    for h, ok in sorted(hosts.items()):
        if not ok:
            continue
        usage = host_usage.get(h, "unknown")
        if usage == "included-oauth":
            state, src = "available", "declared-usage"
        elif usage == "extra-usage":
            state, src = "exhausted", "declared-usage"
        elif unknown_usage == "included":
            state, src = "available", "assumed-included"
        else:
            state, src = "unknown", "no-declaration"
        out.append({"provider": h, "state": state, "checked_at": now, "source": src})
    return out


def availability_from_probes(probes: Mapping[str, Mapping[str, Any]], now: float) -> list[dict[str, Any]]:
    """Map plan-probe results onto the five states (login needed is auth_failed; timeouts and any other
    failed probe are transient). A failed live probe is not routable this run (plan_probe treats it as
    unavailable), so it is never forwarded as ``unknown``; the detail stays in ``source`` as evidence."""
    out = []
    for plan, r in sorted(probes.items()):
        state = r.get("state", "unknown")
        detail = str(r.get("detail", ""))
        if state == "absent":
            continue
        if state == "unknown":
            state = next((s for needle, s in PROBE_DETAIL_STATES if needle in detail), "transient")
        obs = {"provider": plan, "state": state, "checked_at": float(r.get("checked_at", now)),
               "source": f"plan-probe:{detail[:40]}", "live": True}
        reset = r.get("reset_at")
        if state == "exhausted" and isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
            obs["reset_at"] = float(reset)  # usage returns at this time (e.g. Retry-After, or the Codex window reset)
        out.append(obs)
    return out


def signed_in_hosts(hosts: Mapping[str, bool], status_fn: Any = None) -> dict[str, bool]:
    """Executable presence is not a sign-in: keep a host only when its own auth preflight says ready.
    A failing or unavailable status check means not bound this run (never an error, never a model call)."""
    if status_fn is None:
        from side_lane import auth, hosts as host_lookup

        def status_fn(host: str) -> bool:  # type: ignore[misc]
            executable = host_lookup.resolve_host_executable(host)
            return executable is not None and auth.auth_status(host, executable=str(executable)).state == "ready"

    out: dict[str, bool] = {}
    for host, present in hosts.items():
        try:
            out[host] = bool(present) and bool(status_fn(host))
        except Exception:  # noqa: BLE001 - a broken status check just means this host is not bound
            out[host] = False
    return out


GLM_SERVICE = "governed-side-lane-glm"
#: Providers whose paid-by-default route an owner can declare included (kept equal to the tier table by a test).
DECLARABLE_PLANS = ("glm", "gemini")


def _pin_only_bindings(hosts: Mapping[str, bool], extra_providers: Sequence[str]) -> tuple[dict[str, bool], tuple[str, ...]]:
    """Bindings that exist only so an explicit pin can reach them: Gemini (executable present; its auth is
    checked when it runs) and a stored direct GLM key (presence only). The selector keeps both out of
    unpinned selection (Gemini is plan-unverified, GLM is metered and explicit-only)."""
    out = dict(hosts)
    extra = list(extra_providers)
    try:
        from side_lane import credentials, hosts as host_lookup

        if host_lookup.resolve_host_executable("gemini") is not None:
            out["gemini"] = True
        if "glm" not in extra and credentials.credential_present(GLM_SERVICE):
            extra.append("glm")
    except Exception:  # noqa: BLE001 - discovery failure just means no pin-only binding
        pass
    return out, tuple(extra)


def _extra_availability(extra_providers: Sequence[str], host_usage: Mapping[str, str], seen: set[str],
                        now: float) -> list[dict[str, Any]]:
    """Observations for bound non-CLI providers (GLM) that nothing probed: the owner's declaration is the
    evidence (included-oauth is available, extra-usage is exhausted); with no declaration it is unknown."""
    out = []
    for p in extra_providers:
        if p in seen:
            continue
        usage = host_usage.get(p, "unknown")
        state, src = (("available", "declared-usage") if usage == "included-oauth"
                      else ("exhausted", "declared-usage") if usage == "extra-usage" else ("unknown", "no-probe"))
        out.append({"provider": p, "state": state, "checked_at": now, "source": src})
    return out


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", help="one-line objective (never sent anywhere by this command)")
    parser.add_argument("--nodes", type=Path, help="JSON list of {id, task, kind?, tier?, pin_provider?, pin_model?}")
    parser.add_argument("--kind", choices=("mechanical", "trivial", "service_ops", "routine", "coding", "debugging", "cross_system"))
    parser.add_argument("--tier", choices=("low", "medium", "high", "xhigh"))
    parser.add_argument("--thread-id", help="reuse for follow-ups so the route and owner stay put")
    parser.add_argument("--previous", type=Path, help="JSON file holding the earlier selection for this thread")
    parser.add_argument("--task-changed", action="store_true", help="the task materially changed; reselect")
    parser.add_argument("--pin-provider")
    parser.add_argument("--pin-model")
    parser.add_argument("--allow-metered", dest="allow_metered", action="store_true", default=None)
    parser.add_argument("--no-metered", dest="allow_metered", action="store_false")
    parser.add_argument("--unknown-usage", choices=("included", "excluded"), default="included")
    parser.add_argument("--no-commit", action="store_true", help="do not advance the spread counter")


def included_plans_from(host_usage: Mapping[str, str]) -> list[str]:
    """Owner-declarable routes (GLM) the owner has declared included: ``prefs set-usage --host glm --state included-oauth``
    for a public user, or an organization's private policy in its own wrapper. Everything else stays as catalogued."""
    return sorted(h for h, u in host_usage.items() if h in DECLARABLE_PLANS and u == "included-oauth")


def _request(args: argparse.Namespace, *, task: str, thread_id: str, kind: str | None, tier: str | None,
             pin_provider: str | None, pin_model: str | None, previous: Mapping[str, Any] | None,
             bindings: list[dict[str, Any]], availability: list[dict[str, Any]], allow_metered: bool,
             included_plans: Sequence[str] = ()) -> Request:
    pref = {k: v for k, v in (("provider", pin_provider), ("model", pin_model)) if v}
    raw: dict[str, Any] = {
        "caller": "side-lane-local", "thread_id": thread_id, "owner": {"id": owner_id(), "kind": "human"},
        "task": {"objective": task, **({"kind": kind} if kind else {})}, "tier": tier, "preference": pref,
        "bindings": bindings, "availability": availability, "allow_metered": allow_metered,
        "previous_selection": previous, "task_changed": args.task_changed, "included_plans": list(included_plans),
    }
    return Request.parse({k: v for k, v in raw.items() if v is not None})


def load_inputs(args: argparse.Namespace, thread: str) -> tuple[list[tuple], dict[str, Any], Any, str]:
    """Pure input loading and validation (reads only the named files; no probes, no credentials, no state).
    Returns ``(items, prior_by_node, previous, thread)``; raises ContractError/OSError/ValueError."""
    previous = json.loads(args.previous.read_text()) if args.previous else None
    prior_by_node: dict[str, Any] = {}
    if args.nodes:
        nodes = json.loads(args.nodes.read_text())
        if not isinstance(nodes, list) or not nodes:
            raise ContractError("nodes file must be a non-empty list of {id, task}")
        ids: set[str] = set()
        for n in nodes:
            if not (isinstance(n, dict) and isinstance(n.get("id"), str) and n["id"].strip()
                    and isinstance(n.get("task"), str) and n["task"].strip()):
                raise ContractError("each node needs a non-empty string id and task")
            if n["id"] in ids:
                raise ContractError(f"duplicate node id {n['id']!r}")
            ids.add(n["id"])
        if previous is not None:
            if not isinstance(previous, dict) or not isinstance(previous.get("nodes"), list):
                raise ContractError("--previous with --nodes must be the earlier graph output ({thread_id, nodes})")
            prior_by_node = {p.get("id"): p.get("selection") for p in previous["nodes"] if isinstance(p, dict)}
            if not args.thread_id and isinstance(previous.get("thread_id"), str):
                thread = previous["thread_id"]
        items = [(n["id"], n["task"], n.get("kind") or args.kind, n.get("tier") or args.tier,
                  n.get("pin_provider") or args.pin_provider, n.get("pin_model") or args.pin_model) for n in nodes]
    else:
        items = [(None, args.task, args.kind, args.tier, args.pin_provider, args.pin_model)]
    return items, prior_by_node, previous, thread


def validate_inputs(args: argparse.Namespace) -> str | None:
    """Everything that can be checked without a probe: exactly one input mode, readable well-formed files,
    and every node's request fields. Returns an error message, or None when the input is acceptable. The
    private wrapper calls this before any live probe so bad input never causes an external call."""
    if bool(args.task) == bool(args.nodes):
        return "give exactly one of --task or --nodes"
    try:
        items, prior_by_node, previous, thread = load_inputs(args, args.thread_id or "validate")
        for node_id, task, kind, tier, pp, pm in items:
            _request(args, task=task, thread_id=f"{thread}:{node_id}" if node_id else thread, kind=kind, tier=tier,
                     pin_provider=pp, pin_model=pm, previous=prior_by_node.get(node_id) if node_id else previous,
                     bindings=[], availability=[], allow_metered=False)
    except (ContractError, OSError, ValueError) as exc:
        return str(exc)
    return None


def run(args: argparse.Namespace, *, hosts: Mapping[str, bool] | None = None,
        host_usage: Mapping[str, str] | None = None, probes: Mapping[str, Mapping[str, Any]] | None = None,
        openrouter: bool | None = None, extra_providers: Sequence[str] = (), allow_metered_default: bool = False,
        now: float | None = None, state: SpreadState | None = None, out=None) -> int:
    out = out or sys.stdout
    if bool(args.task) == bool(args.nodes):
        print("error: give exactly one of --task or --nodes", file=sys.stderr)
        return 2
    now = time.time() if now is None else now
    if hosts is None or openrouter is None or host_usage is None:
        from side_lane import auto_route

        autodetected = hosts is None
        inv = auto_route.build_inventory(hosts=hosts, host_usage=host_usage, openrouter=openrouter)
        hosts, host_usage, openrouter = inv.hosts, inv.host_usage, inv.openrouter
        if autodetected:  # detected hosts are executables only; require a ready sign-in before binding
            hosts = signed_in_hosts(hosts)
            hosts, extra_providers = _pin_only_bindings(hosts, extra_providers)
    owner = owner_id()
    bindings = bindings_for(hosts, owner, openrouter=bool(openrouter), extra_providers=extra_providers)
    if probes is not None:
        availability = availability_from_probes(probes, now)
        known = {a["provider"] for a in availability}
        availability += [a for a in availability_from_usage(host_usage, hosts, now, unknown_usage="excluded")
                         if a["provider"] not in known]
    else:
        availability = availability_from_usage(host_usage, hosts, now, unknown_usage=args.unknown_usage)
    availability += _extra_availability(extra_providers, host_usage, {a["provider"] for a in availability}, now)
    allow_metered = allow_metered_default if args.allow_metered is None else args.allow_metered
    table = load()
    state = state if state is not None else JsonFileState(default_state_path())
    thread = args.thread_id or f"local-{uuid.uuid4().hex[:12]}"
    try:
        items, prior_by_node, previous, thread = load_inputs(args, thread)
        requests = []  # build and validate every request first: bad input must not leave partial routing state
        for node_id, task, kind, tier, pp, pm in items:
            tid = f"{thread}:{node_id}" if node_id else thread
            requests.append((node_id, _request(
                args, task=task, thread_id=tid, kind=kind, tier=tier, pin_provider=pp, pin_model=pm,
                previous=prior_by_node.get(node_id) if node_id else previous, bindings=bindings,
                availability=availability, allow_metered=allow_metered,
                included_plans=included_plans_from(host_usage))))
        results = []
        for node_id, req in requests:
            sel = selector.select(req, table, now=now, state=state, commit=not args.no_commit)
            results.append((node_id, sel.to_dict()))
    except (ContractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.nodes:
        payload: Any = {"thread_id": thread, "nodes": [{"id": i, "selection": s} for i, s in results]}
    else:
        payload = results[0][1]
    print(json.dumps(payload, indent=2, sort_keys=True), file=out)
    return 0 if all(s["status"] == "selected" for _, s in results) else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m side_lane.model_selection.local")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
