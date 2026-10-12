"""Load and validate the reviewed, versioned model-to-tier table."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .contract import ROUTES, TIERS, Candidate, ContractError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "model-tiers.json"
PLANS = ("included", "metered", "unverified")


@dataclass(frozen=True)
class TierTable:
    version: str
    entries: tuple[Candidate, ...]

    def for_tier(self, tier: str) -> list[Candidate]:
        return [c for c in self.entries if c.tier == tier]

    def find_model(self, model: str) -> list[Candidate]:
        return [c for c in self.entries if c.model == model]

    def find(self, provider: str, model: str | None, tier: str | None = None) -> list[Candidate]:
        return [c for c in self.entries
                if c.provider == provider and (model is None or c.model == model) and (tier is None or c.tier == tier)]


def _strict_flag(entry: dict, name: str = "declarable_included") -> bool:
    """A malformed value (the string "false" is truthy) must fail closed, never enable an automatic paid route."""
    value = entry.get(name, False)
    if not isinstance(value, bool):
        raise ContractError(f"tier table: {name} must be a boolean")
    return value


def load(path: Path | None = None) -> TierTable:
    raw = json.loads(Path(path or DEFAULT_PATH).read_text())
    entries: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for e in raw["entries"]:
        if e["tier"] not in TIERS:
            raise ContractError(f"tier table: bad tier {e['tier']!r}")
        if e["route"] not in ROUTES or e["route"] == "metered_openrouter":
            raise ContractError(f"tier table: bad route {e['route']!r}")
        if e["plan"] not in PLANS:
            raise ContractError(f"tier table: bad plan {e['plan']!r}")
        if _strict_flag(e, "probe_required") and not _strict_flag(e):
            raise ContractError("tier table: probe_required only applies to a declarable_included entry")
        if _strict_flag(e) and e["plan"] != "metered":
            raise ContractError("tier table: only a metered entry can be declarable_included")
        key = (e["provider"], e["model"], e["tier"])
        if key in seen:
            raise ContractError(f"tier table: duplicate {key}")
        seen.add(key)
        entries.append(Candidate(provider=e["provider"], model=e["model"], tier=e["tier"], group=e["group"],
                                 route=e["route"], plan=e["plan"], binding_provider=e["binding_provider"],
                                 effort=e.get("effort"), openrouter_slug=e.get("openrouter_slug"),
                                 catalog_provider=e.get("catalog_provider"), external_catalog=e.get("external_catalog"),
                                 declarable_included=_strict_flag(e), probe_required=_strict_flag(e, "probe_required")))
    return TierTable(version=str(raw["version"]), entries=tuple(entries))
