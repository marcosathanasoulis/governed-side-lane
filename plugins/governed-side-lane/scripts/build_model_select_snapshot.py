#!/usr/bin/env python3
"""Build the frozen, de-identified public model-select snapshot.

This script reads a small, fixed allowlist of paths out of a read-only GCF
checkout at an exact pinned commit (via ``git show``) and this package's own
already-public native-host model inventory, and writes a de-identified
derived-knowledge snapshot to ``config/model-select-snapshot.json`` (or
``--out``).

No GCF private source code is copied into the snapshot: only model
identifiers, capability-support booleans, and a naming-convention quality
tier are retained, plus short, hand-written task-family descriptions and
Jev decision summaries built from the allowlisted fields below. Prices are
never frozen here -- the scorer either fetches them live from OpenRouter's
public models API at run time, or leaves them unknown.

Usage:
    python3 scripts/build_model_select_snapshot.py \
        --gcf-repo /path/to/selector-repo \
        --commit ab30e5f274f2c91061ed028eb659600553a74f14 \
        --out config/model-select-snapshot.json \
        --extra-denylist /path/to/internal-denylist.txt

Internal maintainers regenerating this snapshot from a real internal source
checkout MUST pass ``--extra-denylist`` pointing at that organization's own
private denylist file (one case-insensitive term per line: internal repo
names, client/customer names, product codenames, etc.). That private file
must never live under ``public/``; this script ships with no organization-
or client-specific terms baked in, only generic patterns (emails, URLs,
long numeric ids, path-like strings, secret-shaped tokens, and generic
words like "customer" or "requester_id").
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = 1

# -- Fixed allowlist of GCF paths this builder is permitted to read. ---------
# Only these paths are ever read from the GCF repo; no directory walk, no
# glob, no network. Each one is a reviewed, committed GCF doc/data file at the
# pinned commit, not a live or writable source.
CAPABILITY_SCREEN_PATH = (
    "docs/claude-tag/model-selection/cohort-v2/capability-screen/"
    "jev-exact-routes-2026-09-27.json"
)

# Quoted verbatim from GCF docs/claude-tag/model-selection/cohort-v2/STATUS.md
# at the pinned commit: "Seven visible Codex CLI slugs are candidates:
# gpt-6-sol, gpt-6-astra, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra,
# gpt-5.6-luna, and gpt-5.5." Kept as a literal constant (not re-parsed from
# prose) so the builder stays deterministic and never ingests free text.
CODEX_NATIVE_MODEL_IDS = (
    "gpt-6-sol",
    "gpt-6-astra",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)

# This package's own public native Claude route inventory
# (config/models.json, provider "claude"). Sourced from this already-public
# repository, not from GCF; listed here rather than re-read from models.json
# so a concurrent edit to that file cannot silently change snapshot content
# out from under a reviewed publish.
CLAUDE_NATIVE_MODEL_IDS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5-1",
)

# Maps an OpenRouter route_id from the capability-screen receipt to a public,
# descriptive task-family id. Card codes (F1, B3, ...) from the GCF cohort
# experiment are intentionally not reproduced; they are an internal
# experiment-tracking label, not a public task taxonomy.
ROUTE_TASK_FAMILY = {
    "openrouter:qwen/qwen3-coder": "general_coding_execution",
    "openrouter:bytedance/ui-tars-1.5-7b": "ui_navigation_repair",
    "openrouter:openai/gpt-4o-mini": "lightweight_high_volume_execution",
    "openrouter:openai/o3-mini": "reasoning_heavy_backend_repair",
}

TASK_FAMILY_DESCRIPTIONS = {
    "general_coding_execution": (
        "General-purpose coding task execution: read, edit, and test loops "
        "needing tool calling and moderate reasoning."
    ),
    "ui_navigation_repair": (
        "UI-focused navigation or small visual repair tasks."
    ),
    "lightweight_high_volume_execution": (
        "Cheap, high-volume, low-complexity execution (e.g. small fixes, "
        "simple extraction) where unit cost dominates."
    ),
    "reasoning_heavy_backend_repair": (
        "Backend or logic-heavy repair tasks that benefit from stronger "
        "step-by-step reasoning."
    ),
}

# Naming-convention quality tier only -- never a measured score. Documented
# explicitly so a reader never mistakes it for an evaluated benchmark result.
_HIGH_TIER_MARKERS = ("opus", "gpt-6")
_LOW_TIER_MARKERS = ("haiku", "fable", "gpt-4o-mini", "ui-tars")


def _quality_tier(model_id: str) -> str:
    lowered = model_id.lower()
    if any(marker in lowered for marker in _HIGH_TIER_MARKERS):
        return "high"
    if any(marker in lowered for marker in _LOW_TIER_MARKERS):
        return "low"
    return "medium"


# -- De-identification denylist ----------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://\S+")
LONG_NUMERIC_RE = re.compile(r"\d{9,}")
SECRET_SHAPED_RE = re.compile(
    r"sk-[A-Za-z0-9]{10,}|AIza[0-9A-Za-z_-]{10,}|Bearer\s+[A-Za-z0-9._-]{10,}"
)
# Path-like strings: a GCF/dev-tools source path, never allowed in the
# snapshot (the snapshot stores identifiers and short descriptions, not
# file locations). Deliberately narrow (known path roots or file
# extensions) so ordinary prose containing a slash is not a false positive.
PATH_LIKE_RE = re.compile(
    r"(docs/|common/|scripts/|config/|public/|side_lane/|tests/"
    r"|/Users/|/home/|\.\./|\.py\b|\.json\b|\.md\b)"
)

# Generic, organization-agnostic substrings that must never appear in
# generated snapshot content. Deliberately contains no internal repo, org,
# client, or product-specific term: this file ships publicly. An internal
# maintainer supplies those terms separately through ``--extra-denylist``
# (see module docstring), loaded by ``load_extra_denylist`` and merged in by
# ``scan_for_leaks``.
GENERIC_SENSITIVE_SUBSTRINGS = (
    "asana",
    "gid:",
    "slack",
    "customer",
    "requester_id",
)

# Fields allowed to contain a long hex string (a commit SHA is identifying
# evidence, not a leak) and therefore skipped by the long-numeric scan.
_ALLOWLISTED_FIELD_PATHS = {("generated_from", "selector_commit")}


def load_extra_denylist(path: Path | None) -> tuple[str, ...]:
    """Load one case-insensitive term per line from an external file.

    This is how an internal maintainer supplies organization-, client-, or
    product-specific terms (see the module docstring); this builder never
    bakes such terms in itself, since it ships under ``public/``.
    """

    if path is None:
        return ()
    terms = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        term = raw_line.strip().lower()
        if term and not term.startswith("#"):
            terms.append(term)
    return tuple(terms)


def scan_for_leaks(
    value: Any, *, path: tuple[str, ...] = (), extra_terms: Sequence[str] = ()
) -> list[str]:
    """Return a list of human-readable problems; empty means clean.

    ``extra_terms`` lets a caller (normally ``main`` via ``--extra-denylist``)
    merge in organization-specific terms on top of the generic patterns
    below, without those terms ever being hardcoded in this public file.
    """

    problems: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            problems.extend(scan_for_leaks(item, path=path + (str(key),), extra_terms=extra_terms))
        return problems
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(scan_for_leaks(item, path=path + (str(index),), extra_terms=extra_terms))
        return problems
    if not isinstance(value, str):
        return problems

    location = ".".join(path) or "<root>"
    if EMAIL_RE.search(value):
        problems.append(f"{location}: looks like an email address")
    if URL_RE.search(value):
        problems.append(f"{location}: contains a URL")
    if SECRET_SHAPED_RE.search(value):
        problems.append(f"{location}: looks like a secret/token")
    lowered = value.lower()
    for needle in GENERIC_SENSITIVE_SUBSTRINGS:
        if needle in lowered:
            problems.append(f"{location}: contains denylisted term {needle!r}")
    for needle in extra_terms:
        if needle in lowered:
            problems.append(f"{location}: contains extra-denylisted term {needle!r}")
    if path not in _ALLOWLISTED_FIELD_PATHS and LONG_NUMERIC_RE.search(value):
        problems.append(f"{location}: contains a long numeric id")
    if PATH_LIKE_RE.search(value):
        problems.append(f"{location}: looks like a file path")
    return problems


def _git_show(repo: Path, commit: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git show failed for {path} at {commit}: {result.stderr.strip()}"
        )
    return result.stdout


def _capability_model_rows(repo: Path, commit: str) -> list[dict[str, Any]]:
    raw = _git_show(repo, commit, CAPABILITY_SCREEN_PATH)
    data = json.loads(raw)
    rows = []
    for entry in data.get("openrouter_routes", []):
        route_id = entry.get("route_id")
        family = ROUTE_TASK_FAMILY.get(route_id)
        if family is None:
            continue
        model_id = route_id.split(":", 1)[1]
        rows.append(
            {
                "model_id": model_id,
                "family": family,
                "tools_supported": bool(entry.get("catalog_tools_supported")),
                "tool_choice_supported": bool(entry.get("catalog_tool_choice_supported")),
                "jev_selected_cards": list(entry.get("jev_selected_cards", [])),
            }
        )
    return rows


def build_snapshot(*, repo: Path, commit: str, generated_at: str) -> dict[str, Any]:
    capability_rows = _capability_model_rows(repo, commit)

    task_families: dict[str, Any] = {}

    def _family(family_id: str) -> dict[str, Any]:
        return task_families.setdefault(
            family_id,
            {"description": TASK_FAMILY_DESCRIPTIONS[family_id], "models": {}},
        )

    # OpenRouter models from the capability screen.
    for row in capability_rows:
        entry = _family(row["family"])
        entry["models"][row["model_id"]] = {
            "success_probability": None,
            "quality_tier": _quality_tier(row["model_id"]),
            "capabilities": {
                "tools": row["tools_supported"],
                "code_edit": row["tools_supported"],
                "long_context": None,
            },
            "token_multiplier": None,
        }

    # Native hosts are general-purpose coding workers in every family they can
    # reach; record them once under the general family and let the scorer
    # treat an unlisted (family, model) pair as "no snapshot prior" rather
    # than unqualified.
    general = _family("general_coding_execution")
    for model_id in CLAUDE_NATIVE_MODEL_IDS + CODEX_NATIVE_MODEL_IDS:
        general["models"].setdefault(
            model_id,
            {
                "success_probability": None,
                "quality_tier": _quality_tier(model_id),
                "capabilities": {"tools": True, "code_edit": True, "long_context": None},
                "token_multiplier": None,
            },
        )

    jev_decisions = []
    all_openrouter_model_ids = [row["model_id"] for row in capability_rows]
    for row in capability_rows:
        alternatives = [m for m in all_openrouter_model_ids if m != row["model_id"]]
        jev_decisions.append(
            {
                "task_family": row["family"],
                "chosen_model": row["model_id"],
                "alternatives_considered": alternatives,
                # Source text is explicit that this was a capability/endpoint
                # screen, not a graded task outcome, so no outcome is
                # recorded: leaving it null here rather than inventing a
                # pass/fail label.
                "outcome": None,
                "date": "2026-09-27",
            }
        )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": f"model-select-{commit[:8]}-{generated_at[:10]}",
        "generated_from": {
            "selector_commit": commit,
            "generated_at": generated_at,
            "method": (
                "Deterministic extraction by the checked-in snapshot builder "
                "from the pinned upstream selector commit named in "
                "selector_commit, plus this package's own public native-host "
                "route inventory. No upstream source code is copied; only "
                "model identifiers, capability-support booleans, and a "
                "naming-convention quality tier are retained. Prices are not "
                "frozen here."
            ),
        },
        "task_families": task_families,
        "jev_decisions": jev_decisions,
        "notes": {
            "price_policy": (
                "Prices are fetched live from OpenRouter's public models API "
                "at run time, or treated as unknown. Unknown price is never "
                "treated as free; included-usage hosts carry a nonzero "
                "opportunity cost once a comparable reference price is known."
            ),
            "scope": (
                "Claude and Codex native hosts plus OpenRouter. Devin and "
                "other providers are out of scope for this public skill."
            ),
            "learning": "This snapshot is frozen; it never learns or writes back.",
        },
    }
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcf-repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--extra-denylist",
        type=Path,
        default=None,
        help=(
            "Path to a file of extra case-insensitive denylist terms, one "
            "per line (# comments allowed). Internal maintainers building "
            "this snapshot from a real internal source checkout MUST pass "
            "this, pointing at their own private list of internal repo, "
            "client, and product names -- this script ships with none "
            "baked in. That file must be kept outside public/."
        ),
    )
    args = parser.parse_args(argv)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = build_snapshot(repo=args.gcf_repo, commit=args.commit, generated_at=generated_at)

    extra_terms = load_extra_denylist(args.extra_denylist)
    problems = scan_for_leaks(snapshot, extra_terms=extra_terms)
    if problems:
        for problem in problems:
            print(f"DENYLIST: {problem}", file=sys.stderr)
        print(
            f"refusing to write snapshot: {len(problems)} de-identification "
            "problem(s) found",
            file=sys.stderr,
        )
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(json.dumps(snapshot))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
