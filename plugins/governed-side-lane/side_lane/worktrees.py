from __future__ import annotations

import collections
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import threading
from typing import Callable, Mapping, Sequence


class WorktreeError(Exception):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class WorktreeRun:
    repository: Path
    worktree: Path
    branch: str
    lane_name: str
    starting_commit: str
    #: True when ``worktree`` is a pre-existing owner workspace this run was
    #: explicitly pointed at (``--existing-workspace``) rather than a worktree
    #: the runner created from HEAD. The workspace is NOT a dedicated lane: it
    #: may carry other writers' staged, unstaged and untracked work, nothing
    #: here disposes of it, and ``branch`` is the branch it already stood on
    #: rather than a lane branch this runner created. Every caller that would
    #: create, publish, or remove a lane branch must refuse instead — the flag
    #: is the one thing that tells the two apart.
    existing_workspace: bool = False
    #: The run-record filename stem, when it must differ from ``branch``. An
    #: existing workspace keeps the owner's branch name, which several lanes
    #: over a workspace's life would collide on, so the records of such a run
    #: are keyed on a lane identity instead.
    record_key: "str | None" = None


def _git(
    repo: Path,
    args: Sequence[str],
    runner: Runner,
    *,
    timeout: "float | None" = None,
    env: "dict[str, str] | None" = None,
) -> str:
    extra = {}
    if timeout is not None:
        extra["timeout"] = timeout
    if env is not None:
        extra["env"] = env
    try:
        result = runner(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            **extra,
        )
    except subprocess.TimeoutExpired as exc:
        # A network git call with no timeout can hang _launch forever, which
        # would strand the run BEFORE the summary is printed -- so the
        # "publication is non-fatal" contract would never actually be reached.
        raise WorktreeError(
            f"git {' '.join(args[:2])} timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise WorktreeError(f"could not run git: {exc}") from exc
    if result.returncode:
        raise WorktreeError(
            (result.stderr or result.stdout).strip() or "git command failed"
        )
    return result.stdout.strip()


def safe_lane_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized or len(normalized) > 48:
        raise WorktreeError("lane name must contain 1-48 safe alphanumeric characters")
    return normalized


LANE_ROOT = ".side-lanes"
LANE_EXCLUDE_PATTERN = (
    f"/{LANE_ROOT}/"  # root-anchored: nested .side-lanes dirs stay visible
)
LEGACY_EXCLUDE_PATTERN = f"{LANE_ROOT}/"  # written by 0.3.0-0.3.3; also hid nested dirs
SCRATCH_DIR_NAME = ".side-lane-scratch"
SCRATCH_EXCLUDE_PATTERN = (
    f"/{SCRATCH_DIR_NAME}/"  # root-anchored: nested scratch dirs stay visible
)
LEGACY_SCRATCH_EXCLUDE_PATTERN = (
    f"{SCRATCH_DIR_NAME}/"  # unanchored; also hid nested dirs
)
DEVIN_LOCAL_MCP_EXCLUDE_PATTERN = "/.devin/mcp_config.local.json"
DEVIN_LOCAL_MCP_RELATIVE_PATH = ".devin/mcp_config.local.json"

#: Every exclude line one of the writers below produces. The existing-workspace
#: git-state reading drops exactly these from the shared ``.git/info/exclude``
#: before digesting it: they are this run's own writes, landing after the
#: baseline was captured, and a reading that counted them would report the
#: tool's own setup as the worker's git write. Only the exact lines are
#: dropped — a pattern the owner wrote, including one that merely contains an
#: entry's text, still moves the digest.
_TOOL_WRITTEN_EXCLUDE_PATTERNS = (
    LANE_EXCLUDE_PATTERN,
    LEGACY_EXCLUDE_PATTERN,
    SCRATCH_EXCLUDE_PATTERN,
    LEGACY_SCRATCH_EXCLUDE_PATTERN,
    DEVIN_LOCAL_MCP_EXCLUDE_PATTERN,
)


def _lf_segments(data: bytes) -> "list[tuple[bytes, bytes]]":
    """``(content, terminator)`` for every ``\\n``-delimited line of ``data``.

    ``str.splitlines`` is not a line splitter for this purpose. It breaks on a
    dozen code points — ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\x85``,
    ``\\u2028``, ``\\u2029`` — that are ordinary characters in an exclude or
    gitignore pattern, and it discards the terminators, so a file rebuilt from
    its result cannot preserve the bytes it did not touch. Here ``\\n`` is the
    only terminator, the ``\\r`` of a CRLF pair stays with the terminator rather
    than the content, and a final line with no newline is returned with an empty
    terminator — so ``content + terminator`` is the original bytes, always.
    """

    parts = data.split(b"\n")
    remainder = parts.pop()
    segments: "list[tuple[bytes, bytes]]" = []
    for part in parts:
        if part.endswith(b"\r"):
            segments.append((part[:-1], b"\r\n"))
        else:
            segments.append((part, b"\n"))
    if remainder:
        segments.append((remainder, b""))
    return segments


def _exclude_entry_is_tool_written(content: bytes, pattern: str) -> bool:
    """Whether one exclude line is this tool's own entry for ``pattern``.

    Git ignores trailing spaces and tabs in a pattern, so those are tolerated,
    while a leading-whitespace line is a different (user-authored) pattern and
    is not one of ours — the same rule every writer below uses.
    """

    return (
        content.rstrip(b" \t") == pattern.encode("ascii")
        and not content[:1].isspace()
    )


def _appended_line_position(data: bytes, written: bytes) -> int:
    """The last index where ``written`` sits as the whole line(s) it wrote, or -1.

    Every writer above writes one line: an optional separator LF (added only
    when the file it appended to had no terminal newline), the pattern, and a
    terminating LF. So a ``written`` that does not end with an LF is not one of
    these writes and never matches.

    Without a separator the write begins at a line start, which is the check
    that keeps a match from landing inside an owner's own line: a worker who
    deletes the launch's line and appends the same bytes to the end of a line
    they wrote produces bytes that occur mid-line, and stripping those would
    strip the worker's edit and report the file as unchanged.

    With a separator the leading LF is the launch's evidence that it was
    terminating an *unterminated* line at the end of the file, so that form
    matches only where it still is one: a trailing occurrence. Any bytes after
    it are a later edit by someone else, and the digest is meant to move for
    those.

    The *last* qualifying occurrence is the one removed, so a duplicate line
    added later still leaves one copy behind — and moves the digest, as an
    owner mutation must.
    """

    if not written or not written.endswith(b"\n"):
        return -1
    separator_form = written[:1] == b"\n"
    position = data.rfind(written)
    while position >= 0:
        if separator_form:
            if position + len(written) == len(data):
                return position
        elif position == 0 or data[position - 1 : position] == b"\n":
            return position
        position = data.rfind(written, 0, position)
    return -1


def _without_appended_exclude_lines(
    data: bytes, appended: "tuple[bytes, ...]"
) -> bytes:
    """``data`` with one occurrence of each launch append removed.

    ``appended`` records the byte writes this *specific* launch made to
    ``info/exclude``. Remove one whole-line occurrence per write — see
    :func:`_appended_line_position` — leaving a duplicate matching line visible
    as a mutation. A matching pattern the owner wrote before the launch is
    preserved because the writer does not append it, and bytes a worker writes
    around the launch's line are never taken with it. Every other byte — CRLF
    terminators, comments, leading whitespace, non-UTF8 owner patterns — is
    kept as it stood.
    """

    normalized = data
    for written in reversed(appended):
        # Reversed: the last write this launch made is the one nearest the end
        # of the file, so it is located and removed first.
        position = _appended_line_position(normalized, written)
        if position >= 0:
            normalized = normalized[:position] + normalized[position + len(written) :]
    return normalized


def ensure_lane_exclusion(
    repo: Path,
    *,
    runner: Runner = subprocess.run,
    appended_exclude_lines: "list[bytes] | None" = None,
) -> Path:
    """Exclude ``.side-lanes/`` from ``git status`` in the coordinator checkout.

    Lane worktrees live under the governed repository so their path is
    predictable and audited, but they are never committed. Without this entry
    the first lane leaves an untracked directory behind and every later launch
    fails the dirty-checkout check. ``.git/info/exclude`` is local, untracked,
    and idempotent to update, so no repository file changes.
    """

    # `--path-format=absolute` needs Git 2.31+; anchor a relative answer here instead.
    git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = existing.splitlines()

        # Match only the exact tool-written entries; Git ignores trailing
        # spaces/tabs, so tolerate those, but a leading-whitespace line is a
        # different (user-authored) pattern and is left alone.
        def is_entry(line: str, pattern: str) -> bool:
            return line.rstrip(" \t") == pattern

        if any(is_entry(line, LEGACY_EXCLUDE_PATTERN) for line in lines):
            # Upgrade the unanchored entry in place so nested directories of the
            # same name become visible to the dirty check again; keep one copy.
            rewritten: list[str] = []
            seen_current = False
            for line in lines:
                if is_entry(line, LEGACY_EXCLUDE_PATTERN) or is_entry(
                    line, LANE_EXCLUDE_PATTERN
                ):
                    if seen_current:
                        continue
                    seen_current = True
                    rewritten.append(LANE_EXCLUDE_PATTERN)
                else:
                    rewritten.append(line)
            exclude.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            return exclude
        if any(is_entry(line, LANE_EXCLUDE_PATTERN) for line in lines):
            return exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        appended = f"{prefix}{LANE_EXCLUDE_PATTERN}\n".encode("ascii")
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(appended.decode("ascii"))
        if appended_exclude_lines is not None:
            appended_exclude_lines.append(appended)
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude lane worktrees in {exclude}: {exc}"
        ) from exc
    return exclude


def ensure_scratch_exclusion(
    repo: Path,
    *,
    runner: Runner = subprocess.run,
    appended_exclude_lines: "list[bytes] | None" = None,
) -> Path:
    """Exclude ``.side-lane-scratch/`` from ``git status`` in every checkout.

    Lane scratch files are throwaway by contract: the runner pre-creates the
    directory in each lane worktree, and without this entry it would itself
    fail the dirty checks that guard lane creation and disposal. The entry is
    root-anchored (only the worktree-root scratch directory is reserved, so a
    project directory such as ``src/.side-lane-scratch/`` stays visible) and,
    like the lane-root entry above, is shared with linked worktrees through
    the common ``.git/info/exclude``. When ``appended_exclude_lines`` is
    supplied, the exact line bytes this invocation appended are recorded in
    it, so a launch can tell the audit which exclude lines are its own writes
    and which were there before it started.
    """

    git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = existing.splitlines()

        def is_entry(line: str, pattern: str) -> bool:
            return line.rstrip(" \t") == pattern

        if any(is_entry(line, LEGACY_SCRATCH_EXCLUDE_PATTERN) for line in lines):
            # Upgrade the unanchored entry in place so nested directories of
            # the same name become visible to the dirty check again; keep one
            # copy. This rewrites the file but adds no new line, so it is not
            # recorded in ``appended_exclude_lines`` — there is nothing new to
            # normalize out of the audit.
            rewritten: list[str] = []
            seen_current = False
            for line in lines:
                if is_entry(line, LEGACY_SCRATCH_EXCLUDE_PATTERN) or is_entry(
                    line, SCRATCH_EXCLUDE_PATTERN
                ):
                    if seen_current:
                        continue
                    seen_current = True
                    rewritten.append(SCRATCH_EXCLUDE_PATTERN)
                else:
                    rewritten.append(line)
            exclude.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            return exclude
        if any(is_entry(line, SCRATCH_EXCLUDE_PATTERN) for line in lines):
            return exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        appended = f"{prefix}{SCRATCH_EXCLUDE_PATTERN}\n".encode("ascii")
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(appended.decode("ascii"))
        if appended_exclude_lines is not None:
            appended_exclude_lines.append(appended)
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude scratch directory in {exclude}: {exc}"
        ) from exc
    return exclude


def ensure_devin_local_mcp_exclusion(
    repo: Path,
    *,
    runner: Runner = subprocess.run,
    appended_exclude_lines: "list[bytes] | None" = None,
) -> tuple[Path, bool]:
    """Exclude the Devin per-run local MCP file from ``git status``/``git add -A``.

    The Devin adapter materializes ``.devin/mcp_config.local.json`` inside the
    lane worktree for the run's lifetime (restored or removed on exit). Without
    this entry a worker's mid-run ``git add -A`` would commit the generated
    file — URL plus ``${ENV}`` credential reference, never a value. The entry
    is root-anchored and shared with linked worktrees through the common
    ``.git/info/exclude``. Excludes never apply to tracked files, so the
    adapter rejects a tracked local config before merge; an existing untracked
    file's content is preserved by the merge/restore path. The entry only hides
    the untracked file from status and add-globbing. Return the exclude path and
    whether this invocation appended the entry; a failed restore may remove
    only an entry this invocation created.
    """

    git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_bytes() if exclude.is_file() else b""
        if any(
            _exclude_entry_is_tool_written(content, DEVIN_LOCAL_MCP_EXCLUDE_PATTERN)
            for content, _terminator in _lf_segments(existing)
        ):
            return exclude, False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        # The file is bytes, not text — an owner's pattern in another encoding
        # must not make a launch abort — so the separator is computed from the
        # raw last byte: a file whose last byte is ``\n`` does not need another
        # one, an empty file or any other trailing byte does. Every other byte
        # of the owner's file is preserved exactly as it stood.
        if existing.endswith(b"\n") or not existing:
            prefix = b""
        else:
            prefix = b"\n"
        appended = prefix + DEVIN_LOCAL_MCP_EXCLUDE_PATTERN.encode("ascii") + b"\n"
        with exclude.open("ab") as handle:
            handle.write(appended)
        if appended_exclude_lines is not None:
            appended_exclude_lines.append(appended)
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude the Devin local MCP config in {exclude}: {exc}"
        ) from exc
    return exclude, True


