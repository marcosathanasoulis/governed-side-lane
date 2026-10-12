"""Even spread across providers.

Each owner has a per-provider count of committed selections. The next provider is
the one with the lowest count; ties go to table order, so identical inputs and
identical state always give the same answer. There is deliberately no bias toward
the coordinating harness: it does its own work outside this count, so it uses
more than its even share without help. Counts are per owner across tiers, so the
spread holds over a whole day.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, ContextManager, Iterator, Mapping, Protocol, Sequence

from .contract import Candidate, Request


#: Retry identity is kept for the most recent reservations per owner. A delayed retry of a thread older than
#: this horizon is treated as a new request (its earlier count stays in the totals). Sized for a long working
#: period (thousands of threads) while keeping the state file small.
MAX_RESERVATIONS = 2048


class Tx:
    """A read-choose-record unit with idempotent retries.

    ``begin(key)`` marks the request. If the same request key already reserved a provider, that
    reservation is refunded first, so a retry re-decides from the same counts and ends with the
    same totals instead of counting twice. ``record`` is a no-op unless the transaction commits."""

    def __init__(self, bucket: dict[str, Any], commit: bool) -> None:
        self._bucket, self._commit = bucket, commit
        self._counts: dict[str, int] = bucket.setdefault("counts", {})
        self._reserved: dict[str, str] = bucket.setdefault("reservations", {})
        self._key: str | None = None
        self._old: str | None = None

    def begin(self, key: str) -> None:
        """Remember an earlier reservation for this request. It is only given back when a replacement
        is actually recorded, so a retry that produces no selection never erases committed history."""
        self._key = key
        self._old = self._reserved.get(key)

    def release(self) -> None:
        """Give back this thread's earlier reservation when the work moves to a route that does not count in
        the spread (metered). Only takes effect when the transaction commits."""
        if not self._commit:
            return
        if self._old is not None and self._counts.get(self._old, 0) > 0:
            self._counts[self._old] -= 1
        if self._key is not None:
            self._reserved.pop(self._key, None)
        self._old = None

    def counts(self) -> Mapping[str, int]:
        """Counts as the earlier reservation for this request had not happened, so a retry decides the same way."""
        view = dict(self._counts)
        if self._old is not None and view.get(self._old, 0) > 0:
            view[self._old] -= 1
        return view

    def record(self, provider: str) -> None:
        if not self._commit:
            return
        if self._old is not None and self._counts.get(self._old, 0) > 0:
            self._counts[self._old] -= 1
        self._counts[provider] = self._counts.get(provider, 0) + 1
        if self._key is not None:
            self._reserved.pop(self._key, None)
            self._reserved[self._key] = provider
            while len(self._reserved) > MAX_RESERVATIONS:
                self._reserved.pop(next(iter(self._reserved)))


class NoTx(Tx):
    def __init__(self) -> None:
        super().__init__({}, False)

    def begin(self, key: str) -> None:
        return None

    def release(self) -> None:
        return None


class SpreadState(Protocol):
    def transaction(self, owner_id: str, commit: bool = True) -> ContextManager[Tx]: ...


def _fresh() -> dict[str, Any]:
    return {"counts": {}, "reservations": {}}


def _bucket(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("counts"), dict) or not isinstance(raw.get("reservations"), dict):
        raise StateError("spread state has an unexpected shape; not resetting it")
    counts, reserved = raw["counts"], raw["reservations"]
    if not all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in counts.items()):
        raise StateError("spread state has invalid counts; not resetting it")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in reserved.items()):
        raise StateError("spread state has invalid reservations; not resetting it")
    return {"counts": dict(raw["counts"]), "reservations": dict(raw["reservations"])}


class StateError(OSError):
    """Persisted spread state could not be read; selection fails closed instead of resetting it."""


class LockTimeout(TimeoutError):
    """The state lock could not be taken; selection fails closed rather than steal it."""


class MemoryState:
    def __init__(self) -> None:
        self._owners: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def counts(self, owner_id: str) -> Mapping[str, int]:
        return dict(self._owners.get(owner_id, _fresh())["counts"])

    @contextlib.contextmanager
    def transaction(self, owner_id: str, commit: bool = True) -> Iterator[Tx]:
        with self._lock:
            bucket = _bucket(self._owners[owner_id]) if owner_id in self._owners else _fresh()
            tx = Tx(bucket, commit)
            yield tx
            if commit:
                self._owners[owner_id] = bucket


def _try_lock(fd: int) -> bool:
    """Take an exclusive OS lock tied to the open handle; released automatically if the process dies."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (33, 36):  # lock violation
            return False
        raise


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class JsonFileState:
    """Per-user counters. One transaction holds an OS-level exclusive lock on a sidecar file across
    read, choice and write, so concurrent processes cannot both pick the same minimum. The lock is tied
    to the open handle, so it is released if the holder dies and is never stolen or deleted: there is no
    stale-lock heuristic to race. A held lock past the timeout fails closed (LockTimeout)."""

    def __init__(self, path: Path, *, lock_timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout = lock_timeout

    def _read(self) -> dict[str, Any]:
        """An absent file is an empty state; any other failure raises so history is never overwritten."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StateError(f"cannot read spread state {self.path}: {type(exc).__name__}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StateError(f"spread state {self.path} is corrupt; not resetting it") from exc
        if not isinstance(data, dict):
            raise StateError(f"spread state {self.path} has an unexpected shape; not resetting it")
        for v in data.values():
            _bucket(v)
        return data

    def counts(self, owner_id: str) -> Mapping[str, int]:
        data = self._read()
        return dict(_bucket(data[owner_id])["counts"]) if owner_id in data else {}

    def _acquire(self) -> int:
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise StateError(f"cannot open spread lock {self.lock}: {type(exc).__name__}") from exc
        deadline = time.monotonic() + self.lock_timeout
        try:
            while not _try_lock(fd):
                if time.monotonic() > deadline:
                    raise LockTimeout(f"spread state is locked ({self.lock}); not stealing the lock")
                time.sleep(0.02)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextlib.contextmanager
    def transaction(self, owner_id: str, commit: bool = True) -> Iterator[Tx]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = self._acquire()
        try:
            data = self._read()
            bucket = _bucket(data[owner_id]) if owner_id in data else _fresh()
            tx = Tx(bucket, commit)
            yield tx
            if commit:
                data[owner_id] = bucket
                tmp_fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".spread-")
                with os.fdopen(tmp_fd, "w") as fh:
                    json.dump(data, fh)  # no sort_keys: reservation insertion order drives eviction
                os.replace(tmp, self.path)
        finally:
            _unlock(fd)
            os.close(fd)


class SpreadStrategy:
    """Strategy A: tier-filtered, evenly spread across providers."""

    name = "spread-v1"

    def order(self, request: Request, pool: Sequence[Candidate], counts: Mapping[str, int]) -> tuple[list[Candidate], str]:
        seen: set[str] = set()
        ordered: list[Candidate] = []
        for _, c in sorted(enumerate(pool), key=lambda item: (counts.get(item[1].provider, 0), item[0])):
            if c.provider in seen:  # one candidate per provider per ranking
                continue
            seen.add(c.provider)
            ordered.append(c)
        if len(ordered) > 1:
            reason = f"even spread across {len(ordered)} providers: fewest committed selections first"
        else:
            reason = "only one eligible provider in the tier"
        return ordered, reason
