"""``side-lane receipt``: record what actually ran, next to the selection that chose it.

A selection is a decision, not proof of execution, and a successful sign-in is not proof of included
billing. This command builds an :class:`ExecutionReceipt` from a selection file plus the facts the
execution adapter observed, validates it against the contract, and prints it (optionally appending one
line to a receipts file). Billing evidence defaults to ``unverified``; the caller states ``included`` or
``metered`` only when it has evidence. Usage is a small value-free object of numbers and short strings:
anything that looks like a credential is refused, so a receipt never carries a secret.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import (AUTO_MODELS, BILLING_EVIDENCE, ROUTES, ContractError, ExecutionReceipt, is_served_model_id,
                       parse_previous)

#: Credential-like names. ``token`` is refused but ``tokens`` is not: input_tokens/output_tokens are the usage
#: counts this field exists to carry.
SECRET_KEY = re.compile(r"key|token(?!s)|secret|passw|authorization|bearer|credential|cookie", re.IGNORECASE)
#: Explicit credential shapes: common key prefixes and bearer/authorization/password forms.
CREDENTIAL_SHAPE = re.compile(
    r"(sk-|ghp_|gho_|xox[bp]-|AKIA|AIza)[A-Za-z0-9_-]{6,}|bearer\s|authorization\s*:|passw(or)?d\s*[=:]",
    re.IGNORECASE)
#: Free text and usage values: those shapes, or one long unbroken token-like string, even inside prose.
TOKEN_LIKE = re.compile(
    CREDENTIAL_SHAPE.pattern + r"|(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])", re.IGNORECASE)
#: A model identity (native id or OpenRouter ``vendor/name``) up to the contract's 128 characters. It is checked
#: against the explicit credential shapes, not the generic long-token rule, so a long legitimate id still passes.
MODEL_IDENTITY = re.compile(r"^[A-Za-z0-9._:~+/-]{1,128}$")
SELECTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INT_LIMIT = 2 ** 63
MAX_USAGE_ITEMS = 20
MAX_STRING = 80


def clean_usage(raw: Any) -> dict[str, Any]:
    """Observed usage: a flat object of numbers, booleans and short strings. No nesting, no secrets."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ContractError("observed usage must be an object")
    if len(raw) > MAX_USAGE_ITEMS:
        raise ContractError(f"observed usage has more than {MAX_USAGE_ITEMS} fields")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        # A rejected key is never echoed: it may itself be the secret.
        if (not isinstance(k, str) or not k or len(k) > 64 or SECRET_KEY.search(k) or TOKEN_LIKE.search(k)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", k)):
            raise ContractError("observed usage has a field name that is not allowed (credential-like, malformed or empty)")
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            if not -INT_LIMIT <= v < INT_LIMIT:  # also keeps huge ints away from float conversion (OverflowError)
                raise ContractError(f"observed usage field {k!r} is out of range")
            out[k] = v
        elif isinstance(v, float):
            if not math.isfinite(v):
                raise ContractError(f"observed usage field {k!r} must be a finite number")
            out[k] = v
        elif isinstance(v, str) and len(v) <= MAX_STRING and not TOKEN_LIKE.search(v):
            out[k] = v
        else:
            raise ContractError(f"observed usage field {k!r} must be a number, boolean or short plain string")
    return out


def plain_text(value: Any, name: str) -> Any:
    """Free-text receipt fields (reason, effort, identities) must be short and must not look like a credential."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200 or TOKEN_LIKE.search(value):
        raise ContractError(f"{name} must be short plain text and must not look like a credential")
    return value


def model_text(value: Any, name: str) -> str:
    """A model or provider identity: bounded characters, and never one of the explicit credential shapes."""
    if not isinstance(value, str) or not MODEL_IDENTITY.match(value) or CREDENTIAL_SHAPE.search(value):
        raise ContractError(f"{name} must be a plain identifier and must not look like a credential")
    return value


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON for a receipt")


def build(selection: Mapping[str, Any], *, provider: str, model: str, route: str, success: bool,
          billing: str = "unverified", usage: Any = None, switch_reason: str | None = None,
          effort: str | None = None) -> ExecutionReceipt:
    """The validated receipt for one run. The selected route is copied from the (validated) selection."""
    parse_previous(selection)  # the same structural validation a follow-up applies to a prior selection
    if selection.get("status") != "selected":
        raise ContractError("a receipt needs a selection with status 'selected'")
    provider, model = model_text(provider, "provider"), model_text(model, "model")
    switch_reason, effort = plain_text(switch_reason, "switch_reason"), plain_text(effort, "effort")
    # Everything copied from the selection is untrusted too: the file may be tampered with or hand-written.
    selected_provider, selected_model = model_text(selection["provider"], "selected provider"), model_text(selection["model"], "selected model")
    selected_effort = plain_text(selection.get("effort"), "selected effort")
    selection_id = selection.get("selection_id")
    if not isinstance(selection_id, str) or not SELECTION_ID.match(selection_id) or CREDENTIAL_SHAPE.search(selection_id):
        raise ContractError("selection_id must be a short plain identifier")
    # OpenRouter Auto: what ran is the served model, which can never equal "openrouter/auto". It is the same
    # selected route when it stays on OpenRouter and the served model is inside the allowed set that was sent.
    meta = selection.get("openrouter")
    if meta is not None and not isinstance(meta, Mapping):
        raise ContractError("selection.openrouter must be an object")
    allowed = (meta or {}).get("allowed_models")
    allow_list_ok = (isinstance(allowed, list) and bool(allowed)
                     and all(isinstance(p, str) and p.strip() for p in allowed))  # fail closed on a malformed list
    auto = (selection.get("route") == "metered_openrouter" and selection.get("model") in AUTO_MODELS
            and route == "metered_openrouter" and provider == selection.get("provider")
            and is_served_model_id(model) and model not in AUTO_MODELS
            and allow_list_ok and any(fnmatchcase(model, pat) for pat in allowed))
    receipt = ExecutionReceipt(
        selection_id=selection_id, provider=provider, model=model, route=route,
        success=success, billing_evidence=billing, observed_usage=clean_usage(usage), switch_reason=switch_reason,
        auto_resolved=auto,
        selected_provider=selected_provider, selected_model=selected_model, selected_route=selection["route"],
        selected_tier=selection.get("tier"), selected_effort=selected_effort, effort=effort)
    return receipt.validate()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--selection", type=Path, required=True,
                        help="the selection JSON (a single selection, or one node's selection from graph output)")
    parser.add_argument("--node-id", help="pick this node's selection out of graph output ({thread_id, nodes})")
    parser.add_argument("--provider", required=True, help="the provider that actually ran")
    parser.add_argument("--model", required=True, help="the concrete model that actually ran")
    parser.add_argument("--route", required=True, choices=ROUTES, help="the route that actually ran")
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--success", dest="success", action="store_true", default=None)
    outcome.add_argument("--failure", dest="success", action="store_false")
    parser.add_argument("--billing", choices=BILLING_EVIDENCE, default="unverified",
                        help="billing evidence you actually have; a successful sign-in is not evidence")
    parser.add_argument("--usage", help="observed usage as a small JSON object of numbers and short strings")
    parser.add_argument("--switch-reason", help="required when what ran differs from what was selected")
    parser.add_argument("--effort", help="the effort setting that actually ran")
    parser.add_argument("--append", type=Path, help="append the receipt as one JSON line to this file")


def _selection(args: argparse.Namespace) -> Mapping[str, Any]:
    data = json.loads(args.selection.read_text())
    if args.node_id:
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            raise ContractError("--node-id needs graph output ({thread_id, nodes})")
        match = [n.get("selection") for n in nodes if isinstance(n, dict) and n.get("id") == args.node_id]
        if len(match) != 1:
            raise ContractError(f"node {args.node_id!r} not found exactly once in the graph output")
        return match[0]
    if isinstance(data, dict) and "nodes" in data and "selection_id" not in data:
        raise ContractError("graph output given without --node-id")
    return data


def run(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    try:
        usage = json.loads(args.usage, parse_constant=_reject_constant) if args.usage else None
        receipt = build(_selection(args), provider=args.provider, model=args.model, route=args.route,
                        success=bool(args.success), billing=args.billing, usage=usage,
                        switch_reason=args.switch_reason, effort=args.effort)
        line = json.dumps(receipt.to_dict(), sort_keys=True, allow_nan=False)
        if args.append:
            args.append.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.append, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            if os.name == "posix":  # the mode above only applies to a new file: tighten an existing one too
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(line + "\n")
    except (ContractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True, allow_nan=False), file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m side_lane.model_selection.receipt")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