def lift_devin_local_mcp_exclusion(repo: Path, *, runner: Runner = subprocess.run) -> bool:
    """Stop excluding the Devin per-run local MCP file in this repository.

    The counterpart of :func:`ensure_devin_local_mcp_exclusion`, and it exists
    for exactly one state: a run that could not put the generated file back has
    left it on disk, and the entry this run added at launch would then hide it
    from ``git status`` — which is how a leftover would escape the workspace
    measurement that is supposed to report it. Removing the entry makes the
    leftover an ordinary untracked path again, so the post-run audit and the
    next baseline both see it. A merge failure before launch also removes a
    newly added entry, even for a disposable lane, because the exclude file
    belongs to the shared Git repository and survives that lane's disposal.

    Only the exact tool-written entry is removed — a leading-whitespace line is
    a user-authored pattern and is left alone, on the same rule the writer
    uses. Every other byte of the file is preserved: the file is read and
    written as bytes, lines are split on ``\\n`` alone, and each surviving
    line keeps its own terminator, so a CRLF line stays CRLF, a file whose last
    line has no newline keeps it that way, and bytes that are not UTF-8 (an
    owner's pattern in another encoding) do not make the lift fail. Rewriting
    the file from decoded text instead would translate every line ending and
    append a newline the file never had, which is an edit to content this
    function was not asked to touch. Returns whether an entry was removed. A
    repository whose exclude file cannot be read or rewritten is not an error
    to raise: this runs while a failure is already being reported, so the
    return value carries the fact and the caller names it in its own message
    instead.

    The entry lives in the common ``.git/info/exclude``, so while it is absent
    a concurrent Devin lane in another worktree of this repository would no
    longer have its own generated file hidden from a mid-run ``git add -A``.
    That is the deliberate direction of the trade: the entry is a run-lifetime
    hiding mechanism, a leftover file is exactly the state it must stop hiding,
    and the window only exists after a run has already failed closed.
    """

    try:
        git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        exclude = git_dir / "info" / "exclude"
        if not exclude.is_file():
            return False
        kept = []
        removed = False
        for content, terminator in _lf_segments(exclude.read_bytes()):
            if _exclude_entry_is_tool_written(content, DEVIN_LOCAL_MCP_EXCLUDE_PATTERN):
                removed = True
                continue
            kept.append(content + terminator)
        if not removed:
            return False
        exclude.write_bytes(b"".join(kept))
    except (OSError, WorktreeError):
        return False
    return True


def ensure_devin_local_mcp_untracked(repo: Path, *, runner: Runner = subprocess.run) -> None:
    """Fail closed before merging a generated MCP file into a tracked path.

    ``.git/info/exclude`` protects untracked files only. A tracked local MCP
    file would be modified by the additive merge and could be staged by a
    worker before the post-run restore, so it cannot be used as the temporary
    delivery target.
    """

    try:
        result = runner(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--",
             DEVIN_LOCAL_MCP_RELATIVE_PATH],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise WorktreeError(f"could not inspect tracked Devin MCP config: {exc}") from exc
    if result.returncode == 0 and result.stdout.strip():
        raise WorktreeError(
            f"cannot deliver per-run Devin MCP config because "
            f"{DEVIN_LOCAL_MCP_RELATIVE_PATH} is tracked"
        )
    if result.returncode != 1:
        raise WorktreeError(
            (result.stderr or result.stdout).strip()
            or "could not determine whether the Devin MCP config is tracked"
        )


def prepare_scratch_directory(
    repo: Path,
    worktree: Path,
    *,
    runner: Runner = subprocess.run,
    appended_exclude_lines: "list[bytes] | None" = None,
) -> Path:
    """Create ``<worktree>/.side-lane-scratch`` for run-local throwaway files.

    Scratch (trial scripts, notes, intermediate output) belongs inside the lane
    worktree yet must never reach ``git status`` or ``git add -A``, so the
    exclusion is ensured before the directory exists. It is not removed
    per-run: a review lane is disposed with its whole worktree, and an execute
    lane's worktree — scratch included — is preserved for the coordinator's
    review until the lane branch is dealt with. ``appended_exclude_lines`` is
    threaded to the underlying exclusion writer so an existing-workspace launch
    can record the exact bytes this run added, for the info-state audit.
    """

    ensure_scratch_exclusion(
        repo, runner=runner, appended_exclude_lines=appended_exclude_lines
    )
    scratch = worktree / SCRATCH_DIR_NAME
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(
            f"cannot create scratch directory {scratch}: {exc}"
        ) from exc
    return scratch


WORKTREE_ROOT_ENV = "SIDE_LANE_WORKTREE_ROOT"


def resolve_worktree_root(
    repo: Path, override: str | None = None, *, env: "dict[str, str] | None" = None
) -> Path:
    """Directory that receives lane worktrees.

    Default is ``<repo>/.side-lanes/worktrees`` (kept out of ``git status`` via
    ``.git/info/exclude``). Repositories whose policy forbids nested checkouts
    pass ``--worktree-root`` or set ``SIDE_LANE_WORKTREE_ROOT``; a relative
    value is anchored to the repository root (``../lanes`` is a sibling dir).
    The override may not point inside the repository except at the default
    location, so a typo cannot quietly create an un-excluded nested checkout.
    """

    import os

    raw = (
        override
        if override
        else (os.environ if env is None else env).get(WORKTREE_ROOT_ENV, "")
    )
    default = repo / LANE_ROOT / "worktrees"
    if not raw or not raw.strip():
        return default
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = Path(os.path.normpath(candidate))
    if candidate == default:
        return default
    # Reject both a lexically nested root (even if it is a symlink out of the
    # repository, it is still an in-repo entry indexers walk) and a root whose
    # real path resolves back into the repository through a symlink.
    real_candidate = Path(os.path.realpath(candidate))
    real_repo = Path(os.path.realpath(repo))
    if real_candidate == Path(os.path.realpath(default)):
        return default
    if (
        candidate == repo
        or candidate.is_relative_to(repo)
        or real_candidate == real_repo
        or real_candidate.is_relative_to(real_repo)
    ):
        raise WorktreeError(
            f"worktree root must be outside the repository (or the default {default}): {candidate}"
        )
    return candidate


