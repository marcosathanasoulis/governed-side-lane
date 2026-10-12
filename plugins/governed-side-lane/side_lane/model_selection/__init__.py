"""Shared model selection for Prompt it and Side Lane (see docs/plans/2026-10-11-oauth-roundrobin-routing.md)."""

from .contract import (AVAILABILITY_STATES, CONTRACT_VERSION, ROUTES, TIERS, ContractError, ExecutionReceipt,
                       Request, Selection)
from .rotation import JsonFileState, MemoryState, SpreadStrategy
from .selector import select
from .tiers import TierTable, load

__all__ = ["AVAILABILITY_STATES", "CONTRACT_VERSION", "ROUTES", "TIERS", "ContractError", "ExecutionReceipt",
           "JsonFileState", "MemoryState", "Request", "Selection", "SpreadStrategy", "TierTable", "load", "select"]
