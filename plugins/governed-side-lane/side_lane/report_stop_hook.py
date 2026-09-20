"""Deterministic, stdlib-only Claude Code Stop hook for report validation.

The hook is invoked by Claude Code's `hooks.Stop` per-launch settings. It
receives one JSON object on stdin with `hook_event_name`, `cwd`, and
`stop_hook_active`, and the validated report path through its own
`--settings` JSON argument. It is read-only and never writes the worktree
or disables inherited hooks or permissions.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

MAX_STDIN_BYTES = 8192
# Bounded content window: the helper never reads more than this from a report,
# so an arbitrarily large file costs a fixed read, and a whitespace-only file
# of any size is still rejected.
MAX_CONTENT_SAMPLE = 1024 * 1024
REPORT_NAME = "SIDE_LANE_REPORT.md"


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

    Shared with the CLI's post-run gate so the in-loop hook and the runner's
    final acceptance apply one rule. Never opens anything that ``lstat`` did
    not first confirm is a regular file, so a FIFO, device, or directory can
    never make this block.
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


def _block_decision() -> str:
    return json.dumps({
        "decision": "block",
        "reason": (
            f"Stop blocked: {REPORT_NAME} is missing, empty, a symlink, or not a regular file. "
            "Write or re-read the actual honest findings report, explicitly label incomplete coverage, "
            "preserve screenshots, make no source or git changes, and do not treat completion prose as delivery."
        ),
    })


def _read_stdin(stdin: object) -> bytes:
    """Read a bounded, non-blocking amount from stdin."""
    try:
        return stdin.read(MAX_STDIN_BYTES)
    except OSError:
        return b""


def decide(argv: list[str], stdin: object, stdout: object, stderr: object) -> int:
    """Evaluate one Stop hook event and return the helper exit code.

    The helper exits 0 in all cases; it blocks a stop by printing a
    ``decision: block`` JSON object on stdout and allows a stop by printing
    nothing.
    """
    # The report path is fixed by the parent process; cwd from stdin is ignored.
    settings = _load_settings(argv)
    if settings is None:
        return 0
    report_path = _validated_report_path(settings)
    if report_path is None:
        return 0

    raw = _read_stdin(stdin)
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

    if report_is_valid(report_path):
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
