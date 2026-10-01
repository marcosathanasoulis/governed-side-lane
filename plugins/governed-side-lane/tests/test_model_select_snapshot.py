from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_model_select_snapshot.py"
SNAPSHOT_PATH = ROOT / "config" / "model-select-snapshot.json"

sys.path.insert(0, str(ROOT / "scripts"))
import build_model_select_snapshot as builder  # noqa: E402


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_fixture_repo(root: Path, commit_label: str = "HEAD") -> Path:
    """A throwaway git repo standing in for the pinned GCF checkout."""

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    capability_screen = root / builder.CAPABILITY_SCREEN_PATH
    _write(
        capability_screen,
        {
            "openrouter_routes": [
                {
                    "route_id": "openrouter:qwen/qwen3-coder",
                    "catalog_tools_supported": True,
                    "catalog_tool_choice_supported": True,
                    "jev_selected_cards": ["F1", "B1"],
                },
                {
                    "route_id": "openrouter:bytedance/ui-tars-1.5-7b",
                    "catalog_tools_supported": False,
                    "catalog_tool_choice_supported": False,
                    "jev_selected_cards": ["F2"],
                },
            ]
        },
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "-m", "fixture"],
        check=True,
    )
    return root


class ScanForLeaksTests(unittest.TestCase):
    def test_clean_value_has_no_problems(self) -> None:
        self.assertEqual(builder.scan_for_leaks({"a": ["fine", 1, None]}), [])

    def test_detects_email(self) -> None:
        problems = builder.scan_for_leaks({"x": "someone@example.com"})
        self.assertTrue(any("email" in problem for problem in problems))

    def test_detects_url(self) -> None:
        problems = builder.scan_for_leaks({"x": "see https://example.com/docs"})
        self.assertTrue(any("URL" in problem for problem in problems))

    def test_detects_long_numeric_id(self) -> None:
        problems = builder.scan_for_leaks({"x": "123456789012"})
        self.assertTrue(any("numeric" in problem for problem in problems))

    def test_allowlists_selector_commit_field(self) -> None:
        problems = builder.scan_for_leaks(
            {"generated_from": {"selector_commit": "ab30e5f274f2c91061ed028eb659600553a74f14"}}
        )
        self.assertEqual(problems, [])

    def test_detects_secret_shaped_token(self) -> None:
        problems = builder.scan_for_leaks({"x": "sk-abcdefghijklmnop"})
        self.assertTrue(any("secret" in problem for problem in problems))

    def test_detects_known_sensitive_substrings(self) -> None:
        problems = builder.scan_for_leaks({"x": "the customer deploy"})
        self.assertTrue(any("customer" in problem for problem in problems))

    def test_extra_denylist_is_merged_in(self) -> None:
        problems = builder.scan_for_leaks(
            {"x": "the acme-internal deploy"}, extra_terms=("acme-internal",)
        )
        self.assertTrue(any("acme-internal" in problem for problem in problems))

    def test_extra_denylist_absent_by_default(self) -> None:
        problems = builder.scan_for_leaks({"x": "the acme-internal deploy"})
        self.assertEqual(problems, [])

    def test_load_extra_denylist_reads_terms_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "denylist.txt"
            path.write_text("# comment\nAcme-Internal\n\nOtherCo\n", encoding="utf-8")
            terms = builder.load_extra_denylist(path)
            self.assertEqual(terms, ("acme-internal", "otherco"))

    def test_load_extra_denylist_none_path_is_empty(self) -> None:
        self.assertEqual(builder.load_extra_denylist(None), ())

    def test_detects_path_like_strings(self) -> None:
        problems = builder.scan_for_leaks({"x": "see docs/claude-tag/model-selection/foo.md"})
        self.assertTrue(any("path" in problem for problem in problems))

    def test_task_family_prose_with_slash_is_not_a_false_positive(self) -> None:
        problems = builder.scan_for_leaks({"x": "read, edit, and test loops"})
        self.assertEqual(problems, [])


class BuildSnapshotTests(unittest.TestCase):
    def test_deterministic_on_fixture_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_fixture_repo(Path(directory) / "gcf")
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE, text=True, check=True,
            ).stdout.strip()

            first = builder.build_snapshot(repo=repo, commit=commit, generated_at="2026-09-30T00:00:00Z")
            second = builder.build_snapshot(repo=repo, commit=commit, generated_at="2026-09-30T00:00:00Z")
            self.assertEqual(first, second)

    def test_fixture_snapshot_has_expected_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_fixture_repo(Path(directory) / "gcf")
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE, text=True, check=True,
            ).stdout.strip()
            snapshot = builder.build_snapshot(repo=repo, commit=commit, generated_at="2026-09-30T00:00:00Z")
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertIn("general_coding_execution", snapshot["task_families"])
            self.assertIn(
                "qwen/qwen3-coder",
                snapshot["task_families"]["general_coding_execution"]["models"],
            )
            self.assertTrue(
                snapshot["task_families"]["general_coding_execution"]["models"]["qwen/qwen3-coder"][
                    "capabilities"
                ]["tools"]
            )
            self.assertFalse(
                snapshot["task_families"]["ui_navigation_repair"]["models"]["bytedance/ui-tars-1.5-7b"][
                    "capabilities"
                ]["tools"]
            )
            # Native hosts are present even without a GCF-sourced receipt row.
            self.assertIn(
                "claude-opus-5",
                snapshot["task_families"]["general_coding_execution"]["models"],
            )
            self.assertEqual(builder.scan_for_leaks(snapshot), [])


class CommittedSnapshotTests(unittest.TestCase):
    def test_committed_snapshot_is_clean_and_valid(self) -> None:
        if not SNAPSHOT_PATH.exists():
            self.skipTest("snapshot not generated in this checkout")
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(builder.scan_for_leaks(data), [])


if __name__ == "__main__":
    unittest.main()
