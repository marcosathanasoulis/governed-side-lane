from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

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
        )
        worktrees.dispose_clean_worktree(lane)
        self.assertFalse(lane.worktree.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["stdout"], "finding: bug in api.py")
        self.assertEqual(payload["stderr"], "warn")

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

    def test_nested_side_lanes_directory_still_counts_as_dirty(self) -> None:
        repo = self.make_repo()
        worktrees.create_worktree(repo, "first")
        nested = repo / "src" / ".side-lanes"
        nested.mkdir(parents=True)
        (nested / "file").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(worktrees.WorktreeError, "dirty"):
            worktrees.create_worktree(repo, "second")


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
