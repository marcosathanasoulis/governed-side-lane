"""Tests for the deterministic Claude Code Stop hook helper.

The helper is a stdlib-only script inside the trusted runner package. Claude
Code invokes it as a `hooks.Stop` command, so these tests drive it exactly the
way the host will: a JSON ``--settings`` argument carrying the fixed report
path *and* the run baseline, one JSON object on stdin, and at most one decision
object on stdout.

The baseline is what makes the report this run's: the runner records what the
fixed path already held before the worker starts, and a file identical to that
is not a delivery however plausible it reads.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from side_lane import report_stop_hook, worktrees


REPORT_NAME = "SIDE_LANE_REPORT.md"
FINDINGS = "# Findings\n- item\n"
INHERITED = "# Inherited from a prior task\n\nFindings: plausible and complete.\n"


class ReportStopHookTests(unittest.TestCase):
    def _run(self, settings: object, stdin: str, *, cwd: str | Path | None = None) -> subprocess.CompletedProcess[bytes]:
        helper = Path(report_stop_hook.__file__).resolve()
        settings_text = json.dumps(settings, separators=(",", ":"), sort_keys=True)
        return subprocess.run(
            [sys.executable, str(helper), "--settings", settings_text],
            input=stdin.encode("utf-8"),
            capture_output=True,
            timeout=10,
            cwd=cwd,
        )

    def _armed(self, path: Path | str) -> dict:
        """Settings exactly as the runner arms them: path plus run baseline.

        The capture happens here, which is the runner's prelaunch instant; the
        test may then change the file to represent what the worker did.
        """
        return report_stop_hook.hook_settings(
            report_stop_hook.capture_report_baseline(path))

    def _armed_empty_at_start(self, path: Path | str) -> dict:
        """Settings for a lane whose report path held nothing when it started."""
        return report_stop_hook.hook_settings(
            report_stop_hook.ReportBaseline(Path(path), None))

    def _stop_payload(self, stop_hook_active: bool = False, cwd: str = "/tmp") -> str:
        return json.dumps({
            "hook_event_name": "Stop",
            "cwd": cwd,
            "stop_hook_active": stop_hook_active,
        })

    def _realistic_stop_payload(
        self,
        message_size: int = 0,
        stop_hook_active: bool = False,
        cwd: str = "/tmp",
        *,
        fill: str = "x",
        marker: str = "",
    ) -> str:
        """A Stop event shaped like the real payload, with a sized message.

        Claude Code sends more than the three fields the hook reads; the
        ``last_assistant_message`` in particular can be large, which is why
        the stdin read is bounded.
        """
        return json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": cwd,
                "permission_mode": "acceptEdits",
                "stop_hook_active": stop_hook_active,
                "last_assistant_message": marker + fill * message_size,
            },
            ensure_ascii=False,
        )

    def _realistic_stop_payload_of_size(self, size: int, *, marker: str = "") -> str:
        """A realistic Stop payload serialized to exactly ``size`` UTF-8 bytes."""
        base = len(self._realistic_stop_payload(marker=marker).encode("utf-8"))
        # ASCII fill contributes exactly one serialized byte per character.
        return self._realistic_stop_payload(message_size=size - base, marker=marker)

    def _decision(self, stdout: bytes) -> dict:
        text = stdout.decode("utf-8").strip()
        self.assertTrue(text, "expected one decision object on stdout")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.fail(f"hook stdout is not valid JSON: {text!r}")
        self.assertIsInstance(payload, dict)
        return payload

    def _no_decision(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self.assertEqual(result.stdout.strip(), b"")

    # --- the report is the delivery artifact ---------------------------------

    def test_missing_report_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            result = self._run(self._armed_empty_at_start(path), self._stop_payload())
        self.assertEqual(result.returncode, 0)
        decision = self._decision(result.stdout)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("missing", decision.get("reason", "").lower())

    def test_blank_reports_block(self) -> None:
        for label, content in (("empty", ""), ("whitespace", "   \n\t  "), ("spaces", " " * 9000)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                path.write_text(content, encoding="utf-8")
                result = self._run(self._armed_empty_at_start(path), self._stop_payload())
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_real_report_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            result = self._run(self._armed_empty_at_start(path), self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_report_written_after_the_lane_started_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            settings = self._armed(path)
            path.write_text(FINDINGS, encoding="utf-8")
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_only_whitespace_bytes_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_bytes(b"\n\r\t \x0b\x0c")  # every byte whitespace
            result = self._run(self._armed_empty_at_start(path), self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_large_report_allows_stop_without_reading_it_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text("Findings\n" + ("x" * 8_000_000), encoding="utf-8")
            result = self._run(self._armed_empty_at_start(path), self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    # --- a report that was already there is not this run's delivery -----------

    def test_inherited_report_unchanged_blocks_the_stop(self) -> None:
        """The reported failure: a lane inherits a complete-looking report."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)  # lane start: the file is already there
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        decision = self._decision(result.stdout)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("unchanged", decision.get("reason", "").lower())

    def test_inherited_report_rewritten_by_this_run_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)
            # The worker replaces the inherited file with its own findings.
            path.write_text("# Findings\n- observed A\n", encoding="utf-8")
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_inherited_report_restored_from_git_blocks_the_stop(self) -> None:
        """A worker that resets the tree restores the inherited bytes."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)
            path.write_text(FINDINGS, encoding="utf-8")
            path.write_text(INHERITED, encoding="utf-8")  # git checkout -- .
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_inherited_report_deleted_blocks_the_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)
            path.unlink()
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_inherited_report_amended_by_the_worker_allows_stop(self) -> None:
        """Amending an inherited report is this run's artifact, not stale."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)
            path.write_text(INHERITED + "\n- this run's own finding\n", encoding="utf-8")
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_inherited_report_arms_one_remediation_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = self._armed(path)
            first = self._run(settings, self._stop_payload())
            # The worker was told once; the next stop is allowed so the run
            # cannot loop on the hook.
            second = self._run(settings, self._stop_payload(stop_hook_active=True))
        self.assertEqual(self._decision(first.stdout).get("decision"), "block")
        self._no_decision(second)

    # --- unsafe entries at the report path ------------------------------------

    def test_capture_refuses_a_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.md"
            target.write_text(FINDINGS, encoding="utf-8")
            link = root / REPORT_NAME
            link.symlink_to(target)
            with self.assertRaises(report_stop_hook.UnsafeReportPath) as caught:
                report_stop_hook.capture_report_baseline(link)
            self.assertIn("symlink", str(caught.exception))
            # The link's target was never hashed or rewritten.
            self.assertEqual(target.read_text(encoding="utf-8"), FINDINGS)

    def test_capture_refuses_a_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / REPORT_NAME
            link.symlink_to(root / "gone.md")
            with self.assertRaises(report_stop_hook.UnsafeReportPath):
                report_stop_hook.capture_report_baseline(link)

    def test_capture_refuses_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.mkdir()
            with self.assertRaises(report_stop_hook.UnsafeReportPath) as caught:
                report_stop_hook.capture_report_baseline(path)
        self.assertIn("directory", str(caught.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO not supported on this platform")
    def test_capture_refuses_a_fifo_and_the_hook_never_opens_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            settings = self._armed_empty_at_start(path)
            os.mkfifo(path)
            with self.assertRaises(report_stop_hook.UnsafeReportPath):
                report_stop_hook.capture_report_baseline(path)
            # Armed while the path was empty, the path then became a FIFO:
            # opening it for content would hang with no writer, so the hook
            # must reject it from lstat alone.
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_path_that_becomes_a_symlink_after_the_lane_started_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            settings = self._armed_empty_at_start(path)
            target = root / "elsewhere.md"
            target.write_text(FINDINGS, encoding="utf-8")
            path.symlink_to(target)
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_path_that_becomes_a_directory_after_the_lane_started_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            settings = self._armed_empty_at_start(path)
            path.mkdir()
            result = self._run(settings, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_hook_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(self._armed_empty_at_start(root / REPORT_NAME),
                      self._stop_payload(), cwd=root)
            # The blocked stop must not create, truncate, or seed anything.
            self.assertEqual(sorted(child.name for child in root.iterdir()), [])

    def test_hook_is_read_only_with_an_inherited_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            settings = report_stop_hook.hook_settings(
                report_stop_hook.ReportBaseline(path, report_stop_hook.report_identity(path))
            )
            result = self._run(settings, self._stop_payload(), cwd=root)
            # Only the inherited file is there: the hook preserves nothing and
            # writes nothing, it only reads.
            self.assertEqual(sorted(child.name for child in root.iterdir()), [REPORT_NAME])
            self.assertEqual(path.read_text(encoding="utf-8"), INHERITED)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    # --- bounding the feedback loop ------------------------------------------

    def test_stop_hook_active_true_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            result = self._run(self._armed_empty_at_start(path),
                               self._stop_payload(stop_hook_active=True))
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_non_boolean_stop_hook_active_allows_stop(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                payload = json.dumps({
                    "hook_event_name": "Stop", "cwd": "/tmp", "stop_hook_active": value})
                result = self._run(self._armed_empty_at_start(path), payload)
            self.assertEqual(result.returncode, 0)
            self._no_decision(result)

    def test_non_stop_event_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": "/tmp",
                                  "stop_hook_active": False})
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    # --- malformed input fails open with a diagnostic -------------------------

    def test_malformed_or_absent_stdin_allows_stop(self) -> None:
        cases = ("not-json", "[]", "", json.dumps({"cwd": "/tmp"}))
        for payload in cases:
            with self.subTest(payload=payload[:12]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                result = self._run(self._armed_empty_at_start(path), payload)
            self.assertEqual(result.returncode, 0)
            self._no_decision(result)

    def test_oversized_stdin_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            # Well past the bound: a very large non-JSON input must not hang
            # and must produce no decision.
            result = self._run(self._armed_empty_at_start(path), "x" * 2_000_000)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    # --- the real payload is large; the bound is in bytes ---------------------

    def test_large_realistic_payload_blocks_when_report_missing(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload(message_size=20_000, marker=marker)
            self.assertGreater(len(payload.encode("utf-8")), 8_192)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")
        self.assertNotIn(marker.encode("utf-8"), result.stdout + result.stderr)

    def test_large_realistic_payload_allows_stop_with_valid_report(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            payload = self._realistic_stop_payload(message_size=20_000, marker=marker)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)
        self.assertEqual(result.stderr, b"")

    def test_large_payload_stop_hook_active_still_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload(message_size=20_000, stop_hook_active=True)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_stdin_at_exact_bound_still_decides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload_of_size(report_stop_hook.MAX_STDIN_BYTES)
            self.assertEqual(len(payload.encode("utf-8")), report_stop_hook.MAX_STDIN_BYTES)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_stdin_one_byte_over_bound_is_rejected(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload_of_size(
                report_stop_hook.MAX_STDIN_BYTES + 1, marker=marker)
            self.assertEqual(
                len(payload.encode("utf-8")), report_stop_hook.MAX_STDIN_BYTES + 1)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)
        self.assertIn(b"stdin exceeds the size bound", result.stderr)
        self.assertNotIn(marker.encode("utf-8"), result.stdout + result.stderr)

    def test_multibyte_payload_under_byte_bound_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            # "é" is one character but two UTF-8 bytes: the serialized payload
            # stays comfortably under the bound and must decide normally.
            payload = self._realistic_stop_payload(message_size=10_000, fill="é")
            self.assertLess(len(payload.encode("utf-8")), report_stop_hook.MAX_STDIN_BYTES)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_multibyte_payload_over_byte_bound_is_rejected(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            # 600_000 characters is under the bound counted in characters but
            # over it counted in bytes; the bound must be measured in bytes.
            payload = self._realistic_stop_payload(
                message_size=600_000, fill="é", marker=marker)
            self.assertGreater(
                len(payload.encode("utf-8")), report_stop_hook.MAX_STDIN_BYTES)
            result = self._run(self._armed_empty_at_start(path), payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)
        self.assertIn(b"stdin exceeds the size bound", result.stderr)
        self.assertNotIn(marker.encode("utf-8"), result.stdout + result.stderr)

    def test_missing_or_malformed_settings_fails_open(self) -> None:
        helper = Path(report_stop_hook.__file__).resolve()
        commands = (
            [sys.executable, str(helper)],
            [sys.executable, str(helper), "--settings", "{"],
            [sys.executable, str(helper), "--settings", "[]"],
            [sys.executable, str(helper), "--settings", "{}"],
            [sys.executable, str(helper), "--settings",
             json.dumps({"report_path": "/tmp/other-report.md"})],
        )
        for command in commands:
            with self.subTest(argv=command[2:4]):
                result = subprocess.run(
                    command, input=self._stop_payload().encode("utf-8"),
                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self._no_decision(result)
            # Never invent a path and never echo the payload back.
            self.assertNotIn(b"hook_event_name", result.stdout + result.stderr)

    def test_settings_without_a_run_baseline_fail_open_with_a_diagnostic(self) -> None:
        """A payload from a runner that armed no baseline cannot be trusted."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)
        self.assertIn(b"no run baseline", result.stderr)

    def test_malformed_run_baseline_fails_open_with_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            for identity in ("not-a-hash", "", 7, {"a": 1}, "sha256:zz:size:1"):
                with self.subTest(identity=identity):
                    settings = {
                        "report_path": str(path),
                        report_stop_hook.FRESHNESS_KEY: {
                            report_stop_hook.BASELINE_IDENTITY_KEY: identity},
                    }
                    result = self._run(settings, self._stop_payload())
                self.assertEqual(result.returncode, 0)
                self._no_decision(result)
                self.assertIn(b"malformed run baseline", result.stderr)

    # --- the reason is the worker's repair instruction ------------------------

    def test_block_reason_names_the_required_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                self._armed_empty_at_start(Path(directory) / REPORT_NAME),
                self._stop_payload())
        reason = self._decision(result.stdout).get("reason", "").lower()
        for expected in ("report", "findings", "screenshot", "git", "prose"):
            self.assertIn(expected, reason)

    # --- path handling --------------------------------------------------------

    def test_cwd_in_payload_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / REPORT_NAME).write_text("decoy", encoding="utf-8")
            result = self._run(
                self._armed_empty_at_start(root / REPORT_NAME),
                self._stop_payload(cwd=str(elsewhere)),
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_path_with_spaces_and_quotes_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'dir with "quotes" and spaces'
            root.mkdir()
            path = root / REPORT_NAME
            path.write_text("Findings", encoding="utf-8")
            result = self._run(self._armed_empty_at_start(path), self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_report_is_matched_by_name_after_resolution(self) -> None:
        """A relative or aliased path still resolves to the fixed report name."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text("Findings", encoding="utf-8")
            result = self._run(self._armed_empty_at_start(root / "." / REPORT_NAME),
                               self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)


class ReportFreshnessContractTests(unittest.TestCase):
    """The run-bound baseline the hook and the runner's acceptance share."""

    def _quiet(self):
        """Swallow the helper's own diagnostics where they are expected."""
        return contextlib.redirect_stderr(io.StringIO())

    def test_absent_path_has_no_identity_and_preserves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = report_stop_hook.capture_report_baseline(root / REPORT_NAME)
            # Nothing was there, so nothing is created by the capture either.
            self.assertEqual(sorted(child.name for child in root.iterdir()), [])
        self.assertIsNone(baseline.identity)
        self.assertIsNone(baseline.preserved_path)
        self.assertFalse(baseline.preexisting)

    def test_inherited_report_is_identified_and_preserved_not_moved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            before = path.stat()
            baseline = report_stop_hook.capture_report_baseline(path)
            after = path.stat()
            self.assertEqual(baseline.identity, report_stop_hook.report_identity(path))
            self.assertTrue(baseline.preexisting)
            self.assertIsNotNone(baseline.preserved_path)
            preserved = Path(str(baseline.preserved_path))
            self.assertTrue(preserved.is_file())
            self.assertEqual(preserved.read_text(encoding="utf-8"), INHERITED)
            # The lane's own copy is untouched: identical content, same inode.
            self.assertEqual(path.read_text(encoding="utf-8"), INHERITED)
            self.assertEqual((after.st_ino, after.st_size), (before.st_ino, before.st_size))
            # The preserved copy lives in the lane's ignored scratch.
            self.assertEqual(
                preserved.parent.parent, root / report_stop_hook.SCRATCH_DIR_NAME)

    def test_preservation_rejects_symlinks_without_external_writes(self) -> None:
        for component in ("scratch", "child", "destination"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "lane"
                outside = Path(directory) / "outside"
                root.mkdir()
                outside.mkdir()
                victim = outside / "keep.txt"
                victim.write_text("unchanged", encoding="utf-8")
                path = root / REPORT_NAME
                path.write_text(INHERITED, encoding="utf-8")
                scratch = root / report_stop_hook.SCRATCH_DIR_NAME
                child = scratch / report_stop_hook.PRESERVED_DIR_NAME
                if component == "scratch":
                    scratch.symlink_to(outside, target_is_directory=True)
                elif component == "child":
                    scratch.mkdir()
                    child.symlink_to(outside, target_is_directory=True)
                else:
                    child.mkdir(parents=True)
                    (child / REPORT_NAME).symlink_to(victim)
                with self._quiet():
                    baseline = report_stop_hook.capture_report_baseline(path)
                self.assertIsNone(baseline.preserved_path)
                self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")
                self.assertEqual(list(outside.iterdir()), [victim])
                self.assertFalse(report_stop_hook.report_is_current(baseline))

    def test_preserved_copy_keeps_the_earliest_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text(INHERITED, encoding="utf-8")
            first = report_stop_hook.capture_report_baseline(path)
            path.write_text("# Findings\n- newer\n", encoding="utf-8")
            second = report_stop_hook.capture_report_baseline(path)
            self.assertEqual(first.preserved_path, second.preserved_path)
            content = Path(str(first.preserved_path)).read_text(encoding="utf-8")
        self.assertEqual(content, INHERITED)

    def test_identity_distinguishes_content_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            same = report_stop_hook.report_identity(path)
            path.write_text(FINDINGS, encoding="utf-8")
            self.assertEqual(report_stop_hook.report_identity(path), same)
            path.write_text(FINDINGS + " ", encoding="utf-8")
            self.assertNotEqual(report_stop_hook.report_identity(path), same)
            path.write_text("# Findings\n- item\n".replace("item", "thing"), encoding="utf-8")
            self.assertNotEqual(report_stop_hook.report_identity(path), same)

    def test_empty_file_has_an_identity_but_is_not_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text("", encoding="utf-8")
            self.assertIsNotNone(report_stop_hook.report_identity(path))
            self.assertFalse(report_stop_hook.report_is_valid(path))

    def test_identity_carries_the_full_size_beside_the_hashed_prefix(self) -> None:
        """Only a bounded prefix is hashed, so the size must be part of it."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            identity = report_stop_hook.report_identity(path)
            self.assertRegex(
                identity, report_stop_hook.IDENTITY_PATTERN)
            self.assertTrue(identity.endswith(f":size:{path.stat().st_size}"))

    def test_state_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            self.assertEqual(
                report_stop_hook.report_freshness_state(None), "unverified")
            absent = report_stop_hook.capture_report_baseline(path)
            self.assertEqual(
                report_stop_hook.report_freshness_state(absent), "unusable")
            path.write_text(FINDINGS, encoding="utf-8")
            self.assertEqual(
                report_stop_hook.report_freshness_state(absent), "current")
            path.write_text(INHERITED, encoding="utf-8")
            inherited = report_stop_hook.capture_report_baseline(path)
            self.assertEqual(
                report_stop_hook.report_freshness_state(inherited), "stale")
            path.write_text(" \n\t", encoding="utf-8")
            self.assertEqual(
                report_stop_hook.report_freshness_state(inherited), "unusable")
            path.write_text(FINDINGS, encoding="utf-8")
            self.assertEqual(
                report_stop_hook.report_freshness_state(inherited), "current")

    def test_no_baseline_is_never_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            self.assertFalse(report_stop_hook.report_is_current(None))

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text(FINDINGS, encoding="utf-8")
            baseline = report_stop_hook.capture_report_baseline(path)
            settings = report_stop_hook.hook_settings(baseline)
            back = report_stop_hook.baseline_from_settings(settings)
            self.assertIsNotNone(back)
            self.assertEqual(back.report_path, baseline.report_path)
            self.assertEqual(back.identity, baseline.identity)
            empty = report_stop_hook.ReportBaseline(path, None)
            back = report_stop_hook.baseline_from_settings(
                report_stop_hook.hook_settings(empty))
            self.assertIsNone(back.identity)

    def test_settings_without_the_freshness_block_have_no_baseline(self) -> None:
        with self._quiet():
            self.assertIsNone(report_stop_hook.baseline_from_settings(
                {"report_path": "/tmp/" + REPORT_NAME}))

    def test_settings_identity_must_be_a_real_identity(self) -> None:
        for identity in ("nope", 7, "", "sha256:x:size:1", "sha256:" + "a" * 63):
            with self.subTest(identity=identity), self._quiet():
                self.assertIsNone(report_stop_hook.baseline_from_settings({
                    "report_path": "/tmp/" + REPORT_NAME,
                    report_stop_hook.FRESHNESS_KEY: {
                        report_stop_hook.BASELINE_IDENTITY_KEY: identity},
                }))

    def test_scratch_directory_name_matches_the_worktree_module(self) -> None:
        """This module cannot import the package, so a test pins the literal."""
        self.assertEqual(report_stop_hook.SCRATCH_DIR_NAME, worktrees.SCRATCH_DIR_NAME)


class ReportValidityApiTests(unittest.TestCase):
    """The artifact-type half of the rule the two gates share."""

    def test_report_is_valid_matches_the_helper_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / REPORT_NAME
            self.assertFalse(report_stop_hook.report_is_valid(missing))
            missing.write_text("", encoding="utf-8")
            self.assertFalse(report_stop_hook.report_is_valid(missing))
            missing.write_text("   \n", encoding="utf-8")
            self.assertFalse(report_stop_hook.report_is_valid(missing))
            missing.write_text("# Findings\n- item\n", encoding="utf-8")
            self.assertTrue(report_stop_hook.report_is_valid(missing))
            real = root / "real.md"
            real.write_text("Findings", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(real)
            self.assertFalse(report_stop_hook.report_is_valid(link))

    def test_report_is_valid_fails_closed_on_unreadable_path_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory) / REPORT_NAME
            directory_path.mkdir()
            self.assertFalse(report_stop_hook.report_is_valid(directory_path))

    def test_report_identity_refuses_what_report_is_valid_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory) / REPORT_NAME
            directory_path.mkdir()
            with self.assertRaises(report_stop_hook.UnsafeReportPath):
                report_stop_hook.report_identity(directory_path)


if __name__ == "__main__":
    unittest.main()
