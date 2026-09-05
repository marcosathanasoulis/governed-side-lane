from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence


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


def _git(repo: Path, args: Sequence[str], runner: Runner) -> str:
    result = runner(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise WorktreeError((result.stderr or result.stdout).strip() or "git command failed")
    return result.stdout.strip()


def safe_lane_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized or len(normalized) > 48:
        raise WorktreeError("lane name must contain 1-48 safe alphanumeric characters")
    return normalized


LANE_ROOT = ".side-lanes"
LANE_EXCLUDE_PATTERN = f"{LANE_ROOT}/"


def ensure_lane_exclusion(repo: Path, *, runner: Runner = subprocess.run) -> Path:
    """Exclude ``.side-lanes/`` from ``git status`` in the coordinator checkout.

    Lane worktrees live under the governed repository so their path is
    predictable and audited, but they are never committed. Without this entry
    the first lane leaves an untracked directory behind and every later launch
    fails the dirty-checkout check. ``.git/info/exclude`` is local, untracked,
    and idempotent to update, so no repository file changes.
    """

    git_dir = Path(_git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"], runner))
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if LANE_EXCLUDE_PATTERN in {line.strip() for line in existing.splitlines()}:
            return exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{LANE_EXCLUDE_PATTERN}\n")
    except OSError as exc:
        raise WorktreeError(f"cannot exclude lane worktrees in {exclude}: {exc}") from exc
    return exclude


def create_worktree(repo: Path, lane_name: str, *, runner: Runner = subprocess.run,
                    now: datetime | None = None) -> WorktreeRun:
    repo = repo.resolve()
    top = Path(_git(repo, ["rev-parse", "--show-toplevel"], runner)).resolve()
    if top != repo:
        raise WorktreeError("execute mode requires the repository root")
    ensure_lane_exclusion(repo, runner=runner)
    if _git(repo, ["status", "--porcelain"], runner):
        raise WorktreeError("coordinator checkout is dirty; commit or stash before lane creation")
    if not _git(repo, ["branch", "--show-current"], runner):
        raise WorktreeError("detached HEAD is not supported")
    safe = safe_lane_name(lane_name)
    starting_commit = _git(repo, ["rev-parse", "HEAD"], runner)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    branch = f"side-lane/{safe}-{stamp}"
    worktree = repo / LANE_ROOT / "worktrees" / f"{safe}-{stamp}"
    _git(repo, ["check-ref-format", "--branch", branch], runner)
    if worktree.exists():
        raise WorktreeError(f"worktree destination already exists: {worktree}")
    _git(repo, ["worktree", "add", "-b", branch, str(worktree), "HEAD"], runner)
    return WorktreeRun(repo, worktree, branch, safe, starting_commit)


def git_status(run: WorktreeRun, runner: Runner = subprocess.run) -> str:
    return _git(run.worktree, ["status", "--short", "--branch"], runner)


def write_audit(run: WorktreeRun, *, host: str, mode: str, provider: str,
                model: str, prompt: str, exit_status: int, status: str,
                gateway: str | None = None, auth_method: str | None = None,
                billable: bool | None = None, stdout: str = "",
                stderr: str = "") -> Path:
    result = subprocess.run(["git", "-C", str(run.repository), "rev-parse", "--git-dir"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=True)
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = run.repository / git_dir
    destination = git_dir / "side-lane-runs"
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{run.branch.replace('/', '-')}.json"
    path.write_text(json.dumps({
        "schema_version": 2, "host": host, "mode": mode,
        "provider": provider, "gateway": gateway, "auth_method": auth_method,
        "billable": billable, "model": model,
        "repository": str(run.repository), "worktree": str(run.worktree),
        "branch": run.branch,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "exit_status": exit_status, "git_status": status,
        "stdout": stdout, "stderr": stderr,
    }, indent=2) + "\n", encoding="utf-8")
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
