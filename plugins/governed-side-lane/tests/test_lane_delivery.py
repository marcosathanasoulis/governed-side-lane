"""A lane's exit status is not evidence its work reached git.

2026-09-17: three dispatches in one session each exited 0 having delivered
nothing. Two were the runner correctly refusing (bad flags, dirty checkout),
misread through a shell pipe; the third was a worker that wrote both of its
files correctly, exited 0, and left them untracked. Only the third is the
tool's to catch, and it is the dangerous one: the files look right, the run
looks green, and the work disappears with the worktree.

unittest.TestCase, not bare pytest functions: CI runs
`python -m unittest discover -s tests`, which does not collect module-level
test functions. A first revision of this file wrote them that way, so none of
these checks would have run in the required lane — a check that does not run,
which is the exact failure the file exists to prevent.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from side_lane.worktrees import LaneDelivery, WorktreeRun, lane_delivery


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()


class LaneDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "seed.txt").write_text("seed\n")
        _git(self.repo, "add", "seed.txt")
        _git(self.repo, "commit", "-qm", "seed")
        self.start = _git(self.repo, "rev-parse", "HEAD")

    def run_(self) -> WorktreeRun:
        return WorktreeRun(self.repo, self.repo, "side-lane/test", "test", self.start)

    def test_committed_and_clean_is_delivered(self):
        (self.repo / "new.py").write_text("x = 1\n")
        _git(self.repo, "add", "new.py")
        _git(self.repo, "commit", "-qm", "work")

        delivery = lane_delivery(self.run_())

        self.assertTrue(delivery.committed)
        self.assertEqual(delivery.uncommitted, ())
        self.assertTrue(delivery.delivered)
        self.assertIsNone(delivery.failure_reason())

    def test_wrote_files_but_committed_nothing(self):
        """The shape observed twice on 2026-09-17: correct files, untracked."""
        (self.repo / "pool_lease.py").write_text("# real work\n")
        (self.repo / "test_pool_lease.py").write_text("# real tests\n")

        delivery = lane_delivery(self.run_())

        self.assertFalse(delivery.committed)
        self.assertFalse(delivery.delivered)
        self.assertEqual(
            set(delivery.uncommitted), {"pool_lease.py", "test_pool_lease.py"}
        )
        reason = delivery.failure_reason()
        self.assertIn("committed nothing", reason)
        # Naming the files is what makes the message actionable.
        self.assertIn("pool_lease.py", reason)

    def test_committed_but_left_work_behind(self):
        """Partial delivery is still a loss: the uncommitted half vanishes."""
        (self.repo / "done.py").write_text("done\n")
        _git(self.repo, "add", "done.py")
        _git(self.repo, "commit", "-qm", "half")
        (self.repo / "forgotten.py").write_text("forgotten\n")

        delivery = lane_delivery(self.run_())

        self.assertTrue(delivery.committed)
        self.assertFalse(delivery.delivered)
        self.assertIn("forgotten.py", delivery.failure_reason())
        self.assertIn("left these uncommitted", delivery.failure_reason())

    def test_changed_nothing_at_all(self):
        """The silent no-op: exit 0, clean tree, no commit."""
        delivery = lane_delivery(self.run_())

        self.assertFalse(delivery.delivered)
        reason = delivery.failure_reason()
        self.assertIn("no commit and no file changes", reason)
        self.assertIn("stderr", reason)  # point at where the cause actually is

    def test_modified_tracked_file_counts_as_uncommitted(self):
        """Not only untracked files — an edit to a tracked file is just as lost.

        This case caught a real defect: the first parser used a fixed
        `line[3:]` slice, but `_git` strips its output, so an unstaged
        ` M seed.txt` lost its leading space and the slice reported
        'eed.txt' — sending an operator after a file that does not exist.
        """
        (self.repo / "seed.txt").write_text("edited\n")

        delivery = lane_delivery(self.run_())

        self.assertFalse(delivery.delivered)
        self.assertIn("seed.txt", delivery.uncommitted)

    def test_untracked_path_containing_an_arrow_is_not_read_as_a_rename(self):
        """A file legitimately named `a -> b.txt` must be reported whole.

        The second parser split path text on ' -> ' to find renames, so this
        file was reported as 'b.txt'. Renames are structural in `-z` porcelain
        and are consumed by position instead.
        """
        (self.repo / "a -> b.txt").write_text("tricky\n")

        delivery = lane_delivery(self.run_())

        self.assertIn("a -> b.txt", delivery.uncommitted)
        self.assertNotIn("b.txt", delivery.uncommitted)

    def test_staged_rename_reports_the_destination_once(self):
        _git(self.repo, "mv", "seed.txt", "renamed.txt")

        delivery = lane_delivery(self.run_())

        self.assertIn("renamed.txt", delivery.uncommitted)
        # The original path rides in its own field and must not be counted.
        self.assertNotIn("seed.txt", delivery.uncommitted)

    def test_long_file_lists_are_truncated_with_a_count(self):
        """A large lane must not bury the message it is trying to deliver."""
        for i in range(30):
            (self.repo / f"f{i:03d}.py").write_text("x\n")

        reason = lane_delivery(self.run_()).failure_reason()

        self.assertIn("... and 10 more", reason)

    def test_delivered_requires_both_conditions(self):
        self.assertTrue(LaneDelivery(committed=True, uncommitted=()).delivered)
        self.assertFalse(LaneDelivery(committed=True, uncommitted=("a",)).delivered)
        self.assertFalse(LaneDelivery(committed=False, uncommitted=()).delivered)
        self.assertFalse(LaneDelivery(committed=False, uncommitted=("a",)).delivered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
