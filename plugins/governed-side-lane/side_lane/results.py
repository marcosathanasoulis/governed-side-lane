"""Normalized result contract shared by all native and billable adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaneResult:
    argv: tuple[str, ...]
    returncode: int
    cwd: Path
    host: str
    provider: str
    gateway: str
    model: str
    auth_method: str
    billable: bool
    stdout: str
    stderr: str
    availability: str = "completed"
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()

    @property
    def worktree(self) -> Path:
        return self.cwd

    def as_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "provider": self.provider,
            "gateway": self.gateway,
            "model": self.model,
            "auth_method": self.auth_method,
            "billable": self.billable,
            "cwd": str(self.cwd),
            "exit_status": self.returncode,
            "availability": self.availability,
            "capabilities": list(self.capabilities),
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
        }
