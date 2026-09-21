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


def ensure_lane_exclusion(repo: Path, *, runner: Runner = subprocess.run) -> Path:
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
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{LANE_EXCLUDE_PATTERN}\n")
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude lane worktrees in {exclude}: {exc}"
        ) from exc
    return exclude


def ensure_scratch_exclusion(repo: Path, *, runner: Runner = subprocess.run) -> Path:
    """Exclude ``.side-lane-scratch/`` from ``git status`` in every checkout.

    Lane scratch files are throwaway by contract: the runner pre-creates the
    directory in each lane worktree, and without this entry it would itself
    fail the dirty checks that guard lane creation and disposal. The entry is
    root-anchored (only the worktree-root scratch directory is reserved, so a
    project directory such as ``src/.side-lane-scratch/`` stays visible) and,
    like the lane-root entry above, is shared with linked worktrees through
    the common ``.git/info/exclude``.
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
            # copy.
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
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{SCRATCH_EXCLUDE_PATTERN}\n")
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude scratch directory in {exclude}: {exc}"
        ) from exc
    return exclude


def ensure_devin_local_mcp_exclusion(repo: Path, *, runner: Runner = subprocess.run) -> Path:
    """Exclude the Devin per-run local MCP file from ``git status``/``git add -A``.

    The Devin adapter materializes ``.devin/mcp_config.local.json`` inside the
    lane worktree for the run's lifetime (restored or removed on exit). Without
    this entry a worker's mid-run ``git add -A`` would commit the generated
    file — URL plus ``${ENV}`` credential reference, never a value. The entry
    is root-anchored and shared with linked worktrees through the common
    ``.git/info/exclude``. Excludes never apply to tracked files, so the
    adapter rejects a tracked local config before merge; an existing untracked
    file's content is preserved by the merge/restore path. The entry only hides
    the untracked file from status and add-globbing.
    """

    git_dir = Path(_git(repo, ["rev-parse", "--git-common-dir"], runner))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if any(line.rstrip(" \t") == DEVIN_LOCAL_MCP_EXCLUDE_PATTERN
               for line in existing.splitlines()):
            return exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{DEVIN_LOCAL_MCP_EXCLUDE_PATTERN}\n")
    except OSError as exc:
        raise WorktreeError(
            f"cannot exclude the Devin local MCP config in {exclude}: {exc}"
        ) from exc
    return exclude


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
    repo: Path, worktree: Path, *, runner: Runner = subprocess.run
) -> Path:
    """Create ``<worktree>/.side-lane-scratch`` for run-local throwaway files.

    Scratch (trial scripts, notes, intermediate output) belongs inside the lane
    worktree yet must never reach ``git status`` or ``git add -A``, so the
    exclusion is ensured before the directory exists. It is not removed
    per-run: a review lane is disposed with its whole worktree, and an execute
    lane's worktree — scratch included — is preserved for the coordinator's
    review until the lane branch is dealt with.
    """

    ensure_scratch_exclusion(repo, runner=runner)
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


@dataclass(frozen=True)
class LaneDelivery:
    """Whether a lane's work actually reached git.

    A worker can write correct files, exit 0, and leave them untracked. The
    run then reports success for work that disappears with the worktree —
    observed three times in one session (2026-09-17). Exit status cannot see
    this; only the tree can.
    """

    committed: bool
    uncommitted: tuple[str, ...]

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


def _changed_paths(worktree: Path, runner: Runner) -> tuple[str, ...]:
    """Every path with uncommitted work, from NUL-delimited porcelain.

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

    Paths are reported as git spells them, relative to the worktree root.
    """
    result = runner(
        ["git", "-C", str(worktree), "status", "--porcelain", "-z"],
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
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            index += 1  # the original path rides in its own field; skip it
    return tuple(paths)


def lane_delivery(run: WorktreeRun, runner: Runner = subprocess.run) -> LaneDelivery:
    """Inspect the lane worktree for work that actually landed in git."""
    head = _git(run.worktree, ["rev-parse", "HEAD"], runner)
    changed = _changed_paths(run.worktree, runner)
    return LaneDelivery(committed=head != run.starting_commit, uncommitted=changed)


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
    """The deterministic, path-safe key naming this lane's run records."""

    return run.branch.replace("/", "-")


def _publish_exclusive(path: Path, payload: str) -> None:
    """Create ``path`` once, atomically, refusing to replace an existing file.

    The payload lands in a same-directory temporary file that is flushed and
    fsynced before being hard-linked into place, so a reader either sees no
    file or sees the whole record — never a partial one. ``os.link`` is the
    create-exclusive step: it raises ``FileExistsError`` rather than
    overwriting, which is what makes an already-written record immutable.
    """

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


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
    assignment: "Mapping[str, object] | None" = None,
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

    ``assignment`` links the immutable assignment sidecar this run published
    before its adapter started (its path, digest, task id, and schema version).
    It is written on every outcome that reaches an audit — delivered, failed,
    or unverified — so a failed run still points at what it was assigned. It is
    ``null`` only when the run carried no measurement metadata at all, which
    means this run is *explicitly unmeasured*; it does not mean a measurement
    was attempted and lost, because a sidecar that cannot be published or that
    conflicts aborts the run before any adapter starts. Additive in the same
    way, so the schema version stays 2.
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
                "assignment": dict(assignment) if assignment is not None else None,
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


def remove_worktree(run: WorktreeRun, runner: Runner = subprocess.run) -> None:
    if _git(run.worktree, ["status", "--porcelain"], runner):
        raise WorktreeError("refusing to remove a dirty lane worktree")
    merged = _git(run.repository, ["branch", "--merged", "HEAD"], runner).splitlines()
    if not any(line.strip().lstrip("* ") == run.branch for line in merged):
        raise WorktreeError("refusing to remove an unmerged lane worktree")
    _git(run.repository, ["worktree", "remove", str(run.worktree)], runner)


def dispose_clean_worktree(run: WorktreeRun, runner: Runner = subprocess.run) -> None:
    """Remove a clean disposable lane and its unmodified branch."""

    if _git(run.worktree, ["status", "--porcelain"], runner):
        raise WorktreeError(
            f"disposable lane unexpectedly changed; preserved for diagnosis: {run.worktree}"
        )
    if _git(run.worktree, ["rev-parse", "HEAD"], runner) != run.starting_commit:
        raise WorktreeError("disposable lane branch moved unexpectedly")
    _git(run.repository, ["worktree", "remove", str(run.worktree)], runner)
    _git(run.repository, ["branch", "-d", run.branch], runner)