def _discard_failed_worktree(
    repo: Path, worktree: Path, branch: str, runner: Runner
) -> str | None:
    """Best-effort removal of a linked worktree and branch left by a failed create.

    ``git worktree add`` already succeeded when this runs, so a later failure
    (e.g. scratch-directory setup) must not leak the linked worktree or its
    branch. Removal failures are swallowed for control flow but their output
    is returned so the caller can fold it into the original error instead of
    silently discarding it.
    """

    problems: list[str] = []
    try:
        result = runner(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            problems.append(
                f"worktree removal failed: {(result.stderr or result.stdout).strip()}"
            )
    except OSError as exc:
        problems.append(f"worktree removal failed: {exc}")
    try:
        result = runner(
            ["git", "-C", str(repo), "branch", "-D", branch],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            problems.append(
                f"branch removal failed: {(result.stderr or result.stdout).strip()}"
            )
    except OSError as exc:
        problems.append(f"branch removal failed: {exc}")
    return "; ".join(problems) or None


def create_worktree(
    repo: Path,
    lane_name: str,
    *,
    runner: Runner = subprocess.run,
    now: datetime | None = None,
    worktree_root: str | Path | None = None,
) -> WorktreeRun:
    repo = repo.resolve()
    top = Path(_git(repo, ["rev-parse", "--show-toplevel"], runner)).resolve()
    if top != repo:
        raise WorktreeError("execute mode requires the repository root")
    root = resolve_worktree_root(
        repo, None if worktree_root is None else str(worktree_root)
    )
    nested = root == repo / LANE_ROOT / "worktrees"
    if nested:
        ensure_lane_exclusion(repo, runner=runner)
    if _git(repo, ["status", "--porcelain"], runner):
        raise WorktreeError(
            "coordinator checkout is dirty; commit or stash before lane creation"
        )
    if not _git(repo, ["branch", "--show-current"], runner):
        raise WorktreeError("detached HEAD is not supported")
    safe = safe_lane_name(lane_name)
    starting_commit = _git(repo, ["rev-parse", "HEAD"], runner)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    branch = f"side-lane/{safe}-{stamp}"
    worktree = root / f"{safe}-{stamp}"
    _git(repo, ["check-ref-format", "--branch", branch], runner)
    if worktree.exists():
        raise WorktreeError(f"worktree destination already exists: {worktree}")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"cannot create worktree root {root}: {exc}") from exc
    _git(repo, ["worktree", "add", "-b", branch, str(worktree), "HEAD"], runner)
    try:
        prepare_scratch_directory(repo, worktree, runner=runner)
    except WorktreeError as exc:
        cleanup_problem = _discard_failed_worktree(repo, worktree, branch, runner)
        message = str(exc)
        if cleanup_problem:
            message += f" (cleanup also failed: {cleanup_problem})"
        raise WorktreeError(message) from exc
    return WorktreeRun(repo, worktree, branch, safe, starting_commit)


def git_status(run: WorktreeRun, runner: Runner = subprocess.run) -> str:
    return _git(run.worktree, ["status", "--short", "--branch"], runner)


#: Porcelain status code for a path git tracks nothing at. Every other code —
#: `` M``, ``M ``, ``A ``, `` D``, ``R ``, ``UU`` — describes a change to a
#: path git *does* track, which is a different fact about a lane than an
#: untracked file that happens to share its directory.
UNTRACKED_STATUS = "??"


@dataclass(frozen=True)
class ChangedPath:
    """One path in a lane's git status, with git's own two-character code.

    Path alone is not enough to judge a lane: an untracked scratch artifact
    and a *tracked* file under the same prefix are one identical string to a
    path-only API while being opposite facts. The status is the half that
    tells them apart, so it is carried here rather than discarded at parse
    time and re-guessed from the path.
    """

    status: str
    path: str

    @property
    def untracked(self) -> bool:
        """True when git tracks nothing at this path (porcelain ``??``)."""
        return self.status == UNTRACKED_STATUS


@dataclass(frozen=True)
class LaneDelivery:
    """Whether a lane's work actually reached git.

    A worker can write correct files, exit 0, and leave them untracked. The
    run then reports success for work that disappears with the worktree —
    observed three times in one session (2026-09-17). Exit status cannot see
    this; only the tree can.

    ``uncommitted`` stays the path list every existing caller reads; ``changed``
    carries the same paths with their statuses for the callers that must tell a
    tracked change from an untracked addition. It is empty on a hand-built
    instance, which is not the same as "every path is untracked" — a caller
    that needs the status must fail closed when it is missing.
    """

    committed: bool
    uncommitted: tuple[str, ...]
    changed: tuple[ChangedPath, ...] = ()

    @property
    def delivered(self) -> bool:
        return self.committed and not self.uncommitted

    def failure_reason(self) -> str | None:
        """Why this lane did not deliver, phrased for the operator, or None."""
        if self.delivered:
            return None
        if self.uncommitted:
            listed = "\n  ".join(self.uncommitted[:20])
            more = (
                ""
                if len(self.uncommitted) <= 20
                else (f"\n  ... and {len(self.uncommitted) - 20} more")
            )
            committed_note = (
                "committed some work but left these uncommitted"
                if self.committed
                else "wrote these files and committed nothing"
            )
            return (
                f"lane {committed_note}:\n  {listed}{more}\n"
                "Nothing uncommitted survives the worktree. Commit and push in the "
                "lane prompt, or pass --allow-no-commit if producing no commit is "
                "the intended outcome."
            )
        return (
            "lane produced no commit and no file changes. The worker exited 0 "
            "having changed nothing — usually a refused tool call, an "
            "unanswerable question, or a prompt it treated as already done. "
            "Read the run artifact's stderr, or pass --allow-no-commit if this "
            "is the intended outcome."
        )


def changed_paths(
    worktree: Path, runner: Runner, *, untracked_all: bool = False
) -> tuple[ChangedPath, ...]:
    """Every path with uncommitted work, from NUL-delimited porcelain.

    ``untracked_all`` enumerates the files inside an untracked directory
    individually (porcelain ``-uall``) instead of collapsing it to one
    ``dir/`` entry. A lane's own status reads well either way, but a
    content-and-index baseline cannot: with the collapsed form, a worker's
    edit to one file inside a directory that was already untracked leaves the
    single reported path — and therefore any path-only comparison — unchanged.

    `-z` rather than parsing text, for two reasons learned the hard way:

    * A fixed `line[3:]` slice is wrong, because `_git` strips its output and
      the leading space of an unstaged ` M path` entry is gone by parse time —
      the slice then eats the first character and names a file that does not
      exist.
    * Splitting a path on `" -> "` to find a rename is wrong too: an untracked
      file may legitimately be named `a -> b.txt`, and the operator gets sent
      to `b.txt`. In `-z` mode a rename is structural — the record is followed
      by a separate NUL-terminated field holding the ORIGINAL path — so it is
      consumed by position, never by pattern.

    Paths are reported as git spells them, relative to the worktree root, each
    with the status git reported for it.
    """
    command = ["git", "-C", str(worktree), "status", "--porcelain", "-z"]
    if untracked_all:
        command.append("-uall")
    result = runner(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorktreeError(
            (result.stderr or result.stdout).strip() or "git status failed"
        )
    fields = [f for f in (result.stdout or "").split("\0") if f]
    entries: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        entries.append(ChangedPath(status, path))
        if "R" in status or "C" in status:
            index += 1  # the original path rides in its own field; skip it
    return tuple(entries)


def _changed_paths(worktree: Path, runner: Runner) -> tuple[str, ...]:
    """Every path with uncommitted work, as bare lane-relative paths."""
    return tuple(entry.path for entry in changed_paths(worktree, runner))


def lane_delivery(run: WorktreeRun, runner: Runner = subprocess.run) -> LaneDelivery:
    """Inspect the lane worktree for work that actually landed in git."""
    head = _git(run.worktree, ["rev-parse", "HEAD"], runner)
    changed = changed_paths(run.worktree, runner)
    return LaneDelivery(
        committed=head != run.starting_commit,
        uncommitted=tuple(entry.path for entry in changed),
        changed=changed,
    )


def snapshot_source(repo: Path, runner: Runner = subprocess.run) -> frozenset[str]:
    """The coordinator checkout's changed paths, taken just before dispatch.

    The snapshot is deliberately narrow: git's own porcelain path list, not a
    content hash of the tree. Same-user execute lanes are not an OS sandbox,
    so a worker CAN write outside its worktree — observed 2026-09-19, when a
    worker wrote its report to the coordinator source path, corrected course,
    and the run still reported a clean accepted delivery because nothing ever
    looked at the source checkout. Comparing this baseline against the
    checkout after the run is what turns that write into a named, failed
    delivery instead of a silent one.
    """

    return frozenset(_changed_paths(repo, runner))


def source_mutations(
    repo: Path,
    baseline: "frozenset[str]",
    runner: Runner = subprocess.run,
) -> tuple[str, ...]:
    """Paths that appeared in the coordinator checkout's status during the run.

    Only the delta over ``baseline`` is returned: paths the checkout already
    carried when the worker was dispatched are preexisting and must not be
    blamed on the lane. Lane-owned artifacts never appear here — the lane
    worktrees and scratch directories are git-excluded, and the run audit
    lives inside ``.git`` — so anything in the delta is a real source-tree
    change. When a person or another process edits the same checkout
    concurrently, the delta still reports the change honestly; attribution is
    not claimed either way.
    """

    after = _changed_paths(repo, runner)
    return tuple(sorted(set(after) - set(baseline)))


def _absolute_git_path(base: Path, raw: str) -> Path:
    """Resolve one ``git rev-parse`` path answer against ``base``."""

    path = Path(raw)
    return Path(os.path.realpath(path if path.is_absolute() else base / path))


def is_linked_worktree(workspace: Path, runner: Runner = subprocess.run) -> bool:
    """True when ``workspace`` is a linked worktree, not its own checkout.

    A linked worktree's ``--git-dir`` is a per-worktree directory under the
    common one; a checkout that owns its repository answers the same path for
    both questions. That is the fact an existing-workspace receipt needs to
    record a deliberate selection of the shared primary checkout rather than
    let it read as a default.
    """

    repo = workspace.resolve()
    git_dir = _absolute_git_path(repo, _git(repo, ["rev-parse", "--git-dir"], runner))
    common = _absolute_git_path(
        repo, _git(repo, ["rev-parse", "--git-common-dir"], runner)
    )
    return git_dir != common


EXISTING_WORKSPACE_LOCK_DIR = "side-lane-workspace-locks"
WORKSPACE_LOCK_SCHEMA_VERSION = 1
#: A baseline identity is a digest, and its only use is comparison, so the
#: short form is enough to read two records side by side by eye.
WORKSPACE_DIGEST_CHARS = 12
#: What a path that exists but cannot be hashed as a file — a directory (a
#: submodule), a FIFO, a socket, a device — is recorded as. Reading a FIFO
#: would block this walk forever, so no such path is ever opened, and the
#: sentinel keeps "present, not a hashable kind" distinct from both "absent"
#: (``None``) and "present and readable in principle, but not readable now".
WORKSPACE_UNHASHABLE = "unhashable"
#: What a path of a hashable kind is recorded as when reading it failed —
#: ``lstat`` or the open refused, typically ``EACCES``. This is a third state,
#: not a synonym for either neighbour: recording a failed read as absent or as
#: the unsupported-kind sentinel would let two states nobody read compare
#: equal, and a workspace could be reported as proved unchanged on the
#: strength of two measurements that never happened.
WORKSPACE_UNREADABLE = "unreadable"
#: The two sentinels above are **not** comparable content values, and this
#: tuple is the one place that says so. Two states nobody measured are not two
#: states that agree: ``unhashable == unhashable`` is true of the sentinel
#: string and false as a statement about the workspace, so any comparison that
#: reads it as "unchanged" reports a verdict no measurement supports. A
#: directory git reports as a dirty submodule, and an untracked nested
#: repository, are both of that kind — a worker's edit inside one cannot be
#: seen from here at all, and must be reported as *not measured* rather than as
#: nothing having happened.
WORKSPACE_UNMEASURED = (WORKSPACE_UNHASHABLE, WORKSPACE_UNREADABLE)
#: What an ignored path's status is recorded as. It is not git's porcelain
#: code for the path — ``git status`` lists an ignored file as ``!!`` only when
#: asked to, and reports an ignored directory as one entry — but from the
#: measurement's side an ignored file is a path that exists and is not part of
#: the tracked tree, and it has to be told apart from an untracked addition
#: (``??``) in the record.
_IGNORED_STATUS = "!!"
#: Bounds on the pre-existing ignored set a baseline measures. Ignored files
#: are invisible to the status and index readings, so a baseline that skipped
#: them could not tell an edit to one from no change at all, and a worker's
#: legitimate visible delta would then carry an edit a secret-bearing
#: ``.env`` in its shadow. They are measured instead — but the ignored tree of
#: a real repository is a dependency, build or cache directory that can hold
#: millions of files, and hashing all of it before every launch is not a
#: measurement, it is a hang. The bound is on the set, not on any one entry
#: (an entry over the byte bound is still measured: what the bound protects is
#: the walk, and a single file's size does not change its length), and crossing
#: it fails closed before the worker exists rather than baselining a subset,
#: which would be the same blind spot with a quieter alias.
WORKSPACE_IGNORED_PATH_LIMIT = 20000
WORKSPACE_IGNORED_BYTE_LIMIT = 256 * 1024 * 1024
#: The directories this tool creates inside a workspace, repository-relative
#: and root-anchored. Both are git-excluded by entries the tool writes, both
#: are created and grown by the tool and its workers during the run — a scratch
#: note, a lane's own checkouts — and neither is the owner's work, so neither
#: belongs in the owner's baseline. They are excluded from the ignored read
#: rather than measured and then subtracted: an entry whose path the tool owns
#: is not a measurement of the workspace at all.
_TOOL_OWNED_ROOTS = (LANE_ROOT, SCRATCH_DIR_NAME)


def _is_tool_owned_path(path: str) -> bool:
    """Whether a workspace-relative path is inside one of the tool's own trees."""

    return any(
        path == name or path.startswith(f"{name}/") for name in _TOOL_OWNED_ROOTS
    )


def _workspace_ignored_paths(workspace: Path, runner: Runner) -> "list[str]":
    """Every pre-existing ignored file in the workspace, bounded, sorted.

    ``git ls-files --others --ignored --exclude-standard`` enumerates the
    individual ignored files. ``git status --porcelain --ignored=matching``
    cannot: with a directory-ignoring pattern it collapses the whole directory
    into one entry, so an edit inside it would be compared against a directory
    name. The tool's own trees are dropped here, and the count is checked
    against :data:`WORKSPACE_IGNORED_PATH_LIMIT` before any of them is read.
    """

    result = runner(
        [
            "git",
            "-C",
            str(workspace),
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorktreeError(
            (result.stderr or result.stdout).strip() or "git ls-files failed"
        )
    paths = sorted(
        {
            path
            for path in (result.stdout or "").split("\0")
            if path and not _is_tool_owned_path(path)
        }
    )
    if len(paths) > WORKSPACE_IGNORED_PATH_LIMIT:
        raise WorktreeError(
            f"the existing workspace {workspace} holds at least {len(paths)} "
            f"ignored path(s), at or beyond this run's bounded limit of "
            f"{WORKSPACE_IGNORED_PATH_LIMIT} ignored path(s). An ignored tree "
            "that large cannot be measured before the worker starts, and a "
            "baseline that skipped it would report an edit inside it as no "
            "change. Prune the ignored tree — a dependency, build or cache "
            "directory is the usual one — or point --existing-workspace at a "
            "checkout without it."
        )
    return paths


def _ignored_baseline_entries(
    workspace: Path, paths: "list[str]", measured: "set[str]"
) -> "list[WorkspaceEntry]":
    """A baseline entry for every pre-existing ignored file, within the bound.

    ``measured`` is the set of paths the status/index readings already cover,
    which wins: an ignored path's identity comes from one reading, not two.
    What is measured is the same one-way content digest every other entry uses,
    so an ignored file's *contents* never reach the record or the audit — an
    ignored ``.env`` is reported as changed, never as what it holds. The byte
    total is accumulated as the entries are built and checked against
    :data:`WORKSPACE_IGNORED_BYTE_LIMIT`, so crossing it raises here rather than
    after the whole tree has been read.
    """

    entries: "list[WorkspaceEntry]" = []
    total = 0
    for path in paths:
        if path in measured:
            continue
        try:
            total += os.lstat(workspace / path).st_size
        except OSError:
            pass
        if total > WORKSPACE_IGNORED_BYTE_LIMIT:
            raise WorktreeError(
                f"the ignored paths of the existing workspace {workspace} hold "
                f"at least {total} byte(s), at or beyond this run's bounded "
                f"limit of {WORKSPACE_IGNORED_BYTE_LIMIT} byte(s). A tree that "
                "large cannot be measured before the worker starts, and a "
                "baseline that skipped it would report an edit inside it as no "
                "change. Prune the ignored tree — a dependency, build or cache "
                "directory is the usual one — or point --existing-workspace at "
                "a checkout without it."
            )
        content, mode = _worktree_identity(workspace / path)
        entries.append(
            WorkspaceEntry(
                path=path,
                status=_IGNORED_STATUS,
                content=content,
                index=None,
                mode=mode,
                index_mode=None,
                index_stages=(),
                flag=None,
            )
        )
    return entries


def entry_is_unmeasured(entry: "WorkspaceEntry | None") -> bool:
    """Whether this entry's content is a sentinel rather than a measurement."""

    return entry is not None and entry.content in WORKSPACE_UNMEASURED


@dataclass(frozen=True)
class WorkspaceEntry:
    """One path's identity in a workspace: git's status, content, mode, index.

    A path alone cannot answer whether a file that was already dirty changed
    again, and a status alone cannot either: `` M`` before and `` M`` after is
    the same two characters for an untouched file and for one a worker
    rewrote — and the same two again for one a worker chmodded. Every digest
    here is one-way, so an audit can publish the identity of a path —
    including a dirty one — without publishing the file.
    """

    path: str
    #: Git's own two-character porcelain code, so the record distinguishes a
    #: tracked modification from an untracked addition.
    status: str
    #: Hex sha256 of the working-tree content, ``WORKSPACE_UNHASHABLE`` for a
    #: path that is not of a hashable kind, ``WORKSPACE_UNREADABLE`` when it is
    #: but could not be read, or ``None`` when it is not there at all.
    content: "str | None"
    #: The blob oid git's index records for this path at stage 0, or ``None``
    #: when the index has no stage-0 entry for it (an untracked path, or one a
    #: conflict left unmerged).
    index: "str | None"
    #: The working tree path's own mode in git's vocabulary — ``100644``,
    #: ``100755``, ``120000`` for a symlink — ``None`` when it is not there, or
    #: ``WORKSPACE_UNHASHABLE`` for a kind git holds no mode for. A chmod is a
    #: change a content digest cannot see: the bytes are identical and the
    #: porcelain code can be too.
    mode: "str | None"
    #: The mode of the index's own stage-0 entry — what a ``git add`` staged.
    #: A staged chmod moves this and leaves the oid, the content and the
    #: status exactly as they were.
    index_mode: "str | None"
    #: The nonzero-stage entries of an unmerged path, each written
    #: ``"<stage>:<mode>:<oid>"`` and sorted, so one conflict state is told
    #: from another rather than all of them collapsing to "no stage-0 entry".
    index_stages: tuple[str, ...]
    #: Git's own index tag for this path when it is not the ordinary cached
    #: ``H`` — ``h`` for assume-unchanged, ``S`` for skip-worktree, and the
    #: rest of what ``git ls-files -v`` prints. ``None`` for a path with no
    #: index entry at all (an untracked one) and for one carrying the default
    #: tag. A path's status and content can be identical on both sides of a run
    #: while this moved, and a path that only *has* this field is one git has
    #: stopped reporting entirely — so the flag is compared, not assumed.
    flag: "str | None" = None

    def short(self, value: "str | None") -> "str | None":
        """The readable form of one digest, or the value itself."""

        if value is None or value in (WORKSPACE_UNHASHABLE, WORKSPACE_UNREADABLE):
            return value
        return value[:WORKSPACE_DIGEST_CHARS]

    def short_stage(self, record: str) -> str:
        """The readable form of one conflict-stage record, oid shortened."""

        stage, _, rest = record.partition(":")
        mode, _, oid = rest.partition(":")
        return f"{stage}:{mode}:{oid[:WORKSPACE_DIGEST_CHARS]}"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "content": self.short(self.content),
            "mode": self.short(self.mode),
            "index": self.short(self.index),
            "index_mode": self.short(self.index_mode),
            "index_stages": [self.short_stage(item) for item in self.index_stages],
            "flag": self.flag,
        }


def _worktree_identity(path: Path) -> "tuple[str | None, str | None]":
    """One path's working-tree identity — content digest and mode — from one
    ``lstat``.

    The digest is one-way and never reads the content out. Symlinks are hashed
    by their target, not followed — the branch a lane runs on is not the lane's
    own edit, and a link whose target is a directory or an absent file still
    has a stable identity.

    The three ways a path can be unmeasurable stay three distinct answers,
    because collapsing them is what would let two states nobody read compare
    equal: ``(None, None)`` for a path that is genuinely not there, the
    ``WORKSPACE_UNHASHABLE`` sentinel for a kind that is never opened (reading
    a FIFO would block this walk forever), and ``WORKSPACE_UNREADABLE`` for a
    path of a readable kind whose ``lstat`` or read refused. Only that last one
    is unverified, and it is never reported as a digest.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError:
        return WORKSPACE_UNREADABLE, WORKSPACE_UNREADABLE
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            return None, None
        except OSError:
            return WORKSPACE_UNREADABLE, WORKSPACE_UNREADABLE
        return hashlib.sha256(b"symlink:" + os.fsencode(target)).hexdigest(), "120000"
    if not stat.S_ISREG(info.st_mode):
        return WORKSPACE_UNHASHABLE, WORKSPACE_UNHASHABLE
    mode = "100755" if info.st_mode & 0o111 else "100644"
    digest = hashlib.sha256()
    try:
        with open(path, "rb", buffering=0) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return WORKSPACE_UNREADABLE, mode
    return digest.hexdigest(), mode


#: Paths per ``git ls-files`` call. The argv list is bounded rather than the
#: whole dirty set, so a workspace with thousands of changed paths still reads
#: its index in a handful of git calls.
_INDEX_BATCH_PATHS = 128


@dataclass(frozen=True)
class WorkspaceIndexRecord:
    """One path's staged record in git's index, whole.

    ``oid`` and ``mode`` are the stage-0 entry — what the index holds for an
    ordinary, unconflicted path — and ``stages`` holds the nonzero-stage
    entries of an unmerged one, each ``"<stage>:<mode>:<oid>"``. Keeping only
    the oid would lose a staged chmod, which changes the mode and nothing else;
    keeping only the last line ``ls-files`` printed for a path would lose the
    conflict entries the line before it carried.
    """

    oid: "str | None"
    mode: "str | None"
    stages: tuple[str, ...] = ()


def _index_records(
    workspace: Path, paths: Sequence[str], runner: Runner
) -> "dict[str, WorkspaceIndexRecord]":
    """Index record for each tracked path, from batched ``ls-files`` reads.

    Each path is passed as a literal pathspec, so a file whose name contains a
    glob character is named rather than expanded. Every column ``ls-files``
    prints is kept: mode and stage as well as the blob oid, and every line for
    a path rather than whichever one came last.
    """

    records: dict[str, WorkspaceIndexRecord] = {}
    for start in range(0, len(paths), _INDEX_BATCH_PATHS):
        batch = [f":(literal){path}" for path in paths[start:start + _INDEX_BATCH_PATHS]]
        result = runner(
            ["git", "-C", str(workspace), "ls-files", "-s", "-z", "--", *batch],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            raise WorktreeError(
                (result.stderr or result.stdout).strip() or "git ls-files failed"
            )
        oids: dict[str, str] = {}
        modes: dict[str, str] = {}
        stages: dict[str, list[str]] = {}
        for record in (result.stdout or "").split("\0"):
            if not record:
                continue
            meta, separator, path = record.partition("\t")
            fields = meta.split()
            if not separator or len(fields) < 3:
                continue
            mode, oid, stage = fields[0], fields[1], fields[2]
            if stage == "0":
                oids[path], modes[path] = oid, mode
            else:
                stages.setdefault(path, []).append(f"{stage}:{mode}:{oid}")
        for path in set(oids) | set(modes) | set(stages):
            records[path] = WorkspaceIndexRecord(
                oids.get(path),
                modes.get(path),
                tuple(sorted(stages.get(path, ()))),
            )
    return records


@dataclass(frozen=True)
class WorkspaceGitState:
    """The workspace's git state that is not a path: refs, config, worktrees,
    the object store, the reflog, the ``info`` metadata, the pseudorefs and
    in-progress operation state, and the object metadata.

    A worker told to make no git write of any kind can write git state that no
    path comparison sees. ``git update-ref`` moves a ref, ``git symbolic-ref``
    moves HEAD without a commit, ``git config`` rewrites the local config,
    ``git worktree add`` registers a second working tree, ``git hash-object -w``
    and ``git commit-tree`` write objects only the object store sees, and
    ``git reflog expire`` and ``git reflog delete`` rewrite the reflog without
    changing anything the rest of this record holds. None of them appear in
    ``git status``, so a path-only baseline — however complete its dirty set —
    reports such a run as *no change*, which is a false statement about the
    owner's checkout. These eight readings are the rest of the state, taken
    with the baseline and compared with it, and they are what a deny list
    cannot stand in for: a command-string rule covers the spellings it was
    written for, while this covers whatever the worker actually did.

    The three readings beyond the first five close writes that leave each of
    those five exactly as they were: ``git fetch`` rewrites ``FETCH_HEAD`` and
    can move nothing else, ``git cherry-pick`` and a paused ``rebase`` leave
    state under ``.git/sequencer/`` and ``.git/rebase-merge/``, a
    ``.git/info/attributes`` or ``.git/info/exclude`` edit changes ``git
    diff``'s and git's ignore rules without touching a byte of the tracked
    tree, and ``git commit-graph write`` or ``git multi-pack-index write``
    rewrites the object metadata while ``git count-objects -v`` stays put.

    The config is a **digest**, not its text: a local config legitimately holds
    a remote URL with an embedded credential, and the run audit is published.
    It covers both what git parses from the local config and the raw bytes of
    every local config file that governs the workspace — the common ``config``
    and each ``config.worktree`` git would read for this workspace — because a
    hand-edit that only adds a comment or a blank line changes the file and
    nothing in the parse.
    Refs are recorded as ``(name, oid)`` pairs, worktrees as paths, the object
    store as the bounded ``count-objects -v`` summary, the reflog and the three
    metadata readings as deterministic digests of the paths and contents
    involved — names, sizes and digests, never the object contents, the reflog
    text, or a metadata file's text.
    """

    refs: tuple[tuple[str, str], ...]
    config: str
    worktrees: tuple[str, ...]
    #: The bounded object-store summary ``git count-objects -v`` reports.
    #: Cheap, never hashes pack contents, and never grows with repository
    #: size. ``None`` when the summary could not be read, which fails the
    #: comparison closed rather than reporting no movement.
    objects: "str | None"
    #: A deterministic digest of every reflog file in the workspace's own
    #: ``.git/logs`` and in every linked worktree's ``.git/logs``. A reflog
    #: expire that empties a file is detected (its digest moves) without
    #: hashing the historical pack itself. ``None`` when the walk cannot
    #: reach a reflog file, which fails the comparison closed.
    reflog: "str | None"
    #: A deterministic digest of the common gitdir's ``info`` directory —
    #: ``exclude``, ``attributes``, ``refs`` and whatever else an owner or a
    #: worker put there. Contents are digested, never recorded, and this
    #: tool's own exclude lines are normalized away first, because the tool
    #: writes them itself after the baseline. A walk that cannot be completed
    #: refuses the whole reading rather than yielding ``None``.
    info: "str | None" = None
    #: A deterministic digest of the pseudorefs and in-progress operation
    #: state — ``FETCH_HEAD``, ``ORIG_HEAD``, ``MERGE_HEAD``, ``CHERRY_PICK_HEAD``,
    #: ``REVERT_HEAD``, ``BISECT_LOG``, and the operation directories
    #: ``sequencer``, ``rebase-merge`` and ``rebase-apply`` — in the
    #: workspace's own gitdir. Size and mtime are folded in as well as
    #: content, because a repeat ``git fetch`` rewrites ``FETCH_HEAD`` with
    #: byte-identical content. A walk that cannot be completed refuses the
    #: whole reading rather than yielding ``None``.
    pseudorefs: "str | None" = None
    #: A deterministic digest of the object metadata that the object *count*
    #: does not move — the ``objects/info`` directory (``commit-graph``,
    #: ``commit-graphs/``, ``packs``, ``alternates``) and the pack directory's
    #: ``multi-pack-index*`` files. A walk that cannot be completed refuses the
    #: whole reading rather than yielding ``None``.
    object_metadata: "str | None" = None

    def as_dict(self) -> dict:
        return {
            "refs": [list(item) for item in self.refs],
            "config": self.config,
            "worktrees": list(self.worktrees),
            "objects": self.objects,
            "reflog": self.reflog,
            "info": self.info,
            "pseudorefs": self.pseudorefs,
            "object_metadata": self.object_metadata,
        }

    def moved_refs(self, other: "WorkspaceGitState") -> tuple[str, ...]:
        """Names of refs whose target moved, or that appeared or disappeared.

        Named rather than counted, because a refusal has to say *which* ref the
        worker moved: "a ref moved" is not actionable for an owner deciding
        what to undo. Sorted, so two readings of the same state agree.
        """

        before = dict(self.refs)
        after = dict(other.refs)
        return tuple(
            sorted(
                name
                for name in set(before) | set(after)
                if before.get(name) != after.get(name)
            )
        )

    def moved_by(self, other: "WorkspaceGitState") -> tuple[str, ...]:
        """The component names this state moved, in a fixed order.

        Component names rather than paths, because these are not paths: the
        audit says *which part* of the workspace's git state moved, and the
        ref names and worktree paths beside it say where.
        """

        return tuple(
            name
            for name, changed in (
                ("refs", self.refs != other.refs),
                ("config", self.config != other.config),
                ("worktrees", self.worktrees != other.worktrees),
                ("objects", self.objects != other.objects),
                ("reflog", self.reflog != other.reflog),
                ("info", self.info != other.info),
                ("pseudorefs", self.pseudorefs != other.pseudorefs),
                ("object_metadata", self.object_metadata != other.object_metadata),
            )
            if changed
        )

    def moved_worktrees(self, other: "WorkspaceGitState") -> tuple[str, ...]:
        """Paths of worktree registrations that appeared or disappeared."""

        before, after = set(self.worktrees), set(other.worktrees)
        return tuple(sorted(before ^ after))


def _git_state(
    workspace: Path,
    runner: Runner,
    *,
    tool_appended_exclude_lines: "tuple[bytes, ...]" = (),
) -> WorkspaceGitState:
    """Read the eight git-state readings above, once each.

    Each is a single git call over the workspace's own repository — or, for the
    three readings that walk ``.git`` paths themselves, one bounded walk — so
    the cost does not grow with the number of dirty paths, and each refuses
    rather than contributing nothing: a state that could not be read is not an
    unchanged one, and reporting it as unchanged is exactly the false negative
    this record exists to close. The object-store, object-metadata, pseudoref
    and reflog readings are bounded by design — ``git count-objects -v`` is one
    cheap summary, and the walks reach only the ``info`` directory, the named
    pseudorefs and operation directories, the multi-pack-index family, and the
    small reflog text files — so the baseline cost does not grow with the size
    of the pack history.

    ``tool_appended_exclude_lines`` is the set of exclude line bytes this
    specific launch wrote; only those are normalized out of the info-state
    digest, so an owner line that happens to match a tool pattern stays in
    the comparison.
    """

    refs_result = runner(
        [
            "git",
            "-C",
            str(workspace),
            "for-each-ref",
            "--format=%(refname)%00%(objectname)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if refs_result.returncode:
        raise WorktreeError(
            (refs_result.stderr or refs_result.stdout).strip()
            or "git for-each-ref failed"
        )
    refs: list[tuple[str, str]] = []
    for line in (refs_result.stdout or "").splitlines():
        name, separator, oid = line.partition("\0")
        if separator and name:
            refs.append((name, oid))
    # The local config as a digest, from two readings of the same files:
    # `git config --local --list` is what a `git config` write moves, and the
    # raw bytes below are what any other edit to those files moves. `--local`
    # is the workspace's own repository config; the global and system layers
    # are outside this workspace and are not this run's to report on.
    config_result = runner(
        ["git", "-C", str(workspace), "config", "--local", "--list", "--null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if config_result.returncode:
        raise WorktreeError(
            (config_result.stderr or config_result.stdout).strip()
            or "git config --local --list failed"
        )
    registered = runner(
        ["git", "-C", str(workspace), "worktree", "list", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if registered.returncode:
        raise WorktreeError(
            (registered.stderr or registered.stdout).strip()
            or "git worktree list failed"
        )
    workspaces = tuple(
        sorted(
            line[len("worktree ") :]
            for line in (registered.stdout or "").splitlines()
            if line.startswith("worktree ")
        )
    )
    # Bounded object-store summary. ``git count-objects -v`` is the cheapest
    # way to ask git "did the loose/pack count change?" — it reports one row
    # per counter and never hashes a single byte of the pack itself, so the
    # comparison stays a function of git's own bookkeeping, not of the size
    # of the repository's history. An object the worker dropped through
    # ``git hash-object -w`` or ``git commit-tree`` moves this counter while
    # moving no path, no ref, and no HEAD.
    objects_result = runner(
        ["git", "-C", str(workspace), "count-objects", "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if objects_result.returncode:
        raise WorktreeError(
            (objects_result.stderr or objects_result.stdout).strip()
            or "git count-objects -v failed"
        )
    objects_digest = hashlib.sha256(
        (objects_result.stdout or "").encode("utf-8")
    ).hexdigest()
    # Reflog digest. Walks every gitdir that belongs to this repository: the
    # common ``.git/logs`` and each per-worktree gitdir's own ``logs``.
    # The file contents are reflog text — short lines, never the historical
    # pack — so the comparison is bounded by the size of the reflog itself
    # and not by the size of the repository. An expire that empties a file
    # is detected because the digest changes; a delete that removes the
    # file is detected because the walk's file list changes.
    reflog_digest = _reflog_digest(workspace, runner)
    # The three readings that close what the first five cannot see: git's own
    # `info` metadata, the pseudorefs and operation state of the workspace's
    # gitdir, and the object metadata the object counters do not move. All
    # three are digests of paths this reading walks itself, so what is
    # published is a comparison and never a file's text.
    own_gitdir, common_gitdir = _resolve_gitdirs(workspace, runner)
    return WorkspaceGitState(
        refs=tuple(sorted(refs)),
        config=_config_state_digest(
            config_result.stdout or "", own_gitdir, common_gitdir
        ),
        worktrees=workspaces,
        objects=objects_digest,
        reflog=reflog_digest,
        info=_info_state_digest(common_gitdir, tool_appended_exclude_lines),
        pseudorefs=_pseudoref_state_digest(own_gitdir),
        object_metadata=_object_metadata_digest(common_gitdir),
    )


def _reflog_digest(workspace: Path, runner: Runner) -> str:
    """A deterministic digest of every reflog file in the workspace's repository.

    The common gitdir is the workspace's own ``.git/logs`` for a primary
    checkout, and a per-worktree gitdir's ``logs`` for a linked one. The
    workspace's own linked worktrees add their own ``logs`` directories
    inside the common gitdir's ``worktrees/<name>/logs`` and each must be
    walked too — ``git reflog expire`` on a linked worktree touches only
    that worktree's gitdir, which a digest of the common dir alone would miss.

    Returns a hex sha256 of the file paths and contents in a fixed order, so
    a worker that edits a single byte in any reflog file moves the digest.
    Refuses rather than returning ``None`` when a directory exists but cannot
    be listed: the comparison would otherwise report "no change" for a state
    nothing was read from.
    """

    common_raw = _git(workspace, ["rev-parse", "--git-common-dir"], runner)
    common = _absolute_git_path(workspace, common_raw)
    hasher = hashlib.sha256()
    for logs_root in _reflog_roots(common):
        for current, dirs, files in _walk_reflog(logs_root):
            dirs.sort()
            for filename in sorted(files):
                path = Path(current) / filename
                rel = path.relative_to(common).as_posix()
                hasher.update(rel.encode("utf-8"))
                hasher.update(b"\0")
                try:
                    with open(path, "rb") as handle:
                        while True:
                            chunk = handle.read(64 * 1024)
                            if not chunk:
                                break
                            hasher.update(chunk)
                except OSError as exc:
                    raise WorktreeError(
                        f"reflog file {path} could not be read: {exc}"
                    ) from exc
    return hasher.hexdigest()


def _walk_reflog(root: Path):
    """``os.walk`` over a reflog directory, with directory-access errors raised.

    The default ``os.walk`` silently swallows ``os.listdir`` errors and
    refuses the rest of the tree; the audit's whole point is to refuse rather
    than pretend nothing was there, so the failure surfaces here.
    """

    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        raise WorktreeError(
            f"reflog directory {root} could not be listed: {exc}"
        ) from exc
    dirs: list[str] = []
    files: list[str] = []
    for entry in entries:
        full = root / entry
        kind = _gitdir_path_kind(full)
        if kind == "directory":
            dirs.append(entry)
        elif kind == "file":
            files.append(entry)
    yield (str(root), dirs, files)
    for sub in dirs:
        yield from _walk_reflog(root / sub)


def _reflog_roots(common: Path) -> tuple[Path, ...]:
    """The ``logs`` directory of every gitdir under ``common``.

    The common gitdir's own ``logs`` is included, and so is each
    per-worktree gitdir's ``logs`` under ``common/worktrees/<name>/logs``.
    The worktree names git exposes through ``git worktree list`` are not
    used here — the workspace's own linked worktrees are exactly the
    gitdirs the digests must cover, and ``os.listdir(common / "worktrees")``
    reads them directly.

    Returns the ``logs`` paths themselves, not the gitdirs: ``git
    reflog expire`` rewrites text inside ``logs/HEAD`` and
    ``logs/refs/heads/<name>``, not the loose-object store or the index
    that share the common gitdir, and a digest that walked the whole
    gitdir would move on every object access and never stop moving.
    """

    roots: list[Path] = []
    common_logs = common / "logs"
    if _gitdir_path_kind(common_logs) == "directory":
        roots.append(common_logs)
    worktrees_dir = common / "worktrees"
    if _gitdir_path_kind(worktrees_dir) == "directory":
        try:
            entries = sorted(os.listdir(worktrees_dir))
        except OSError as exc:
            raise WorktreeError(
                f"reflog worktree directory {worktrees_dir} could not be listed: {exc}"
            ) from exc
        for entry in entries:
            candidate = worktrees_dir / entry
            if _gitdir_path_kind(candidate) != "directory":
                continue
            candidate_logs = candidate / "logs"
            if _gitdir_path_kind(candidate_logs) == "directory":
                roots.append(candidate_logs)
    return tuple(roots)


def _gitdir_path_kind(path: Path) -> str:
    """Classify Git audit paths without following links outside the gitdir."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise WorktreeError(f"Git path {path} could not be inspected: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise WorktreeError(f"Git path {path} is a symlink")
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    raise WorktreeError(f"Git path {path} is not a regular file or directory")


#: The pseudorefs and in-progress operation state of a gitdir. Every one of
#: these is a file or directory git writes to *record what it is doing*, in the
#: workspace's own gitdir — which is what makes them invisible to every other
#: reading here. ``git fetch`` is the case that proves it: run twice with
#: nothing to bring over, it moves no ref, writes no object, touches no reflog
#: and changes no path — it rewrites ``FETCH_HEAD`` and nothing else. A
#: stopped ``git rebase``, ``git cherry-pick`` or ``git merge`` leaves the rest
#: of its state under the operation directories, so the owner's checkout is
#: mid-operation even though ``git status`` reports it as they left it.
_PSEUDOREF_NAMES = (
    "FETCH_HEAD",
    "ORIG_HEAD",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "AUTO_MERGE",
)
_PSEUDOREF_DIRS = ("sequencer", "rebase-merge", "rebase-apply")
#: The pack-directory prefix whose files are object *metadata*, not object
#: data: ``multi-pack-index``, its ``.rev`` reverse index and its ``.bitmap``
#: companion. Rewriting any of them leaves ``git count-objects -v`` — and
#: therefore the object-store reading — exactly as it was.
_OBJECT_METADATA_PREFIX = "multi-pack-index"


def _resolve_gitdirs(workspace: Path, runner: Runner) -> "tuple[Path, Path]":
    """The workspace's own gitdir and the repository's common gitdir.

    They differ for a linked worktree, where the per-worktree state — the
    pseudorefs and the operation directories — lives in the worktree's own
    gitdir while the shared state lives in the common one.
    """

    own = _absolute_git_path(
        workspace, _git(workspace, ["rev-parse", "--git-dir"], runner)
    )
    common = _absolute_git_path(
        workspace, _git(workspace, ["rev-parse", "--git-common-dir"], runner)
    )
    return own, common


def _local_config_paths(
    own: Path, common: Path
) -> "tuple[tuple[str, Path], ...]":
    """Every local config file that governs this workspace, with a label.

    The repository's own ``config`` lives in the common gitdir, and it is the
    file a ``git config --local`` write rewrites. A per-worktree
    ``config.worktree`` is read on top of it when the repository enables
    ``extensions.worktreeConfig``: from the common gitdir for the primary
    checkout, and from the worktree's *own* gitdir for a linked one — the same
    split :func:`_resolve_gitdirs` exists for. A linked worktree never has a
    ``config`` of its own (git reads none there), so this is exactly the set of
    files git itself would read for this workspace. For a primary checkout
    ``own`` and ``common`` are the same directory, and the label keeps the two
    readings apart rather than folding one path twice.
    """

    paths = [
        ("common/config", common / "config"),
        ("common/config.worktree", common / "config.worktree"),
    ]
    if own != common:
        paths.append(("worktree/config.worktree", own / "config.worktree"))
    return tuple(paths)


def _config_state_digest(semantic: str, own: Path, common: Path) -> str:
    """The local config as git parses it *and* as the files stand on disk.

    The parsed view alone is not the config: a comment, a blank line or a
    reformatting changes the file git will parse and changes no setting in the
    parse. In a mode that forbids every git write, a worker editing the owner's
    config file by hand would move nothing this record held. So every local
    config file that governs the workspace is digested by its raw bytes beside
    the parsed view, and either kind of edit moves the one component.

    Only digests are kept — never the files' text — because a local config
    legitimately holds a remote URL with an embedded credential and this record
    is published. A file that is not there folds as absent rather than as an
    error, so a repository without ``config.worktree`` reads stably; a file
    that is there and cannot be read refuses the whole reading, because a state
    that was not read is not an unchanged one.
    """

    hasher = hashlib.sha256()
    hasher.update(b"parsed\0")
    hasher.update(semantic.encode("utf-8"))
    for label, path in _local_config_paths(own, common):
        _fold_gitdir_tree(hasher, f"raw:{label}", path, with_stat=False)
    return hasher.hexdigest()


def _fold_gitdir_tree(
    hasher,
    label: str,
    path: Path,
    *,
    with_stat: bool,
    normalize: "Callable[[str, bytes], bytes] | None" = None,
) -> None:
    """Fold one gitdir path into ``hasher``, recursively and deterministically.

    Directories are folded as their sorted contents, so a file added, removed
    or renamed inside one moves the digest without a second reading. A file's
    bytes are read in chunks and never returned, so a metadata file's text
    cannot leave through the audit. ``with_stat`` additionally folds size and
    ``mtime_ns`` — the pseudoref case, where git rewrites a file with content
    identical to what was already there and only the timestamp moves — and a
    missing path folds as absent rather than as an error, so a gitdir that
    holds none of these files still digests stably. Anything that cannot be
    listed or read raises: a state that was not read is not an unchanged one.
    """

    kind = _gitdir_path_kind(path)
    hasher.update(label.encode("utf-8"))
    hasher.update(b"\0")
    if kind == "missing":
        hasher.update(b"absent\0")
        return
    if kind == "directory":
        hasher.update(b"directory\0")
        try:
            names = sorted(os.listdir(path))
        except OSError as exc:
            raise WorktreeError(
                f"Git metadata directory {path} could not be listed: {exc}"
            ) from exc
        for name in names:
            _fold_gitdir_tree(
                hasher,
                f"{label}{name}/",
                path / name,
                with_stat=with_stat,
                normalize=normalize,
            )
        return
    hasher.update(b"file\0")
    try:
        info = path.lstat()
        if with_stat:
            hasher.update(f"{info.st_size}:{info.st_mtime_ns}".encode("ascii"))
            hasher.update(b"\0")
        if normalize is None:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
        else:
            # A child's label carries the trailing slash its parent could not
            # know was a directory; the normalizer is handed the path it names.
            hasher.update(normalize(label.rstrip("/"), path.read_bytes()))
    except OSError as exc:
        raise WorktreeError(
            f"Git metadata file {path} could not be read: {exc}"
        ) from exc


def _normalize_info_file(
    label: str, data: bytes, appended: "tuple[bytes, ...]" = ()
) -> bytes:
    """One ``.git/info`` file as the owner's own content, launch lines removed.

    The one file this tool writes into is ``exclude``, where the scratch and
    Devin entries are its own launch-time writes and must not read as the
    worker's. ``appended`` is the set of line bytes *this specific* launch
    added — so a launch that wrote nothing normalizes nothing, a launch that
    wrote ``/.side-lane-scratch/\\n`` strips that exact line, and an owner
    line that happens to match a tool pattern stays in the digest (the
    finding's regression). Nothing else under ``info`` is touched: an
    ``attributes`` edit, or any other file an owner or a worker puts there,
    differs byte for byte from the baseline and moves the digest.
    """

    if label == "info/exclude":
        return _without_appended_exclude_lines(data, appended)
    return data


def _info_state_digest(
    common: Path, tool_appended_exclude_lines: "tuple[bytes, ...]" = ()
) -> str:
    """A deterministic digest of the common gitdir's ``info`` directory.

    ``.git/info`` holds git's own repository-wide metadata: ``exclude`` (which
    ``ensure_scratch_exclusion`` and ``ensure_devin_local_mcp_exclusion``
    append to), ``attributes`` (which changes what ``git diff`` and a
    checkout's filters do to every path), ``refs`` (which changes how refs are
    packed), and ``alternates``. None of them is a path, a ref, the config or a
    reflog entry, and none of them appears in ``git status`` — so an edit there
    was invisible to every reading the baseline had. Only digests are recorded;
    no file's text is ever published.

    ``tool_appended_exclude_lines`` is the set of exclude bytes *this* launch
    added: the digest is computed against the owner's content, with only those
    bytes stripped, so a baseline taken after the tool wrote lines and an
    after-run reading taken under the same launch compare as the owner would
    compare them.
    """

    hasher = hashlib.sha256()
    _fold_gitdir_tree(
        hasher,
        "info/",
        common / "info",
        with_stat=False,
        normalize=lambda label, data: _normalize_info_file(
            label, data, tool_appended_exclude_lines
        ),
    )
    return hasher.hexdigest()


def _pseudoref_state_digest(own: Path) -> str:
    """A deterministic digest of the workspace gitdir's pseudorefs and state.

    Content alone would miss the case this reading exists for: a repeat ``git
    fetch`` rewrites ``FETCH_HEAD`` with byte-identical text, so only the
    size-and-mtime fold below tells the second fetch from the first. That fold
    is deliberately confined to this component — folding timestamps into the
    ref, config, object or reflog readings would report every ordinary read as
    a write — and it never invents a movement the baseline cannot see: the
    reading is taken with the baseline and compared with it, so a timestamp
    that moved between the two was moved by something that ran in between.
    """

    hasher = hashlib.sha256()
    for name in _PSEUDOREF_NAMES:
        _fold_gitdir_tree(hasher, name, own / name, with_stat=True)
    for name in _PSEUDOREF_DIRS:
        _fold_gitdir_tree(hasher, f"{name}/", own / name, with_stat=True)
    return hasher.hexdigest()


def _object_metadata_digest(common: Path) -> str:
    """A deterministic digest of the object metadata the counters do not move.

    ``git count-objects -v`` counts objects, so ``git commit-graph write`` and
    ``git multi-pack-index write`` — both of which rewrite how objects are
    *found* while leaving the set of them and the loose/pack counters exactly
    as they were — are invisible to it. This reading covers the whole
    ``objects/info`` directory (``commit-graph``, ``commit-graphs/``,
    ``packs``, ``alternates``, and anything else there) plus the pack
    directory's ``multi-pack-index`` family. Pack files themselves are still
    not hashed: the counters cover those, and hashing a history would make the
    baseline cost grow with the repository.
    """

    hasher = hashlib.sha256()
    objects = common / "objects"
    _fold_gitdir_tree(hasher, "objects/info/", objects / "info", with_stat=True)
    pack = objects / "pack"
    try:
        names = [
            name
            for name in sorted(os.listdir(pack))
            if name.startswith(_OBJECT_METADATA_PREFIX)
        ]
    except FileNotFoundError:
        names = []
    except OSError as exc:
        raise WorktreeError(
            f"object pack directory {pack} could not be listed: {exc}"
        ) from exc
    for name in names:
        _fold_gitdir_tree(
            hasher, f"objects/pack/{name}", pack / name, with_stat=True
        )
    return hasher.hexdigest()


def _index_flags(workspace: Path, runner: Runner) -> "dict[str, str]":
    """Every tracked path whose index entry carries a non-default tag.

    ``git ls-files -v`` prints one tag per tracked path — ``H`` for an ordinary
    cached entry, lowercase for one marked assume-unchanged, ``S`` for
    skip-worktree, and the other tags git documents — so asking for the whole
    index once is what makes this *complete* rather than a guess about which
    paths a worker might have hidden. Only the non-``H`` tags are kept: every
    other tracked path's default tag says nothing a content-and-index entry
    does not already say.

    These flags are exactly why a path-only or status-only baseline has a hole
    in it: a tracked file marked assume-unchanged stops being reported by
    ``git status`` at all, so a worker's edit to it is invisible to every
    reading taken from ``git status`` — the edit is not "clean", it is
    unmeasured. The caller hashes the content of the paths named here, which is
    the only reading that sees such an edit.
    """

    result = runner(
        ["git", "-C", str(workspace), "ls-files", "-v", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise WorktreeError(
            (result.stderr or result.stdout).strip() or "git ls-files -v failed"
        )
    flags: dict[str, str] = {}
    for record in (result.stdout or "").split("\0"):
        if len(record) < 3 or record[1] != " ":
            continue
        tag, path = record[0], record[2:]
        if tag != "H":
            flags[path] = tag
    return flags


#: The status a path carries when the *only* reason it is in a baseline is an
#: index flag. Git itself reports nothing for such a path — that is the whole
#: point of assume-unchanged — so the two columns a clean path would print are
#: what its own status reads as, and the `flag` field is what says why it is
#: here at all.
_FLAGGED_STATUS = "  "


@dataclass(frozen=True)
class WorkspaceBaseline:
    """The content-and-index identity of an existing workspace's dirty set.

    ``entries`` covers every path git reports as carrying uncommitted work —
    tracked modifications and additions, staged changes, and every file inside
    an untracked directory — each with the status git gave it, a digest of its
    working-tree content, that path's own mode, its whole index record: blob
    oid, mode, and any conflict stages, plus that entry's index tag when it is
    not the ordinary cached one. ``branch`` and ``head`` record what the
    workspace stood on, both of which can move, and ``git_state`` records the
    parts of git's state that are not paths at all — refs, the local config,
    and the worktree registrations. ``sha256`` is the digest of the whole
    record, so two runs' baselines can be compared, and retained, without
    retaining content.

    The entries are the dirty set *plus* every tracked path whose index tag is
    not the default: a path marked assume-unchanged is one ``git status`` no
    longer reports, so leaving it out made a worker's edit to it — and a
    worker's own flagging of it — compare as *no change*.
    """

    workspace: Path
    branch: str
    head: str
    entries: tuple[WorkspaceEntry, ...]
    git_state: WorkspaceGitState
    sha256: str
    #: The exact ``info/exclude`` line bytes this launch appended, in the
    #: order the writers produced them. The info-state digest is computed
    #: against the owner's content with only these bytes stripped, so an
    #: owner line that merely matches a tool pattern is preserved and a
    #: worker edit to it is reported; a launch that wrote nothing has
    #: ``()`` and normalizes nothing. Not folded into :attr:`sha256` —
    #: the digest is a content identity, and these bytes are a description
    #: of how to read it.
    tool_appended_exclude_lines: tuple[bytes, ...] = ()

    def unverified_paths(self) -> tuple[str, ...]:
        """Paths this baseline could not measure, so could not compare.

        A path recorded here is not "unchanged" and not "changed": nothing
        about it was established, and a comparison that reads two unmeasured
        states as equal would be reporting a verdict no measurement supports.

        Both unmeasured kinds are here. A path of a kind this walk never opens
        — a dirty submodule's directory, an untracked nested repository, a FIFO
        — is exactly as unmeasured as one whose read refused: the walk records
        a sentinel for it, and the sentinel is not evidence. Selecting only the
        unreadable one made the unsupported kind compare equal to itself, so a
        worker's real edit inside an already-dirty submodule was reported as
        *no change* — a false statement about the owner's workspace.
        """

        return tuple(
            entry.path for entry in self.entries if entry_is_unmeasured(entry)
        )


def _workspace_digest(
    branch: str,
    head: str,
    entries: Sequence[WorkspaceEntry],
    git_state: "WorkspaceGitState",
) -> str:
    payload = json.dumps(
        {
            "branch": branch,
            "head": head,
            "entries": [entry.as_dict() for entry in entries],
            "git_state": git_state.as_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_workspace_baseline(
    workspace: Path,
    runner: Runner = subprocess.run,
    *,
    tool_appended_exclude_lines: "tuple[bytes, ...]" = (),
) -> WorkspaceBaseline:
    """Read an existing workspace's whole dirty state, before a worker starts.

    This is the baseline an existing-workspace lane is judged against, so it is
    deliberately a content-and-index identity rather than the path list
    :func:`snapshot_source` takes: a workspace handed to a lane is expected to
    arrive dirty, and a path-only baseline would report a worker's rewrite of
    an already-dirty file as "unchanged". Neither digest is content, so the
    record can be published in the run audit as it stands.

    Three readings beyond the dirty set are what make it a *complete* baseline
    rather than a complete-looking one. The index tags name every tracked path
    git has been told to stop reporting, which are added to the entries with
    their content hashed — the only way an edit under an assume-unchanged flag
    becomes visible at all. The ignored paths are added the same way, because
    an ignored file is invisible to ``git status`` for a different reason and
    has the same consequence: an edit to a pre-existing ignored ``.env`` would
    otherwise ride beside a legitimate visible delta and be reported as
    unchanged, and a secret-bearing file is exactly what ignores are used for.
    That reading is bounded and fails closed before the worker exists when the
    ignored set is larger than this run can measure. The git state reads refs,
    the local config, the worktree registrations, git's own ``info`` metadata,
    the pseudorefs and operation state, and the object metadata, none of which
    appear in ``git status`` and all of which a worker told to make no git write
    of any kind could otherwise move without this record noticing.

    ``tool_appended_exclude_lines`` is the set of ``info/exclude`` line bytes
    *this* launch wrote: only those are normalized out of the info-state
    digest, so an owner line that merely matches a tool pattern is preserved
    and a worker edit to that owner line is reported.
    """

    repo = workspace.resolve()
    branch = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], runner)
    head = _git(repo, ["rev-parse", "HEAD"], runner)
    paths = changed_paths(repo, runner, untracked_all=True)
    flags = _index_flags(repo, runner)
    records = _index_records(repo, [entry.path for entry in paths], runner)
    entries = []
    for entry in paths:
        content, mode = _worktree_identity(repo / entry.path)
        staged = records.get(entry.path)
        entries.append(
            WorkspaceEntry(
                path=entry.path,
                status=entry.status,
                content=content,
                index=None if staged is None else staged.oid,
                mode=mode,
                index_mode=None if staged is None else staged.mode,
                index_stages=() if staged is None else staged.stages,
                flag=flags.get(entry.path),
            )
        )
    # Every flagged path git status no longer reports, measured the same way as
    # one it does. Its status is what git says about it, which for a hidden
    # path is nothing — the `flag` field is what accounts for its presence.
    dirty = {entry.path for entry in paths}
    for path in sorted(set(flags) - dirty):
        content, mode = _worktree_identity(repo / path)
        staged = records.get(path)
        if staged is None:
            # The flag is on a path with no index entry in the paths we asked
            # for, so read its record directly rather than recording an entry
            # the comparison could not use.
            staged = _index_records(repo, [path], runner).get(path)
        entries.append(
            WorkspaceEntry(
                path=path,
                status=_FLAGGED_STATUS,
                content=content,
                index=None if staged is None else staged.oid,
                mode=mode,
                index_mode=None if staged is None else staged.mode,
                index_stages=() if staged is None else staged.stages,
                flag=flags[path],
            )
        )
    entries = tuple(entries)
    # The pre-existing ignored files, measured the same way, appended after the
    # paths the two readings above already cover. This is the reading that has
    # to happen before the worker exists: it is what makes an ignored file's
    # movement a *delta with a before* — a change from a state that was read —
    # rather than a change that appears from nowhere, and the bound it checks
    # refuses the launch outright when the ignored tree is too large to measure
    # rather than baselining a subset of it.
    entries = entries + tuple(
        _ignored_baseline_entries(
            repo,
            _workspace_ignored_paths(repo, runner),
            {entry.path for entry in entries},
        )
    )
    git_state = _git_state(
        repo, runner, tool_appended_exclude_lines=tool_appended_exclude_lines
    )
    return WorkspaceBaseline(
        workspace=repo,
        branch=branch,
        head=head,
        entries=entries,
        git_state=git_state,
        sha256=_workspace_digest(branch, head, entries, git_state),
        tool_appended_exclude_lines=tuple(tool_appended_exclude_lines),
    )


@dataclass(frozen=True)
class WorkspaceDelta:
    """One path a worker changed in an existing workspace, and how.

    ``changes`` names every difference found, in a fixed order, because a
    single path can move in more than one way at once — a file can be rewritten
    *and* restaged — and reporting only the first difference found would hide
    the rest.
    """

    path: str
    changes: tuple[str, ...]
    before: "WorkspaceEntry | None"
    after: "WorkspaceEntry | None"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "changes": list(self.changes),
            "before": None if self.before is None else self.before.as_dict(),
            "after": None if self.after is None else self.after.as_dict(),
        }


def workspace_deltas(
    baseline: WorkspaceBaseline, after: WorkspaceBaseline
) -> tuple[WorkspaceDelta, ...]:
    """What a worker changed relative to a baseline, existing dirt excluded.

    A path the baseline already carried and whose content, mode, index record,
    index tag and status are all unchanged is not a delta, however dirty it is:
    the whole point of capturing them all is that preexisting work is neither
    claimed nor blamed. A path that changed in place, one that appeared, and
    one a worker removed are each a delta.

    A path whose content could not be measured on either side — the baseline's
    or the one taken after — is a delta too, reported as ``unverified``,
    because two failed measurements are not evidence that nothing changed. That
    is the one case here that is a statement about the measurement rather than
    about the workspace, and it never reads as "unchanged".

    "Could not be measured" covers both unmeasured kinds, not only the
    unreadable one: a path of a kind this walk never opens — a dirty submodule,
    an untracked nested repository — has no content on either side, only the
    same sentinel twice, and ``sentinel != sentinel`` is false. Comparing them
    as content made a worker's real edit inside such a path report *no change*.
    A path is therefore only compared field by field when both sides are real
    measurements of it.

    The change kinds are ``unverified``, ``added``, ``removed``, ``content``,
    ``mode``, ``index``, ``status`` and ``flag``. The last is the one git can
    hide: a tracked path marked assume-unchanged stops being reported by
    ``git status`` entirely, so a content comparison would have nothing to
    compare — the baseline carries such paths on purpose, and a movement in the
    tag itself is the index write that set or cleared it. A path the baseline
    carries because it was already *ignored* is compared by the same rules: its
    ``status`` is the ``!!`` marker, its content and mode are real
    measurements, and it has no index record to move — so an edit to a
    pre-existing ignored file is a ``content`` delta exactly as an edit to a
    tracked one is, beside whatever the worker did where git can see it.
    """

    before = {entry.path: entry for entry in baseline.entries}
    current = {entry.path: entry for entry in after.entries}
    deltas: list[WorkspaceDelta] = []
    for path in sorted(set(before) | set(current)):
        previous, found = before.get(path), current.get(path)
        changes: list[str] = []
        if entry_is_unmeasured(previous) or entry_is_unmeasured(found):
            changes.append("unverified")
        if previous is None:
            changes.append("added")
            # A path that appeared in the comparison carrying an index tag is
            # one whose entry is flagged, and flagging a tracked path is an
            # index write (`git update-index --assume-unchanged`). Saying only
            # "added" would report that write as if the worker had produced a
            # file: it is named here so the index half of the record sees it.
            if found is not None and found.flag is not None:
                changes.append("flag")
            # The same reading for the other index write a new path can carry.
            # A stage-0 entry that DIFFERS from HEAD is one the index did not
            # have before: the baseline holds every path git reported as
            # carrying uncommitted work, so a path it never carried had an
            # index matching HEAD — or no entry at all. `git add newfile` wrote
            # one, and reading only the *moved* identity missed it, because an
            # entry that never existed cannot move: the in-place comparison
            # below never runs for this path, so it reported "added", the index
            # half of the record named nothing, and a delivery was accepted for
            # work the owner never staged. Git's own index column is what
            # separates that write from the ordinary case — a clean tracked
            # path a worker rewrote carries the entry HEAD gave it, and its
            # index column is unchanged (`" "`).
            if (
                found is not None
                and found.index is not None
                and found.status[:1] not in (" ", "?")
            ):
                changes.append("index")
        elif found is None:
            # A path the baseline carried has no entry in the after baseline.
            # That covers three genuinely different worker writes, plus a
            # state that is no worker write at all:
            #
            #   - the baseline's index column carried a staged entry
            #     (``A``, ``M``, ``R``, ``C``, or ``D`` in the first
            #     position) and a worker reset it back to HEAD or removed it
            #     with ``git rm``. Either way the owner's index moved, and
            #     a delivery that reports only "removed" is one that lets a
            #     worker erase somebody's staged work without the audit
            #     naming the write. The reset case still leaves the file on
            #     disk at HEAD's content (so it isn't a real removal); the
            #     ``git rm`` case also takes the file off disk. The index
            #     mutation is reported in both, and the file loss is reported
            #     only when it actually happened;
            #
            #   - the baseline carried a worktree-only change (`` M`` or
            #     `` D``) and the path no longer exists, which is a real
            #     removal the worker made to a file the owner had already
            #     changed;
            #
            #   - the baseline carried only an untracked or ignored file
            #     (``??`` or ``!!``) and the file is gone. It was present
            #     when the worker was launched, so losing it is a removal
            #     even though no index entry changed.
            index_status = previous.status[:1] if previous.status else ""
            file_still_present = (baseline.workspace / path).exists()
            if index_status in ("A", "M", "R", "C", "D"):
                changes.append("index")
                if not file_still_present:
                    changes.append("removed")
            elif index_status in ("?", "!"):
                if not file_still_present:
                    changes.append("removed")
            elif not file_still_present:
                changes.append("removed")
            # The mirror case: a path that leaves the comparison having carried
            # a tag is one whose flag was cleared, which is the same write in
            # the other direction.
            if previous.flag is not None:
                changes.append("flag")
        elif "unverified" not in changes:
            if previous.content != found.content:
                changes.append("content")
            if previous.mode != found.mode:
                changes.append("mode")
            if (
                previous.index != found.index
                or previous.index_mode != found.index_mode
                or previous.index_stages != found.index_stages
            ):
                changes.append("index")
            if previous.status != found.status:
                changes.append("status")
            if previous.flag != found.flag:
                changes.append("flag")
        if changes:
            deltas.append(WorkspaceDelta(path, tuple(changes), previous, found))
    return tuple(deltas)


def resolve_existing_workspace(
    repo: Path, workspace: str | Path, runner: Runner = subprocess.run
) -> Path:
    """Resolve and validate the workspace ``--existing-workspace`` named.

    Refused here, before anything is claimed or read, because a lane pointed at
    the wrong tree is worse than a lane that did not start: a path inside a
    workspace, a checkout of another repository, or a detached HEAD all produce
    a run whose records and deltas would describe something other than what the
    worker actually wrote to.
    """

    repo = repo.resolve()
    top = Path(_git(repo, ["rev-parse", "--show-toplevel"], runner)).resolve()
    if top != repo:
        raise WorktreeError("execute mode requires the repository root")
    candidate = Path(workspace).expanduser()
    if not candidate.is_absolute():
        raise WorktreeError(
            "--existing-workspace must be an absolute path "
            "(the owner workspace to run in): "
            f"{workspace}"
        )
    candidate = Path(os.path.realpath(candidate))
    if not candidate.is_dir() or not (candidate / ".git").exists():
        raise WorktreeError(f"--existing-workspace is not a Git worktree: {candidate}")
    if Path(
        _git(candidate, ["rev-parse", "--show-toplevel"], runner)
    ).resolve() != candidate:
        raise WorktreeError(
            f"--existing-workspace must name the workspace root, not a path "
            f"inside it: {candidate}"
        )
    if _absolute_git_path(candidate, _git(candidate, ["rev-parse", "--git-common-dir"], runner)) != _absolute_git_path(
        repo, _git(repo, ["rev-parse", "--git-common-dir"], runner)
    ):
        raise WorktreeError(
            f"--existing-workspace {candidate} is not a worktree of {repo}: a "
            "lane's run records, lock, and git exclusions live in this "
            "repository's .git, so only a worktree that shares it can be used"
        )
    if _git(candidate, ["rev-parse", "--abbrev-ref", "HEAD"], runner) == "HEAD":
        raise WorktreeError(
            f"--existing-workspace {candidate} is on a detached HEAD: nothing "
            "records which branch the owner's work belongs to, so the lane "
            "could not report it"
        )
    return candidate


def adopt_existing_workspace(
    repo: Path,
    workspace: str | Path,
    lane_name: str,
    *,
    runner: Runner = subprocess.run,
    now: datetime | None = None,
    appended_exclude_lines: "list[bytes] | None" = None,
) -> WorktreeRun:
    """Point a lane at a pre-existing owner workspace instead of creating one.

    Nothing is created, checked out, or cleaned: the workspace is the owner's
    own checkout, exactly as it stood, and it is expected to be dirty. What is
    established here is only that it is a worktree *of this repository* — the
    run's records, lock, and git exclusions all live in this repository's
    ``.git``, and a checkout from elsewhere would leave a lane whose audit
    describes a tree no record could be found beside.

    The returned run carries the branch the workspace already stood on, never a
    created lane branch, and ``existing_workspace`` set so that no caller
    publishes, disposes, or commits on its behalf. ``appended_exclude_lines``
    is forwarded to :func:`prepare_scratch_directory` so the existing-workspace
    audit knows the exact bytes this launch wrote into ``info/exclude``, and
    can normalize them out without also normalizing an owner line that happens
    to match a tool pattern.
    """

    repo = repo.resolve()
    candidate = resolve_existing_workspace(repo, workspace, runner)
    prepare_scratch_directory(
        repo,
        candidate,
        runner=runner,
        appended_exclude_lines=appended_exclude_lines,
    )
    safe = safe_lane_name(lane_name)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    # The stamp is readable but second-granular, and only the readable half of
    # the key: a created lane cannot collide on it because `git worktree add -b`
    # refuses a branch that already exists, so a second lane of the same name
    # stops loudly. Nothing here has that backstop — the workspace is the
    # owner's, no branch is created, and the record is written with a plain
    # overwrite — so the run identifier is part of the key. Without it, a second
    # run in the same second would replace the first run's only record.
    run_id = os.urandom(2).hex()
    return WorktreeRun(
        repo,
        candidate,
        _git(candidate, ["rev-parse", "--abbrev-ref", "HEAD"], runner),
        safe,
        _git(candidate, ["rev-parse", "HEAD"], runner),
        existing_workspace=True,
        record_key=f"existing-{safe}-{stamp}-{run_id}",
    )


@dataclass(frozen=True)
class WorkspaceLock:
    """This run's claim on one existing workspace, held for the worker's life.

    The claim is a create-exclusive file, not a service: the sidecar records
    who holds it, on which host, at which pid, so a later run that finds it
    taken can name the holder instead of stalling. ``reclaimed`` is always
    False: this run takes over nothing, and a file it found — whatever the
    state of the process recorded in it — is never removed (see
    :func:`acquire_workspace_lock`).
    """

    path: Path
    workspace: Path
    token: str
    reclaimed: bool


def workspace_lock_path(
    repo: Path, workspace: Path, runner: Runner = subprocess.run
) -> Path:
    """The lock file one workspace resolves to, in the repository's own .git."""

    git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    resolved = Path(os.path.realpath(workspace))
    key = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:16]
    stem = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-")[:32]
    return git_dir / EXISTING_WORKSPACE_LOCK_DIR / f"{stem or 'workspace'}-{key}.lock"


def _read_workspace_lock(path: Path) -> Mapping[str, object]:
    """Read a lock file's holder record, or fail closed."""

    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorktreeError(
            f"existing-workspace lock {path} is unreadable; refusing to guess "
            f"whether its holder is still running: {exc}"
        ) from exc
    if not isinstance(holder, dict):
        raise WorktreeError(
            f"existing-workspace lock {path} is not a holder record; remove it "
            "by hand once you have confirmed no lane is writing there"
        )
    return holder


def _process_is_alive(pid: int) -> bool:
    """Whether a same-host pid is still running. Unknown answers stay alive."""

    if pid <= 1:
        # 0 and 1 are not a lane's pid, and 0/negative would address a process
        # group; neither is evidence the holder is gone.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _lock_holder_state(
    holder: Mapping[str, object], probe: Callable[[int], bool]
) -> str:
    """``"alive"``, ``"stale"``, or ``"unknown"`` for one holder record.

    A holder on another host, or one recorded without a usable pid, cannot be
    judged from here and answers ``"unknown"`` — which is refused, never
    reclaimed: taking a lock this process cannot prove is abandoned would be
    exactly the conflicting writer the lock exists to stop.
    """

    host = holder.get("host")
    pid = holder.get("pid")
    if not isinstance(host, str) or host != socket.gethostname():
        return "unknown"
    if type(pid) is not int:
        return "unknown"
    return "alive" if probe(pid) else "stale"


def _lock_refusal(path: Path, workspace: Path, holder: Mapping[str, object],
                  state: str) -> str:
    """Why the claim was refused, and what the operator can do about it.

    Every state refuses. A holder that is provably gone is not an exception:
    this run reads the file, and anything it did to the file afterwards would
    be acting on a claim that may already have been replaced, so the file is
    named for the operator to release deliberately instead.

    The one recovery that removes a file is offered only for the ``stale``
    state — a holder on this host whose process is provably gone. It is an
    operator act, taken once, after confirming that nothing is writing there,
    and it is the *only* thing in this mode that ever removes a live-looking
    lock file. Removing a lock whose holder is still running is not a recovery
    step and is not offered: it is what breaks the serialization this file
    exists to provide, and no participating run performs it (see
    :func:`release_workspace_lock`).
    """

    who = holder.get("lane_name") or "an unnamed lane"
    where = holder.get("branch") or "an unnamed branch"
    when = holder.get("acquired_at") or "an unrecorded time"
    if state == "alive":
        detail = (
            f"its holder is still running (pid {holder.get('pid')})"
        )
        recovery = (
            "Wait for the holder to finish and retry. Do not remove the file "
            "while its holder is running: that is what would let two workers "
            "write this workspace at once"
        )
    elif state == "stale":
        detail = (
            f"its holder is not running any more (pid {holder.get('pid')} on "
            "this host), though the file is left behind — and this run will not "
            "remove it, because a claim read a moment ago can already have been "
            "replaced by another run's live one"
        )
        recovery = (
            "Confirm the holder is really gone and nothing else is writing "
            f"there (for example `ps -p {holder.get('pid')}`), then remove "
            f"{path} by hand, once, and retry. Only remove it for a holder "
            "proved gone; a lock removed while its holder still runs is the "
            "one act that can put two workers in this workspace"
        )
    else:
        detail = (
            f"its holder was on another host ({holder.get('host')!r}) or left no "
            "usable pid, so this run cannot tell whether it is still writing"
        )
        recovery = "Wait for the holder to finish and retry"
    return (
        f"another lane already has the existing workspace {workspace} locked: "
        f"{path} was taken by {who} on {where} at {when}, and {detail}. Two "
        "workers in one workspace would interleave writes to the same files, so "
        f"this run is refused rather than queued. {recovery}."
    )


def acquire_workspace_lock(
    repo: Path,
    workspace: Path,
    *,
    lane_name: str,
    branch: str = "",
    now: datetime | None = None,
    probe: Callable[[int], bool] = _process_is_alive,
    runner: Runner = subprocess.run,
) -> WorkspaceLock:
    """Claim one existing workspace for this run, or refuse with the holder named.

    The claim is bounded and self-describing: it is one create-exclusive file
    under the repository's ``.git``, it names the holder, and it is released by
    the run that took it. Nothing else is ever reclaimed, and no lock file is
    ever removed here — not even one whose recorded process is provably gone.
    Reading a holder and removing it are two steps, and a competing reclaimer
    can replace the file between them; a run that unlinked what it had read
    would then delete its successor's live claim, and two workers would write
    one workspace. A holder that is gone is refused for the same reason, with
    the file named so the operator can confirm nothing is writing there and
    release it by hand. A lock this run cannot remove is a lock it cannot lose
    underneath a live holder.
    """

    path = workspace_lock_path(repo, workspace, runner)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(
            f"cannot create the existing-workspace lock directory {path.parent}: {exc}"
        ) from exc
    token = hashlib.sha256(os.urandom(32)).hexdigest()
    payload = json.dumps(
        {
            "schema_version": WORKSPACE_LOCK_SCHEMA_VERSION,
            "workspace": str(Path(os.path.realpath(workspace))),
            "lane_name": lane_name,
            "branch": branch,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": (now or datetime.now(timezone.utc)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "token": token,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        _publish_exclusive(path, payload)
    except FileExistsError:
        holder = _read_workspace_lock(path)
        state = _lock_holder_state(holder, probe)
        raise WorktreeError(_lock_refusal(path, workspace, holder, state)) from None
    except OSError as exc:
        raise WorktreeError(
            f"cannot take the existing-workspace lock {path}: {exc}"
        ) from exc
    return WorkspaceLock(path, Path(os.path.realpath(workspace)), token, False)


def release_workspace_lock(lock: WorkspaceLock) -> bool:
    """Release this run's claim. False when the file is no longer this run's.

    Never raises and never removes a file it cannot prove it wrote: a lock a
    successor already reclaimed must survive this call, or the successor's
    claim would be dropped underneath it.

    **The exact boundary of that guarantee.** The read above and the unlink
    below are two steps, and POSIX offers no compare-and-delete on a path, so
    they are not atomic. The residual window is only reachable by an operator
    act: for this call to remove a successor's *live* claim, the file must be
    removed and a new claim published between these two steps, and no
    participating run ever removes a lock it does not hold — every
    create-exclusive publish needs the path to be absent first, so a competing
    runner cannot replace the file while it is there (``acquire_workspace_lock``
    refuses instead, and removes nothing). Normal participating runs therefore
    cannot reproduce it, and the same-user authority this mode runs under
    means no runner-side lock would stop an operator who removes a live lock
    deliberately anyway. What the operator is told, and the only removal this
    mode ever invites, is :func:`_lock_refusal`'s ``stale`` recovery: one
    deliberate removal, for a holder proved gone, with nothing writing there.
    """

    try:
        holder = _read_workspace_lock(lock.path)
    except WorktreeError:
        return False
    if holder.get("token") != lock.token:
        return False
    try:
        lock.path.unlink()
    except OSError:
        return False
    return True


# Publication talks to a remote, so it is the one git call here that can block
# on the network or on a credential/SSH prompt. Bound it and refuse to prompt:
# an interactive prompt in an unattended lane is an indefinite hang.
PUBLISH_TIMEOUT_S = 120.0


def _noninteractive_env() -> "dict[str, str]":
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


def publish_lane_branch(
    run: WorktreeRun, *, remote: str = "origin", runner: Runner = subprocess.run
) -> str:
    """Push a delivered lane's branch to ``remote``; return the tracking ref.

    A delivered lane otherwise strands its commits on a local branch on one
    machine — measured 2026-09-17, 40 such commits across 36 worktrees were on
    no remote at all. Pushing is also what lets ``worktree_doctor`` later
    reclaim the worktree through its PRUNE path with its guards intact:
    deleting the tree here instead would destroy the evidence the coordinator
    still needs to inspect. ``--set-upstream`` so a follow-up push or pull
    from inside the worktree needs no arguments.
    """

    _refuse_existing_workspace(run, "publish")
    # lane_delivery() asks the worktree's HEAD whether anything was committed,
    # but what gets pushed is the named branch. A worker that switched branches
    # or detached HEAD and committed there satisfies delivery while this branch
    # is unchanged -- publishing it would then report published: true over a
    # commit that is still local, which is the exact lie this whole path exists
    # to prevent. It is not hypothetical: a lane has created its own branch and
    # committed to it before. Fail closed on any mismatch.
    on = _git(run.worktree, ["rev-parse", "--abbrev-ref", "HEAD"], runner)
    if on != run.branch:
        where = "a detached HEAD" if on == "HEAD" else f"branch {on}"
        raise WorktreeError(
            f"refusing to publish {run.branch}: the lane worktree is on {where}, "
            "so its commits are not on the branch that would be pushed"
        )

    try:
        _git(
            run.repository,
            ["push", "--set-upstream", remote, run.branch],
            runner,
            timeout=PUBLISH_TIMEOUT_S,
            env=_noninteractive_env(),
        )
    except WorktreeError as exc:
        # Re-wrap rather than let the bare git stderr speak for itself: the
        # operator needs the branch and remote named, not only git's complaint.
        raise WorktreeError(
            f"cannot publish lane branch {run.branch} to {remote}: {exc}"
        ) from exc
    return f"{remote}/{run.branch}"


#: A verification command is a whole test suite, not one git call, so it gets
#: a far longer leash than publication — but still a leash: a hung suite must
#: become a failed verification, never an indefinite stall of the runner.
VERIFY_TIMEOUT_S = 900.0

#: The tail is where test failures are; the head of a huge log is noise.
VERIFY_OUTPUT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of a caller-supplied verification command.

    A timeout or an unstartable command is recorded as a failure (exit codes
    124 and 127, the ``timeout(1)`` and shell conventions) rather than raised:
    the caller's contract is "judge this lane", and "the judge crashed" answers
    nothing.
    """

    command: str
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def _verify_output_tail(output: str) -> str:
    """Keep the last ``VERIFY_OUTPUT_TAIL_CHARS`` characters, labeled as cut.

    Truncation must be visible in the retained text itself: a silent cut
    reads as "this was all the output there was", which sends the operator
    hunting through a log we threw away.
    """

    if len(output) <= VERIFY_OUTPUT_TAIL_CHARS:
        return output
    return (
        f"[verify output truncated — kept the last {VERIFY_OUTPUT_TAIL_CHARS} "
        f"of {len(output)} characters]\n"
        + output[-VERIFY_OUTPUT_TAIL_CHARS:]
    )


def _bounded_verify_runner(
    command,
    *,
    cwd=None,
    shell=False,
    timeout=None,
    **_ignored,
):
    """Run a verification command without trusting it to be small or well-behaved.

    Three hazards, all of which would take down the run rather than the lane:

    * ``subprocess.run`` buffers the whole pipe before anyone truncates it, so a
      noisy or looping suite can exhaust memory for the entire timeout — and the
      summary, and the branch publication, never happen. Stream into a bounded
      tail instead; only the tail is ever kept anyway.
    * A command is free to emit bytes that are not valid text. Strict decoding
      would raise straight past both handlers in ``verify_lane``, so decode with
      replacement: mojibake in the evidence beats no result at all.
    * ``shell=True`` means the timeout kills one shell and orphans whatever it
      spawned (``make test`` -> pytest -> browsers). Those survivors keep
      touching the worktree we are about to publish, so start a new session and
      stop the whole group, the way _bounded_process does for workers.
    """

    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    kept: "collections.deque[str]" = collections.deque(maxlen=VERIFY_OUTPUT_TAIL_CHARS)
    seen = 0

    def drain() -> None:
        nonlocal seen
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(8192), b""):
            text = chunk.decode("utf-8", "replace")
            seen += len(text)
            kept.extend(text)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    def collected() -> str:
        body = "".join(kept)
        if seen > len(body):
            return (
                f"[verify output truncated — kept the last {len(body)} "
                f"of {seen} characters]\n" + body
            )
        return body

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        reader.join(timeout=5)
        raise subprocess.TimeoutExpired(command, timeout, output=collected())
    reader.join(timeout=5)
    return subprocess.CompletedProcess(command, process.returncode, collected(), "")


def _stop_process_group(process: "subprocess.Popen") -> None:
    """SIGTERM the group, then SIGKILL what is left."""

    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or a platform without process groups: fall back to
            # the direct child so the timeout still stops something.
            process.kill()
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def verify_lane(
    run: WorktreeRun,
    command: str,
    *,
    timeout: float = VERIFY_TIMEOUT_S,
    runner: Runner = _bounded_verify_runner,
) -> VerifyResult:
    """Run ``command`` in the lane worktree and judge it by its exit code.

    2026-09-17: a lane told to run the test suite and paste real output ran
    it 18 times, saw ``JSONDecodeError`` nine times, committed the failing
    tests anyway, and exited 0; the coordinator caught it only by re-running
    the suite by hand. ``lane_delivery`` proves work reached git — this
    proves the work *works*, so the runner never has to trust a transcript's
    prose about tests.
    """

    try:
        result = runner(
            command,
            cwd=str(run.worktree),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung suite is a failed lane, not a crashed runner: raising here
        # would turn "the tests hang" into an uncaught error with no summary
        # and no published branch. Truncate any partial output first so the
        # timeout note itself can never be cut away by the tail limit.
        partial = exc.stdout
        if isinstance(partial, bytes):
            # TimeoutExpired carries bytes even when the runner was in text
            # mode (bpo-87597); decode defensively.
            partial = partial.decode("utf-8", "replace")
        note = (
            f"verify command timed out after {timeout}s and was killed — "
            "a hang is a failed verification, not an inconclusive one"
        )
        kept = _verify_output_tail(partial or "")
        return VerifyResult(command, 124, f"{kept}\n{note}" if kept else note)
    except OSError as exc:
        # The shell itself could not start; 127 is the shell's own convention
        # for a command it cannot run. Fail the verification, do not crash.
        return VerifyResult(
            command, 127, _verify_output_tail(f"could not run verify command: {exc}")
        )
    return VerifyResult(
        command, result.returncode, _verify_output_tail(result.stdout or "")
    )


ASSIGNMENT_SCHEMA_VERSION = "1.0.0"


def _runs_directory(run: WorktreeRun) -> Path:
    """The per-repository directory holding this repository's run records.

    Both the terminal audit and the assignment sidecar live here, so a reader
    of either artifact finds the other by lane key without a second lookup.
    """

    result = subprocess.run(
        ["git", "-C", str(run.repository), "rev-parse", "--git-dir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = run.repository / git_dir
    destination = git_dir / "side-lane-runs"
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def lane_record_key(run: WorktreeRun) -> str:
    """The deterministic, path-safe key naming this lane's run records.

    A created lane keys on the branch it made, which is unique to that lane.
    An existing workspace keys on the run's own ``record_key`` instead: it
    keeps the owner's branch, and several lanes over one workspace's life
    would otherwise all resolve to the same record path and overwrite each
    other's audit.
    """

    return (run.record_key or run.branch).replace("/", "-")


def _publish_exclusive(path: Path, payload: str) -> None:
    """Create ``path`` once, atomically, refusing to replace an existing file.

    The payload lands in a same-directory temporary file that is flushed and
    fsynced before being hard-linked into place, so a reader either sees no
    file or sees the whole record — never a partial one. ``os.link`` is the
    create-exclusive step: it raises ``FileExistsError`` rather than
    overwriting, which is what makes an already-written record immutable.

    The temporary name is unique per attempt rather than per process. With a
    pid-only name, a temp file left behind by a crashed run — or a second
    claimant inside one process — made ``os.open`` raise ``EEXIST`` on the
    *temporary*, while the real destination did not exist at all: the caller
    then read the absent destination and reported the lock as "unreadable",
    naming no holder. The destination's own create-exclusive ``os.link`` is
    unchanged, so immutability is untouched; only the name of the scratch file
    the payload is staged in is now unguessable.
    """

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


SIDE_LANE_RUNTIME_DIR = "side-lane-runtime"


def repository_runtime_dir(
    repo: Path, runner: Runner = subprocess.run
) -> Path:
    """The per-repository directory holding one run's ephemeral host runtime.

    A host adapter that needs somewhere to write per-run files — a Devin run
    directory, a generated policy file — puts them here, in the repository's
    own ``.git``, beside ``side-lane-runs`` and the workspace locks. That is
    the seam that keeps a run's own runtime out of every worktree: a dedicated
    lane's files must not appear in its delivery check, and an existing owner
    workspace must not gain a runtime directory next to it, in the owner's
    parent directory, as a side effect of a run that claims to leave the tree
    exactly as it found it.
    """

    result = runner(
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = Path(repo) / git_dir
    destination = git_dir / SIDE_LANE_RUNTIME_DIR
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _read_existing_assignment(path: Path) -> Mapping[str, object]:
    """Read a previously published assignment record, or fail closed."""

    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorktreeError(
            f"existing assignment record is unreadable; refusing to guess: {path}"
        ) from exc
    if not isinstance(existing, dict):
        raise WorktreeError(f"existing assignment record is not an object: {path}")
    return existing


# Record fields describing one attempt rather than the assignment itself.
# `assigned_at` is the instant the FIRST attempt was assigned — a faithful
# retry must not move it — and `lane_worktree` is where that first attempt
# ran. Everything else (task id, weight, disposition, rework lineage, the
# configured route, the lane branch and repository) defines the assignment, and
# a retry disagreeing on any of it is a different assignment for the same lane:
# that must fail loudly rather than be folded into the first one.
_ATTEMPT_FIELDS = frozenset({"assigned_at", "lane_worktree"})


def _assignment_identity(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in record.items() if key not in _ATTEMPT_FIELDS
    }


@dataclass(frozen=True)
class AssignmentRecord:
    path: Path
    sha256: str
    reused: bool


def write_assignment(
    run: WorktreeRun,
    *,
    measurement: Mapping[str, object],
    assigned_at: str,
    planned: Mapping[str, object],
) -> AssignmentRecord:
    """Persist this lane's immutable assignment sidecar before the adapter runs.

    ``measurement`` is the already-validated, metadata-only preassignment
    (``side_lane.cli.load_measurement``): task id, host family, preassigned
    weight, planning disposition, and the rework/parent lineage. ``assigned_at``
    is the authoritative UTC instant this runner assigned the task. ``planned``
    is the route identity this runner was *configured* with — the host,
    provider, model, and gateway selected before any worker exists. That is
    deliberately not the resolved identity: what a provider actually answered
    with is recorded later, only in the terminal audit, so the two can be
    compared instead of conflated.

    The record is written beside the terminal audit under the same lane key and
    is never rewritten. A retry that presents the same assignment reuses the
    published file — and, with it, the original ``assigned_at`` — so no re-run
    can move a task's assignment instant. A retry that presents a *different*
    assignment raises instead of overwriting: the first assignment for a lane
    is the one that stands. Which fields that comparison covers, and why, is
    ``_ATTEMPT_FIELDS`` below.
    """

    record: dict[str, object] = {
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "task_id": measurement["task_id"],
        "host_family": measurement["host_family"],
        "preassigned_weight": measurement["preassigned_weight"],
        "planning_disposition": measurement["planning_disposition"],
        "rework": measurement["rework"],
        "parent_task_id": measurement.get("parent_task_id"),
        "assigned_at": assigned_at,
        "planned_provider": planned["provider"],
        "planned_model": planned["model"],
        "planned_host": planned["host"],
        "planned_gateway": planned["gateway"],
        "lane_branch": run.branch,
        "lane_worktree": str(run.worktree),
        "repository": str(run.repository),
    }
    path = _runs_directory(run) / f"{lane_record_key(run)}.assignment.json"
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        _publish_exclusive(path, payload)
    except FileExistsError:
        existing = _read_existing_assignment(path)
        if _assignment_identity(existing) != _assignment_identity(record):
            raise WorktreeError(
                f"assignment conflict: {path} already records a different "
                "assignment for this lane; refusing to overwrite it"
            ) from None
        # Digest the file as it actually exists: on a reuse the retained
        # assigned_at makes those bytes differ from the payload just built.
        return AssignmentRecord(
            path, hashlib.sha256(path.read_bytes()).hexdigest(), True
        )
    except OSError as exc:
        # Fail closed on anything else the filesystem refused. A worker must
        # never start against an assignment this runner could not record, so
        # this is a WorktreeError the caller turns into an aborted run rather
        # than an unhandled traceback.
        raise WorktreeError(
            f"could not publish assignment record {path}: {exc}"
        ) from exc
    return AssignmentRecord(
        path, hashlib.sha256(payload.encode("utf-8")).hexdigest(), False
    )


def write_audit(
    run: WorktreeRun,
    *,
    host: str,
    mode: str,
    provider: str,
    model: str,
    prompt: str,
    exit_status: int,
    status: str,
    gateway: str | None = None,
    auth_method: str | None = None,
    billable: bool | None = None,
    stdout: str = "",
    stderr: str = "",
    requested_model: str | None = None,
    resolved_model: str | None = None,
    usage: dict | None = None,
    provider_artifact: str | None = None,
    read_roots: Sequence[str] = (),
    web_domains: Sequence[str] = (),
    skill_catalog: "Sequence[Mapping[str, object]]" = (),
    run_mcp_servers: "Sequence[Mapping[str, object]]" = (),
    source_changes: Sequence[str] = (),
    source_check_unverified: str | None = None,
    assignment: "Mapping[str, object] | None" = None,
    execute_profile: str | None = None,
    existing_workspace: "Mapping[str, object] | None" = None,
    failure: "Mapping[str, object] | None" = None,
    publication: "Mapping[str, object] | None" = None,
) -> Path:
    """Persist one lane's run record outside its disposable worktree.

    ``read_roots`` records the canonical directories the coordinator granted
    the worker read-only access to, in addition to the lane worktree. It is
    additive: the field is always present as a list (empty when nothing was
    granted), so a reader can tell "no read root was requested" from "this
    record predates read roots".

    ``web_domains`` records the exact public documentation hostnames the
    coordinator granted through ``--web-domain`` (execute mode only), in the
    canonical sorted order the rules were rendered from. It records the grant
    the coordinator made; it is a permission-matching scope, not a network
    sandbox, and neither an unlisted destination nor an origin's own redirect
    is covered by it. Additive in the same way, so the schema stays 2.

    ``skill_catalog`` records the pinned skills materialized into the lane
    worktree for this run (execute mode only), each with name, version,
    content hash, license, origin, and the absolute path the worker was told
    to read. It records *delivery*, not host-native skill discovery and not
    any live tool call. Additive in the same way, so the schema version stays
    2.

    ``run_mcp_servers`` records the per-run MCP registrations delivered from
    the coordinator's validated ``--mcp-config`` file (execute mode only):
    server NAMES and the source config path — never URLs, env names'
    values, or header contents, which stay out of every audit record. It
    records delivery, not authentication and not any live tool call.

    ``source_changes`` records the coordinator-checkout paths that changed
    between dispatch and completion (execute mode only), as detected by the
    before/after status comparison. It records the change, not who made it —
    an out-of-lane worker write and a concurrent human edit are
    indistinguishable here. Additive like the rest, schema stays 2.

    ``source_check_unverified`` says why the comparison above could not be
    made, when it could not be made at all — a checkout that could not be read
    before the worker started, or one that could not be read again after. It is
    the same distinction ``source_changes`` already draws with an empty list
    versus a failed look, written out so the reason travels with the record:
    an empty list beside a ``null`` here means the checkout was compared and
    was untouched, while an empty list beside a reason means nobody knows. It
    is ``null`` on the ordinary path, and a review lane, which gets no source
    guarantee to check, never sets it. Additive in the same way, so the schema
    version stays 2.

    ``assignment`` links the immutable assignment sidecar this run published
    before its adapter started (its path, digest, task id, and schema version).
    It is written on every outcome that reaches an audit — delivered, failed,
    or unverified — so a failed run still points at what it was assigned. It is
    ``null`` only when the run carried no measurement metadata at all, which
    means this run is *explicitly unmeasured*; it does not mean a measurement
    was attempted and lost, because a sidecar that cannot be published or that
    conflicts aborts the run before any adapter starts. Additive in the same
    way, so the schema version stays 2.

    ``execute_profile`` records which execute-lane tool profile this run
    selected — the per-command allowlist of the public default, or the local
    developer profile a route declaring ``execution_location:
    local-user-workspace`` defaults to. It records the selection, not what the
    worker did with it, and it is ``null`` for a review run and for any record
    written before the profile existed. Additive like the rest, schema stays 2.

    ``existing_workspace`` records the one thing that makes this run's delivery
    verdict readable: whether the lane ran in a workspace the runner created
    from HEAD, or in a pre-existing owner workspace it was explicitly pointed
    at. When it is set it carries that workspace's branch, HEAD, whether it is
    the shared primary checkout rather than a linked worktree, the baseline
    digest captured before the worker started, and the per-path deltas the
    worker actually produced — content and index digests, never file contents.
    It is ``null`` for a created-worktree lane. Additive like the rest, so the
    schema version stays 2.

    ``failure`` records the run that ended by raising rather than by returning:
    the stage that failed, the exception's type, and its redacted message, with
    ``delivered`` and ``verified`` explicitly ``null``. It exists so the
    outcome that produces no ``stdout``, no ``stderr`` and no provider result
    still has a record naming what happened — and it is ``null`` for every run
    that reached an audit normally, so an existing field-reader is unaffected.
    It carries no prompt, file content, or credential: the message is passed
    through the same provider-secret redaction every other recorded string
    uses. Additive in the same way, so the schema version stays 2.

    ``publication`` records what the run's task authority said about
    publication, and what it could not: the task's no-external-publication
    authority when the run carried it, the host's own enforcement of that
    refusal, and that the worker's own publication was not verified. The caller
    assembles it before the worker starts, and this audit is written once the
    adapter has returned but still ahead of the delivery inspection and the
    push decision — so nothing in this block is a publication outcome. It holds
    neither the worker's own publication, which nothing in the run observes,
    nor the runner's eventual push, which the caller keeps in its summary
    instead; a reader must not read this block as evidence about either, and
    must not read a missing publication outcome here as one that did not
    happen. Additive in the same way, so the schema version stays 2.
    """
    destination = _runs_directory(run)
    path = destination / f"{lane_record_key(run)}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "host": host,
                "mode": mode,
                "provider": provider,
                "gateway": gateway,
                "auth_method": auth_method,
                "billable": billable,
                "model": model,
                "requested_model": requested_model or model,
                "resolved_model": resolved_model,
                "usage": usage,
                "provider_artifact": provider_artifact,
                "repository": str(run.repository),
                "worktree": str(run.worktree),
                "read_roots": list(read_roots),
                "web_domains": list(web_domains),
                "skill_catalog": [dict(item) for item in skill_catalog],
                "run_mcp_servers": [dict(item) for item in run_mcp_servers],
                "source_changes": list(source_changes),
                "source_check_unverified": source_check_unverified,
                "assignment": dict(assignment) if assignment is not None else None,
                "execute_profile": execute_profile,
                "existing_workspace": (
                    dict(existing_workspace)
                    if existing_workspace is not None
                    else None
                ),
                "failure": dict(failure) if failure is not None else None,
                "publication": dict(publication) if publication is not None else None,
                "branch": run.branch,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "exit_status": exit_status,
                "git_status": status,
                "stdout": stdout,
                "stderr": stderr,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _refuse_existing_workspace(run: WorktreeRun, action: str) -> None:
    """Stop disposal-shaped operations from touching an owner's workspace.

    Every one of these ends in ``git worktree remove`` or ``git branch -d`` on
    ``run.worktree``/``run.branch``. For a created lane that is cleanup; for an
    existing workspace it is the owner's checkout and the owner's branch, and
    neither was ever this run's to remove. The flag is the one thing that tells
    the two apart, so the refusal lives here rather than at each call site.
    """

    if run.existing_workspace:
        raise WorktreeError(
            f"refusing to {action} the existing owner workspace {run.worktree}: it "
            "was supplied for this run, not created by it, and may hold work "
            "belonging to others"
        )


def remove_worktree(run: WorktreeRun, runner: Runner = subprocess.run) -> None:
    _refuse_existing_workspace(run, "remove")
    if _git(run.worktree, ["status", "--porcelain"], runner):
        raise WorktreeError("refusing to remove a dirty lane worktree")
    merged = _git(run.repository, ["branch", "--merged", "HEAD"], runner).splitlines()
    if not any(line.strip().lstrip("* ") == run.branch for line in merged):
        raise WorktreeError("refusing to remove an unmerged lane worktree")
    _git(run.repository, ["worktree", "remove", str(run.worktree)], runner)


def dispose_clean_worktree(run: WorktreeRun, runner: Runner = subprocess.run) -> None:
    """Remove a clean disposable lane and its unmodified branch."""

    _refuse_existing_workspace(run, "dispose")
    if _git(run.worktree, ["status", "--porcelain"], runner):
        raise WorktreeError(
            f"disposable lane unexpectedly changed; preserved for diagnosis: {run.worktree}"
        )
    if _git(run.worktree, ["rev-parse", "HEAD"], runner) != run.starting_commit:
        raise WorktreeError("disposable lane branch moved unexpectedly")
    _git(run.repository, ["worktree", "remove", str(run.worktree)], runner)
    _git(run.repository, ["branch", "-d", run.branch], runner)
