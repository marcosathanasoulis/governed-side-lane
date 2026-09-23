from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from side_lane import worktrees


class WorktreeTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
        return repo

    def test_creates_unique_dedicated_branch_and_worktree(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(
            repo, "Parser Task", now=datetime(2026, 8, 29, 12, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(lane.branch, "side-lane/parser-task-20260829123000")
        self.assertTrue((lane.worktree / ".git").exists())
        self.assertNotEqual(lane.worktree, repo)
        self.assertEqual(lane.starting_commit, subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip())
        self.assertFalse((repo / "INPROCESS.md").exists())

    def test_audit_persists_findings_outside_the_disposable_worktree(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(
            repo, "review", now=datetime(2026, 8, 29, 12, 32, tzinfo=timezone.utc)
        )
        path = worktrees.write_audit(
            lane, host="claude", mode="review", provider="claude",
            model="claude-sonnet-5", prompt="Review", exit_status=0,
            status="## review", stdout="finding: bug in api.py", stderr="warn",
            requested_model="claude-sonnet-5", resolved_model="claude-sonnet-5-20260901",
            usage={"input_tokens": 12}, provider_artifact="/tmp/receipt.json",
        )
        worktrees.dispose_clean_worktree(lane)
        self.assertFalse(lane.worktree.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["stdout"], "finding: bug in api.py")
        self.assertEqual(payload["stderr"], "warn")
        self.assertEqual(payload["requested_model"], "claude-sonnet-5")
        self.assertEqual(payload["resolved_model"], "claude-sonnet-5-20260901")
        self.assertEqual(payload["usage"], {"input_tokens": 12})
        self.assertEqual(payload["provider_artifact"], "/tmp/receipt.json")

    def test_refuses_dirty_coordinator_checkout(self) -> None:
        repo = self.make_repo()
        (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.create_worktree(repo, "task")

    def test_cleanup_refuses_dirty_or_unmerged_lane(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(
            repo, "task", now=datetime(2026, 8, 29, 12, 31, tzinfo=timezone.utc)
        )
        (lane.worktree / "new.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.remove_worktree(lane)
        (lane.worktree / "new.txt").unlink()
        with self.assertRaisesRegex(worktrees.WorktreeError, "unmerged"):
            worktrees.remove_worktree(lane)

    def test_disposes_clean_review_lane(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(
            repo, "review",
            now=datetime(2026, 8, 29, 12, 32, tzinfo=timezone.utc),
        )
        worktrees.dispose_clean_worktree(lane)
        self.assertFalse(lane.worktree.exists())
        branches = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", lane.branch],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(branches, "")

    def test_dirty_disposable_lane_is_preserved(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "review")
        (lane.worktree / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "preserved for diagnosis"):
            worktrees.dispose_clean_worktree(lane)
        self.assertTrue(lane.worktree.exists())

    def test_worktree_creation_does_not_create_claims_file(self) -> None:
        repo = self.make_repo()
        worktrees.create_worktree(repo, "task")
        self.assertFalse((repo / "INPROCESS.md").exists())

    def test_disposable_lane_with_commit_is_preserved(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "review")
        (lane.worktree / "file.txt").write_text("review commit\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(lane.worktree), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(lane.worktree), "commit", "-m", "unexpected"], check=True, capture_output=True)
        with self.assertRaisesRegex(worktrees.WorktreeError, "branch moved"):
            worktrees.dispose_clean_worktree(lane)
        self.assertTrue(lane.worktree.exists())

    def test_publish_pushes_the_lane_branch_from_the_coordinator_repo(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "task")
        calls = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            # Publication first asks the worktree which branch it is on; answer
            # honestly, or the fail-closed guard (correctly) refuses to push.
            out = f"{lane.branch}\n" if "rev-parse" in argv else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        ref = worktrees.publish_lane_branch(lane, runner=runner)
        self.assertEqual(ref, f"origin/{lane.branch}")
        # The push runs against the coordinator repository, not the worktree.
        self.assertIn(
            ["git", "-C", str(repo.resolve()), "push", "--set-upstream", "origin", lane.branch],
            calls,
        )

    def test_publish_delivers_the_branch_to_a_real_remote(self) -> None:
        repo = self.make_repo()
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        origin = Path(remote.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
        lane = worktrees.create_worktree(repo, "deliver")
        (lane.worktree / "lane.txt").write_text("delivered\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(lane.worktree), "add", "lane.txt"], check=True)
        subprocess.run(["git", "-C", str(lane.worktree), "commit", "-m", "work"], check=True, capture_output=True)

        ref = worktrees.publish_lane_branch(lane)

        self.assertEqual(ref, f"origin/{lane.branch}")
        remote_head = subprocess.run(
            ["git", "--git-dir", str(origin), "rev-parse", f"refs/heads/{lane.branch}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        local_head = subprocess.run(
            ["git", "-C", str(lane.worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, local_head)

    def test_publish_failure_carries_git_stderr(self) -> None:
        repo = self.make_repo()  # no remote named "origin" exists
        lane = worktrees.create_worktree(repo, "orphan")
        with self.assertRaises(worktrees.WorktreeError) as raised:
            worktrees.publish_lane_branch(lane)
        message = str(raised.exception)
        self.assertIn(lane.branch, message)
        self.assertIn("origin", message)
        # The operator diagnoses a push from git's own complaint, not ours.
        self.assertIn("does not appear to be a git repository", message)


class SideLaneExclusionTests(WorktreeTests):
    def test_untracked_lane_worktrees_do_not_count_as_dirty(self) -> None:
        repo = self.make_repo()
        first = worktrees.create_worktree(repo, "first")
        self.assertTrue(first.worktree.is_dir())
        self.assertEqual(first.worktree.parent.parent, repo.resolve() / ".side-lanes")
        exclude = repo / ".git" / "info" / "exclude"
        self.assertEqual(exclude.read_text(encoding="utf-8").count("/.side-lanes/"), 1)
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                        check=True, capture_output=True, text=True).stdout, "")
        second = worktrees.create_worktree(repo, "second")
        self.assertNotEqual(first.worktree, second.worktree)
        self.assertEqual(exclude.read_text(encoding="utf-8").count("/.side-lanes/"), 1)

    def test_other_untracked_files_still_block_lane_creation(self) -> None:
        repo = self.make_repo()
        worktrees.create_worktree(repo, "first")
        (repo / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.create_worktree(repo, "second")

    def test_existing_exclude_entries_are_preserved(self) -> None:
        repo = self.make_repo()
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.log\n", encoding="utf-8")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "*.log\n/.side-lanes/\n")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "*.log\n/.side-lanes/\n")

    def test_legacy_unanchored_entry_is_upgraded_in_place(self) -> None:
        repo = self.make_repo()
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.log\n.side-lanes/\nbuild/\n", encoding="utf-8")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "*.log\n/.side-lanes/\nbuild/\n")
        exclude.write_text("/.side-lanes/\n.side-lanes/\n", encoding="utf-8")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "/.side-lanes/\n")
        # A user-authored line with leading whitespace is a different pattern: untouched,
        # and the real entry is still added.
        exclude.write_text("  .side-lanes/\n", encoding="utf-8")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "  .side-lanes/\n/.side-lanes/\n")
        # Trailing whitespace on the tool entry is tolerated.
        exclude.write_text(".side-lanes/ \t\n", encoding="utf-8")
        worktrees.ensure_lane_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8"), "/.side-lanes/\n")
        nested = repo / "src" / ".side-lanes"
        nested.mkdir(parents=True)
        (nested / "file").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.create_worktree(repo, "task")

    def test_nested_side_lanes_directory_still_counts_as_dirty(self) -> None:
        repo = self.make_repo()
        worktrees.create_worktree(repo, "first")
        nested = repo / "src" / ".side-lanes"
        nested.mkdir(parents=True)
        (nested / "file").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.create_worktree(repo, "second")


class ScratchDirectoryTests(WorktreeTests):
    def container_repo(self) -> Path:
        # Sibling-root writes live next to the repository, so give the test its
        # own container instead of leaking into the system temp directory.
        container = tempfile.TemporaryDirectory()
        self.addCleanup(container.cleanup)
        repo = Path(container.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
        return repo

    def test_lane_worktree_gets_git_excluded_scratch_directory(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "task")
        scratch = lane.worktree / ".side-lane-scratch"
        self.assertTrue(scratch.is_dir())
        exclude = repo / ".git" / "info" / "exclude"
        self.assertIn(".side-lane-scratch/", exclude.read_text(encoding="utf-8"))
        (scratch / "note.txt").write_text("throwaway\n", encoding="utf-8")
        # Ignored in the linked lane worktree: neither status nor add -A sees it.
        self.assertEqual(subprocess.run(
            ["git", "-C", str(lane.worktree), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout, "")
        staged = subprocess.run(
            ["git", "-C", str(lane.worktree), "add", "-A", "--dry-run"],
            check=True, capture_output=True, text=True).stdout
        self.assertNotIn(".side-lane-scratch", staged)
        # The coordinator checkout stays clean too.
        self.assertEqual(subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout, "")

    def test_scratch_exclusion_is_root_anchored_not_nested(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "task")
        nested = lane.worktree / "src" / ".side-lane-scratch"
        nested.mkdir(parents=True)
        (nested / "output").write_text("build artifact\n", encoding="utf-8")
        root_scratch = lane.worktree / ".side-lane-scratch"
        (root_scratch / "note.txt").write_text("throwaway\n", encoding="utf-8")

        def is_ignored(relative: str) -> bool:
            result = subprocess.run(
                ["git", "-C", str(lane.worktree), "check-ignore", "--quiet", relative])
            return result.returncode == 0

        # The nested directory is a real project path, not lane scratch: it
        # must stay visible to the dirty check.
        self.assertFalse(is_ignored("src/.side-lane-scratch/output"))
        self.assertTrue(is_ignored(".side-lane-scratch/note.txt"))
        status = subprocess.run(
            ["git", "-C", str(lane.worktree), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True).stdout
        self.assertIn("src/.side-lane-scratch/output", status)
        self.assertNotIn(".side-lane-scratch/note.txt", status)

    def test_legacy_unanchored_scratch_entry_upgrades_in_place(self) -> None:
        repo = self.make_repo()
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"{worktrees.LEGACY_SCRATCH_EXCLUDE_PATTERN}\n", encoding="utf-8")
        worktrees.ensure_scratch_exclusion(repo)
        lines = exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count(worktrees.SCRATCH_EXCLUDE_PATTERN), 1)
        self.assertNotIn(worktrees.LEGACY_SCRATCH_EXCLUDE_PATTERN, lines)
        # Idempotent against a checkout that already has both lines written.
        exclude.write_text(
            f"{worktrees.LEGACY_SCRATCH_EXCLUDE_PATTERN}\n{worktrees.SCRATCH_EXCLUDE_PATTERN}\n",
            encoding="utf-8")
        worktrees.ensure_scratch_exclusion(repo)
        lines = exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count(worktrees.SCRATCH_EXCLUDE_PATTERN), 1)
        self.assertNotIn(worktrees.LEGACY_SCRATCH_EXCLUDE_PATTERN, lines)

    def test_failed_scratch_setup_rolls_back_the_leaked_worktree_and_branch(self) -> None:
        repo = self.make_repo()
        with mock.patch("side_lane.worktrees.prepare_scratch_directory",
                        side_effect=worktrees.WorktreeError("boom")):
            with self.assertRaises(worktrees.WorktreeError):
                worktrees.create_worktree(repo, "task")
        # Nothing left behind: no linked worktree directory and no dangling branch.
        worktree_list = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(worktree_list.count("worktree "), 1)  # only the main checkout
        branches = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "side-lane/*"],
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout, "")

    def test_scratch_exclude_entry_is_appended_once(self) -> None:
        repo = self.make_repo()
        worktrees.create_worktree(repo, "first")
        exclude = repo / ".git" / "info" / "exclude"
        self.assertEqual(exclude.read_text(encoding="utf-8").count(".side-lane-scratch/"), 1)
        worktrees.ensure_scratch_exclusion(repo)
        self.assertEqual(exclude.read_text(encoding="utf-8").count(".side-lane-scratch/"), 1)
        worktrees.create_worktree(repo, "second")
        self.assertEqual(exclude.read_text(encoding="utf-8").count(".side-lane-scratch/"), 1)

    def test_devin_local_mcp_exclusion_is_root_anchored_and_appended_once(self) -> None:
        repo = self.make_repo()
        exclude, added = worktrees.ensure_devin_local_mcp_exclusion(repo)
        self.assertTrue(added)
        lines = exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/.devin/mcp_config.local.json"), 1)
        _, added = worktrees.ensure_devin_local_mcp_exclusion(repo)
        self.assertFalse(added)
        self.assertEqual(exclude.read_text(encoding="utf-8").splitlines().count(
            "/.devin/mcp_config.local.json"), 1)
        generated = repo / ".devin" / "mcp_config.local.json"
        generated.parent.mkdir()
        generated.write_text("{}\n", encoding="utf-8")
        self.assertEqual(subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout, "")
        nested = repo / "src" / ".devin" / "mcp_config.local.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("{}\n", encoding="utf-8")
        # Root-anchored: a nested same-named file stays visible to status.
        nested_check = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q",
             "src/.devin/mcp_config.local.json"], capture_output=True)
        self.assertNotEqual(nested_check.returncode, 0)
        self.assertIn("src/", subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout)

    def test_scratch_files_never_block_disposal_and_die_with_the_worktree(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "review")
        scratch = lane.worktree / ".side-lane-scratch"
        scratch.joinpath("notes").mkdir()
        (scratch / "notes" / "trace.txt").write_text("x\n", encoding="utf-8")
        worktrees.dispose_clean_worktree(lane)
        self.assertFalse(lane.worktree.exists())

    def test_sibling_root_worktree_also_gets_the_scratch_directory(self) -> None:
        repo = self.container_repo()
        lane = worktrees.create_worktree(repo, "task",
                                         worktree_root=str(repo.parent / "lanes"))
        self.assertTrue((lane.worktree / ".side-lane-scratch").is_dir())
        self.assertIn(".side-lane-scratch/",
                      (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertEqual(subprocess.run(
            ["git", "-C", str(lane.worktree), "status", "--porcelain"],
            check=True, capture_output=True, text=True).stdout, "")


class WorktreeRootTests(WorktreeTests):
    def make_repo(self) -> Path:
        # Sibling-root tests write next to the repository, so give each test its
        # own container instead of leaking into the system temp directory.
        container = tempfile.TemporaryDirectory()
        self.addCleanup(container.cleanup)
        repo = Path(container.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
        return repo

    def test_sibling_root_is_used_and_needs_no_exclusion(self) -> None:
        repo = self.make_repo()
        root = repo.parent / "lanes"
        lane = worktrees.create_worktree(repo, "task", worktree_root=str(root))
        self.assertEqual(lane.worktree.parent.resolve(), root.resolve())
        self.assertTrue((lane.worktree / ".git").exists())
        self.assertFalse((repo / ".side-lanes").exists())
        exclude = repo / ".git" / "info" / "exclude"
        self.assertNotIn("/.side-lanes/", exclude.read_text(encoding="utf-8") if exclude.exists() else "")
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                        check=True, capture_output=True, text=True).stdout, "")

    def test_relative_root_is_anchored_to_the_repository(self) -> None:
        repo = self.make_repo()
        lane = worktrees.create_worktree(repo, "task", worktree_root="../side-lanes-elsewhere")
        self.assertEqual(lane.worktree.parent.resolve(), (repo.parent / "side-lanes-elsewhere").resolve())

    def test_environment_override_and_default(self) -> None:
        repo = self.make_repo()
        self.assertEqual(worktrees.resolve_worktree_root(repo, env={}), repo / ".side-lanes" / "worktrees")
        self.assertEqual(worktrees.resolve_worktree_root(repo, env={"SIDE_LANE_WORKTREE_ROOT": "../x"}), (repo.parent / "x"))
        self.assertEqual(worktrees.resolve_worktree_root(repo, "", env={"SIDE_LANE_WORKTREE_ROOT": "../x"}), (repo.parent / "x"))
        self.assertEqual(worktrees.resolve_worktree_root(repo, ".side-lanes/worktrees"), repo / ".side-lanes" / "worktrees")
        # The default reached through a symlinked checkout path is still the default.
        alias = repo.parent / "repo-alias"
        alias.symlink_to(repo)
        self.assertEqual(worktrees.resolve_worktree_root(repo, str(alias / ".side-lanes" / "worktrees")),
                         repo / ".side-lanes" / "worktrees")

    def test_nested_non_default_root_is_refused(self) -> None:
        repo = self.make_repo()
        for bad in ("lanes", ".", str(repo / "sub"), ".side-lanes/other"):
            with self.assertRaisesRegex(worktrees.WorktreeError, "outside the repository"):
                worktrees.resolve_worktree_root(repo, bad)

    def test_symlink_back_into_the_repository_is_refused(self) -> None:
        repo = self.make_repo()
        link = repo.parent / "lanes-link"
        link.symlink_to(repo / "nested-lanes")
        with self.assertRaisesRegex(worktrees.WorktreeError, "outside the repository"):
            worktrees.resolve_worktree_root(repo, str(link))
        via_parent = repo.parent / "parent-link"
        via_parent.symlink_to(repo)
        with self.assertRaisesRegex(worktrees.WorktreeError, "outside the repository"):
            worktrees.resolve_worktree_root(repo, str(via_parent / "sub"))
        outside = repo.parent / "outside-link"
        (repo.parent / "real-outside").mkdir()
        outside.symlink_to(repo.parent / "real-outside")
        self.assertEqual(worktrees.resolve_worktree_root(repo, str(outside)), outside)
        # Lexically in-repo entry that points outside is still refused.
        inner_link = repo / "lanes-out"
        inner_link.symlink_to(repo.parent / "real-outside")
        with self.assertRaisesRegex(worktrees.WorktreeError, "outside the repository"):
            worktrees.resolve_worktree_root(repo, "lanes-out")


if __name__ == "__main__":
    unittest.main()


class PublishGuardTests(unittest.TestCase):
    """Publication must not vouch for commits that are not on the branch it pushes."""

    def run_for(self, branch: str = "side-lane/task-1") -> worktrees.WorktreeRun:
        return worktrees.WorktreeRun(
            Path("/repo"), Path("/repo/.side-lanes/worktrees/task-1"), branch, "task", "abc123"
        )

    def runner_for(self, head: str, *, push=None):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=head + "\n", stderr="")
            if "push" in cmd:
                if push is not None:
                    return push(cmd, kwargs)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return runner, calls

    def test_publishes_when_the_worktree_is_on_the_lane_branch(self) -> None:
        run = self.run_for()
        runner, calls = self.runner_for("side-lane/task-1")
        ref = worktrees.publish_lane_branch(run, runner=runner)
        self.assertEqual(ref, "origin/side-lane/task-1")
        push = [c for c, _ in calls if "push" in c][0]
        self.assertEqual(push[-3:], ["--set-upstream", "origin", "side-lane/task-1"])

    def test_refuses_when_the_worker_switched_branches(self) -> None:
        runner, calls = self.runner_for("some-other-branch")
        with self.assertRaises(worktrees.WorktreeError) as caught:
            worktrees.publish_lane_branch(self.run_for(), runner=runner)
        self.assertIn("some-other-branch", str(caught.exception))
        self.assertFalse([c for c, _ in calls if "push" in c], "must not push on a mismatch")

    def test_refuses_on_a_detached_head(self) -> None:
        runner, calls = self.runner_for("HEAD")
        with self.assertRaises(worktrees.WorktreeError) as caught:
            worktrees.publish_lane_branch(self.run_for(), runner=runner)
        self.assertIn("detached", str(caught.exception))
        self.assertFalse([c for c, _ in calls if "push" in c])

    def test_push_is_bounded_and_noninteractive(self) -> None:
        runner, calls = self.runner_for("side-lane/task-1")
        worktrees.publish_lane_branch(self.run_for(), runner=runner)
        _, kwargs = [c for c in calls if "push" in c[0]][0]
        self.assertEqual(kwargs["timeout"], worktrees.PUBLISH_TIMEOUT_S)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", kwargs["env"]["GIT_SSH_COMMAND"])

    def test_a_hanging_push_becomes_a_worktree_error(self) -> None:
        def push(cmd, kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        runner, _ = self.runner_for("side-lane/task-1", push=push)
        with self.assertRaises(worktrees.WorktreeError) as caught:
            worktrees.publish_lane_branch(self.run_for(), runner=runner)
        self.assertIn("timed out", str(caught.exception))


class VerifyLaneTests(unittest.TestCase):
    """`--verify` exists because a lane's claim about tests is prose, not fact.

    2026-09-17: a lane told to run the suite and paste real output ran it 18
    times, saw JSONDecodeError nine times, committed the failing tests, and
    exited 0. These tests pin the machine-checked half of that story: the
    command runs in the lane worktree, its exit code is the verdict, and a
    hang or a huge log degrades to a failed, tail-truncated result — never
    an exception the caller has to catch.
    """

    def run_for(self) -> worktrees.WorktreeRun:
        return worktrees.WorktreeRun(
            Path("/repo"),
            Path("/repo/.side-lanes/worktrees/task-1"),
            "side-lane/task-1",
            "task",
            "abc123",
        )

    def test_the_command_runs_in_the_lane_worktree_via_the_shell(self) -> None:
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        worktrees.verify_lane(self.run_for(), "make test", runner=runner)
        cmd, kwargs = calls[0]
        self.assertEqual(cmd, "make test")
        self.assertEqual(kwargs["cwd"], str(self.run_for().worktree))
        self.assertTrue(kwargs["shell"])

    def test_a_real_passing_command_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = worktrees.WorktreeRun(
                Path(directory), Path(directory), "side-lane/task-1", "task", "abc123"
            )
            result = worktrees.verify_lane(lane, "echo lane-verify-ok")
        self.assertTrue(result.passed)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.command, "echo lane-verify-ok")
        self.assertIn("lane-verify-ok", result.output)

    def test_a_real_failing_command_fails_with_merged_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lane = worktrees.WorktreeRun(
                Path(directory), Path(directory), "side-lane/task-1", "task", "abc123"
            )
            result = worktrees.verify_lane(lane, "echo boom >&2; exit 9")
        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, 9)
        # The failure text usually lands on stderr; it must reach the output.
        self.assertIn("boom", result.output)

    def test_long_output_is_cut_to_a_labeled_tail(self) -> None:
        def runner(cmd, **_kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, "HEAD-of-log\n" + "x" * 20000 + "\nFAIL: the tail", ""
            )

        result = worktrees.verify_lane(self.run_for(), "make test", runner=runner)
        self.assertIn("FAIL: the tail", result.output)
        self.assertIn("truncated", result.output)  # the cut says it is a cut
        self.assertNotIn("HEAD-of-log", result.output)
        self.assertLess(len(result.output), 5000)

    def test_a_timeout_is_a_failed_result_not_an_exception(self) -> None:
        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        result = worktrees.verify_lane(self.run_for(), "sleep 999", runner=runner)
        self.assertFalse(result.passed)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("timed out", result.output)

    def test_a_timeout_with_partial_output_keeps_it_and_the_note(self) -> None:
        # TimeoutExpired carries bytes even in text mode (bpo-87597).
        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, kwargs.get("timeout", 0), output=b"partial bytes"
            )

        result = worktrees.verify_lane(self.run_for(), "slow-suite", runner=runner)
        self.assertFalse(result.passed)
        self.assertIn("timed out", result.output)
        self.assertIn("partial bytes", result.output)

    def test_the_timeout_is_actually_passed_down(self) -> None:
        calls = []

        def runner(cmd, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        worktrees.verify_lane(self.run_for(), "true", runner=runner)
        self.assertEqual(calls[0]["timeout"], worktrees.VERIFY_TIMEOUT_S)

    def test_an_unstartable_command_is_a_failure_not_a_crash(self) -> None:
        def runner(cmd, **_kwargs):
            raise OSError("no shell")

        result = worktrees.verify_lane(self.run_for(), "make test", runner=runner)
        self.assertFalse(result.passed)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not run verify command", result.output)


class VerifyRunnerHardeningTests(unittest.TestCase):
    """The default verify runner must survive a hostile command, not just a slow one."""

    def run_for(self, worktree: Path) -> worktrees.WorktreeRun:
        return worktrees.WorktreeRun(
            worktree, worktree, "side-lane/task-1", "task", "abc123"
        )

    def temp_worktree(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_huge_output_is_bounded_and_reports_what_it_dropped(self) -> None:
        # 2 MB through the pipe; only the tail may survive, and the note must
        # say so -- the point of streaming is that we never hold the whole log.
        worktree = self.temp_worktree()
        command = (
            "python3 -c \"import sys;[sys.stdout.write('x'*1000+chr(10)) "
            "for _ in range(2000)];sys.stdout.write('TAILMARKER')\""
        )
        result = worktrees.verify_lane(self.run_for(worktree), command, timeout=120)
        self.assertTrue(result.passed)
        self.assertLess(len(result.output), worktrees.VERIFY_OUTPUT_TAIL_CHARS + 200)
        self.assertIn("TAILMARKER", result.output)
        self.assertIn("truncated", result.output)

    def test_undecodable_bytes_do_not_crash_the_runner(self) -> None:
        worktree = self.temp_worktree()
        command = "python3 -c \"import sys;sys.stdout.buffer.write(b'\\xff\\xfe bad bytes')\""
        result = worktrees.verify_lane(self.run_for(worktree), command, timeout=120)
        self.assertTrue(result.passed)
        self.assertIn("bad bytes", result.output)

    def test_a_timeout_kills_descendants_not_just_the_shell(self) -> None:
        # The grandchild writes a marker 3s after its parent shell is killed
        # at 1s. If the timeout only killed the shell, the orphan survives and
        # writes into the worktree we are about to publish.
        worktree = self.temp_worktree()
        marker = worktree / "orphan-survived"
        child = worktree / "child.py"
        child.write_text(
            "import time\n"
            "time.sleep(3)\n"
            "open(" + repr(str(marker)) + ", 'w').write('1')\n",
            encoding="utf-8",
        )
        parent = worktree / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, " + repr(str(child)) + "])\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        result = worktrees.verify_lane(
            self.run_for(worktree), "python3 " + str(parent), timeout=1
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, 124)
        time.sleep(4)
        self.assertFalse(
            marker.exists(),
            "a descendant outlived the timeout and wrote to the worktree",
        )
