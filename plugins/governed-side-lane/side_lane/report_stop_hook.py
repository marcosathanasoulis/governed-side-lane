"""Deterministic, stdlib-only Claude Code Stop hook for report validation.

The hook is invoked by Claude Code's `hooks.Stop` per-launch settings. It
receives one JSON object on stdin carrying `hook_event_name`, `cwd`, and
`stop_hook_active` alongside fields such as `session_id`, `transcript_path`,
`permission_mode`, and `last_assistant_message` — the last of which can be
large, so stdin is read under a fixed byte bound and unknown fields are
ignored. The validated report path and this run's freshness baseline arrive
together through its own `--settings` JSON argument. It is read-only and never
writes the worktree or disables inherited hooks or permissions.

A report meets the mechanical freshness gate when its sampled identity *differs*
from what the runner recorded there before the worker started. A lane worktree
is added from HEAD, so a repository that tracks `SIDE_LANE_REPORT.md` hands
every new lane a complete-looking report no worker wrote; path, non-emptiness,
and mtime alone cannot tell that inherited file from a real delivery. The same
:class:`ReportBaseline` therefore drives the in-loop hook below and the
runner's post-run acceptance, so both apply one rule to one path. A changed
identity alone does not establish semantic task identity or report quality.

This module is imported by the runner AND executed as a bare script by Claude
Code (the hook command points at this file), so it must import nothing from
the package: a failed import would exit non-zero, which Claude Code reads as a
blocked stop for every turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# Bounded stdin window: the Stop payload's last_assistant_message can be
# large, so the bound must fit a real event while still capping the read.
MAX_STDIN_BYTES = 1024 * 1024
# Bounded content window: the helper never reads more than this from a report,
# so an arbitrarily large file costs a fixed read, and a whitespace-only file
# of any size is still rejected.
MAX_CONTENT_SAMPLE = 1024 * 1024
# Bounded identity window: a report's identity is the hash of at most this many
# leading bytes plus the file's FULL size, so an inherited multi-megabyte blob
# costs a fixed read while any size change still changes the identity.
MAX_IDENTITY_BYTES = 8 * 1024 * 1024
REPORT_NAME = "SIDE_LANE_REPORT.md"
# Keys of the `--settings` payload this package's own adapter builds. The
# path and the run baseline travel together; the baseline is process argv
# fixed by the parent process, so a worker cannot rewrite it the way it could
# rewrite a file inside its own lane.
FRESHNESS_KEY = "report_freshness"
BASELINE_IDENTITY_KEY = "baseline_identity"
# The lane's ignored scratch directory, where a copy of an inherited report is
# preserved so replacing it erases no history. Kept as a literal here because
# this module must import nothing from the package; tests pin it to
# ``side_lane.worktrees.SCRATCH_DIR_NAME``.
SCRATCH_DIR_NAME = ".side-lane-scratch"
PRESERVED_DIR_NAME = "report-inherited"
IDENTITY_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}:size:[0-9]+\Z")


class UnsafeReportPath(RuntimeError):
    """Something a freshness baseline must not be taken from sits at the path.

    A symlink, FIFO, socket, device, or directory is refused rather than
    inspected: following a link would take the baseline from a file other than
    the lane's own artifact path, and reading a FIFO could block forever.
    """


@dataclass(frozen=True)
class ReportBaseline:
    """What the fixed report path already held when this run started.

    ``identity`` is None when nothing was there — the ordinary case, where any
    valid report at the path is this run's. ``preserved_path`` is the copy of a
    preexisting report kept in the lane's ignored scratch, or None when there
    was nothing to preserve or the copy could not be written.
    """

    report_path: Path
    identity: str | None
    preserved_path: Path | None = None

    @property
    def preexisting(self) -> bool:
        """True when the run started with a report already at the path."""
        return self.identity is not None


def _diagnostic(message: str) -> None:
    """Emit one non-blocking diagnostic to stderr without payload data."""
    print(message, file=sys.stderr)


def _load_settings(argv: list[str]) -> dict[str, object] | None:
    """Parse the single `--settings` JSON argument."""
    if len(argv) != 3 or argv[1] != "--settings":
        _diagnostic("report stop hook: expected --settings JSON argument")
        return None
    raw = argv[2]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _diagnostic(f"report stop hook: --settings is not valid JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        _diagnostic("report stop hook: --settings must be a JSON object")
        return None
    return payload


def _validated_report_path(settings: dict[str, object]) -> Path | None:
    """Return the fixed, absolute report path from the settings object."""
    report_path = settings.get("report_path")
    if not isinstance(report_path, str) or not report_path:
        _diagnostic("report stop hook: missing or invalid report_path")
        return None
    path = Path(report_path)
    if path.name != REPORT_NAME:
        _diagnostic("report stop hook: report_path is not SIDE_LANE_REPORT.md")
        return None
    # Normalize without following symlinks: a symlinked report must stay
    # visible to the lstat check below and be blocked, not silently accepted
    # because the link target happens to resolve somewhere plausible.
    try:
        resolved = Path(os.path.abspath(os.fspath(path)))
    except (OSError, ValueError) as exc:
        _diagnostic(f"report stop hook: cannot resolve report_path: {exc}")
        return None
    if resolved.name != REPORT_NAME:
        _diagnostic("report stop hook: resolved report name changed")
        return None
    return resolved


def _is_nonsymlink_regular_file(mode: int) -> bool:
    """A regular file that is not a symlink."""
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _has_non_whitespace(path: Path) -> bool:
    """Read a bounded window and require at least one non-space byte in it.

    The caller has already established from ``lstat`` that this is a regular
    file, so the read can never block on a FIFO or device. At most
    ``MAX_CONTENT_SAMPLE`` bytes are ever read, whatever the file's size; a
    window that is entirely whitespace is not a report.
    """
    try:
        with open(path, "rb") as fh:
            sample = fh.read(MAX_CONTENT_SAMPLE)
    except OSError as exc:
        _diagnostic(f"report stop hook: cannot read {REPORT_NAME}: {exc}")
        return False
    return not sample.isspace()


def report_is_valid(path: Path | str) -> bool:
    """True when the fixed report is a non-empty, non-symlink regular file.

    The artifact-type half of the freshness rule: both the in-loop hook and the
    runner's post-run gate reach it through :func:`report_is_current`, which
    adds the run-bound comparison. Never opens anything that ``lstat`` did not
    first confirm is a regular file, so a FIFO, device, or directory can never
    make this block.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if not _is_nonsymlink_regular_file(st.st_mode):
        return False
    if st.st_size == 0:
        return False
    return _has_non_whitespace(Path(path))


