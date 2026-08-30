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
        }