def _unsafe_kind(mode: int) -> str:
    """Name what is at the path instead of a plain file, for a diagnostic."""
    if stat.S_ISLNK(mode):
        return "a symlink"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    if stat.S_ISSOCK(mode):
        return "a socket"
    return "not a regular file"


def report_identity(path: Path | str) -> str | None:
    """Identity of the plain file at ``path``, or None when nothing is there.

    The identity is the SHA-256 of a bounded prefix plus the file's full size.
    It is computed only from what ``lstat`` already confirmed is a regular,
    non-symlink file — the same never-open-what-lstat-did-not-confirm rule as
    :func:`report_is_valid` — so a FIFO or device can never make this block and
    a link is never followed. Anything else at the path raises
    :class:`UnsafeReportPath`, and so does a file that cannot be read: a
    baseline nobody could take is exactly the case that must fail closed.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise UnsafeReportPath(f"cannot inspect {REPORT_NAME}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise UnsafeReportPath(
            f"{REPORT_NAME} is {_unsafe_kind(st.st_mode)}, not a plain file"
        )
    digest = hashlib.sha256()
    remaining = MAX_IDENTITY_BYTES
    try:
        with open(path, "rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise UnsafeReportPath(f"cannot read {REPORT_NAME}: {exc}") from exc
    return f"sha256:{digest.hexdigest()}:size:{st.st_size}"


def _preserve_inherited_report(source: Path, preserve_dir: Path) -> Path | None:
    """Copy an inherited report into ignored scratch before it can be replaced.

    The copy is written under the lane's scratch directory, which git excludes
    from the lane worktree, so preserving history can never enter a delivery or
    the coordinator's checkout. An existing copy is left alone: the earliest
    content is the one worth keeping. A copy that cannot be written is a
    diagnostic, not a failure — freshness is still enforced without it.
    """
    destination = preserve_dir / PRESERVED_DIR_NAME / REPORT_NAME
    try:
        if preserve_dir != source.parent / SCRATCH_DIR_NAME:
            raise OSError("preservation directory is not the lane scratch directory")
        # Check each existing component before creating the next. These are
        # prelaunch checks, not a sandbox against concurrent same-user writers.
        for directory in (preserve_dir, destination.parent):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                directory.mkdir()
                mode = directory.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise OSError("preservation directory is not a plain directory")
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not _is_nonsymlink_regular_file(mode):
                raise OSError("preserved report is not a plain file")
            return destination
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as original:
            if not stat.S_ISREG(os.fstat(original.fileno()).st_mode):
                raise OSError("inherited report is not a plain file")
            # Exclusive creation cannot overwrite a newly appeared destination.
            with destination.open("xb") as saved:
                shutil.copyfileobj(original, saved)
    except OSError as exc:
        _diagnostic(f"report baseline: cannot preserve {REPORT_NAME}: {exc}")
        return None
    return destination


def capture_report_baseline(
    report_path: Path | str, *, preserve_dir: Path | str | None = None
) -> ReportBaseline:
    """Record what the fixed report path holds, before any worker starts.

    This is the run's freshness baseline. It must be captured by the runner
    before the worker is launched: a baseline taken afterwards would compare
    the worker's own artifact with itself and prove nothing. An inherited
    report is copied into ``preserve_dir`` (the lane's ignored scratch by
    default) so that overwriting it erases no history, and the original is
    never modified, moved, or deleted.
    """
    path = Path(os.path.abspath(os.fspath(report_path)))
    identity = report_identity(path)
    preserved: Path | None = None
    if identity is not None:
        directory = (
            path.parent / SCRATCH_DIR_NAME
            if preserve_dir is None
            else Path(os.path.abspath(os.fspath(preserve_dir)))
        )
        preserved = _preserve_inherited_report(path, directory)
    return ReportBaseline(path, identity, preserved)


def hook_settings(baseline: ReportBaseline) -> dict[str, object]:
    """The `--settings` payload that arms a report-only run's Stop hook.

    The fixed path and the run baseline are one payload, so the in-loop hook
    and the runner's acceptance cannot drift onto different rules or paths.
    """
    return {
        "report_path": str(baseline.report_path),
        FRESHNESS_KEY: {BASELINE_IDENTITY_KEY: baseline.identity},
    }


def baseline_from_settings(settings: dict[str, object]) -> ReportBaseline | None:
    """Read the run baseline back out of a hook `--settings` payload.

    None means the payload is malformed, names the wrong path, or carries no
    run baseline at all. The caller then allows the stop with a diagnostic
    instead of blocking: a hook must never wedge a worker over input it cannot
    trust, and the runner's post-run acceptance is authoritative either way —
    it always holds a captured baseline and fails closed without one.
    """
    path = _validated_report_path(settings)
    if path is None:
        return None
    freshness = settings.get(FRESHNESS_KEY)
    if not isinstance(freshness, dict) or BASELINE_IDENTITY_KEY not in freshness:
        _diagnostic("report stop hook: --settings carries no run baseline")
        return None
    identity = freshness[BASELINE_IDENTITY_KEY]
    if identity is not None and (
        not isinstance(identity, str) or IDENTITY_PATTERN.match(identity) is None
    ):
        _diagnostic("report stop hook: --settings carries a malformed run baseline")
        return None
    return ReportBaseline(path, identity)


def report_freshness_state(baseline: ReportBaseline | None) -> str:
    """One word for the operator, and the single rule both gates apply.

    ``current``    valid file with a new sampled content identity
    ``stale``      its sampled identity matches the pre-launch baseline
    ``unusable``   missing, empty, whitespace-only, or not a plain file
    ``unverified`` no run baseline was captured, so nothing can be claimed
    """
    if baseline is None:
        return "unverified"
    if not report_is_valid(baseline.report_path):
        return "unusable"
    try:
        identity = report_identity(baseline.report_path)
    except UnsafeReportPath:
        return "unusable"
    if identity is None:
        # The file vanished between the two looks; fail closed.
        return "unusable"
    if baseline.identity is None:
        return "current"
    return "current" if identity != baseline.identity else "stale"


def report_is_current(baseline: ReportBaseline | None) -> bool:
    """True when the fixed report passes the mechanical freshness gate."""
    return report_freshness_state(baseline) == "current"


def _block_decision() -> str:
    return json.dumps({
        "decision": "block",
        "reason": (
            f"Stop blocked: {REPORT_NAME} is missing, empty, unreadable, not a regular file, "
            "or unchanged from what the lane started with — a report that was already there "
            "before this run is not this run's delivery. "
            "Write or re-read the actual honest findings report for this task, explicitly label "
            "incomplete coverage, preserve screenshots, make no source or git changes, and do not "
            "treat completion prose as delivery."
        ),
    })


def _read_stdin(stdin: object) -> bytes | str | None:
    """Read a bounded amount from stdin.

    Loops until EOF or ``MAX_STDIN_BYTES + 1`` bytes have arrived: one
    ``read(n)`` on a pipe may return fewer than ``n`` bytes without reaching
    EOF. Returns ``None`` when more than ``MAX_STDIN_BYTES`` bytes are
    present, so the caller rejects the input outright instead of parsing a
    silently truncated prefix.
    """
    chunks = []
    remaining = MAX_STDIN_BYTES + 1
    try:
        while remaining > 0:
            chunk = stdin.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return b""
    if remaining == 0:
        return None
    if chunks and isinstance(chunks[0], str):
        return "".join(chunks)
    return b"".join(chunks)


def decide(argv: list[str], stdin: object, stdout: object, stderr: object) -> int:
    """Evaluate one Stop hook event and return the helper exit code.

    The helper exits 0 in all cases; it blocks a stop by printing a
    ``decision: block`` JSON object on stdout and allows a stop by printing
    nothing.
    """
    # The report path and the run baseline are fixed by the parent process;
    # cwd from stdin is ignored.
    settings = _load_settings(argv)
    if settings is None:
        return 0
    baseline = baseline_from_settings(settings)
    if baseline is None:
        return 0

    raw = _read_stdin(stdin)
    if raw is None:
        _diagnostic("report stop hook: stdin exceeds the size bound")
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _diagnostic(f"report stop hook: malformed stdin JSON: {exc}")
        return 0
    if not isinstance(payload, dict):
        _diagnostic("report stop hook: stdin is not a JSON object")
        return 0
    if payload.get("hook_event_name") != "Stop":
        # Not a Stop event; let it pass.
        return 0

    stop_hook_active = payload.get("stop_hook_active")
    if not isinstance(stop_hook_active, bool):
        _diagnostic("report stop hook: stop_hook_active is not an exact boolean")
        return 0
    if stop_hook_active:
        # This worker already continued because of a stop hook; allow the stop
        # now to bound the feedback to one round.
        return 0

    if report_is_current(baseline):
        return 0

    print(_block_decision(), file=stdout)
    return 0


def main(argv: list[str] | None = None, stdin: object | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if stdin is None:
        stdin = sys.stdin.buffer
    return decide(argv, stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
