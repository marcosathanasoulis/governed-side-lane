"""Tests for the deterministic Claude Code Stop hook helper.

The helper is a stdlib-only script inside the trusted runner package. Claude
Code invokes it as a `hooks.Stop` command, so these tests drive it exactly the
way the host will: a JSON ``--settings`` argument carrying the fixed report
path, one JSON object on stdin, and at most one decision object on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from side_lane import report_stop_hook


REPORT_NAME = "SIDE_LANE_REPORT.md"


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
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        decision = self._decision(result.stdout)
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("missing", decision.get("reason", "").lower())

    def test_blank_reports_block(self) -> None:
        for label, content in (("empty", ""), ("whitespace", "   \n\t  "), ("spaces", " " * 9000)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                path.write_text(content, encoding="utf-8")
                result = self._run({"report_path": str(path)}, self._stop_payload())
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_real_report_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text("Findings:\n- observed A\n", encoding="utf-8")
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_only_whitespace_bytes_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_bytes(b"\n\r\t \x0b\x0c")  # every byte whitespace
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_large_report_allows_stop_without_reading_it_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text("Findings\n" + ("x" * 8_000_000), encoding="utf-8")
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_symlink_report_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_file = root / "real.md"
            real_file.write_text("Findings", encoding="utf-8")
            link = root / REPORT_NAME
            link.symlink_to(real_file)
            result = self._run({"report_path": str(link)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_symlink_to_nowhere_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / REPORT_NAME
            link.symlink_to(Path(directory) / "gone.md")
            result = self._run({"report_path": str(link)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_directory_at_report_path_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.mkdir()
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO not supported on this platform")
    def test_fifo_report_does_not_block_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            os.mkfifo(path)
            # Opening a FIFO for content would hang with no writer. The helper
            # must reject a non-regular file from lstat alone.
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")

    def test_hook_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run({"report_path": str(root / REPORT_NAME)}, self._stop_payload(),
                      cwd=root)
            # The blocked stop must not create, truncate, or seed anything.
            self.assertEqual(sorted(child.name for child in root.iterdir()), [])

    # --- bounding the feedback loop ------------------------------------------

    def test_stop_hook_active_true_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            result = self._run({"report_path": str(path)}, self._stop_payload(stop_hook_active=True))
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_non_boolean_stop_hook_active_allows_stop(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                payload = json.dumps({
                    "hook_event_name": "Stop", "cwd": "/tmp", "stop_hook_active": value})
                result = self._run({"report_path": str(path)}, payload)
            self.assertEqual(result.returncode, 0)
            self._no_decision(result)

    def test_non_stop_event_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": "/tmp",
                                  "stop_hook_active": False})
            result = self._run({"report_path": str(path)}, payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    # --- malformed input fails open with a diagnostic -------------------------

    def test_malformed_or_absent_stdin_allows_stop(self) -> None:
        cases = ("not-json", "[]", "", json.dumps({"cwd": "/tmp"}))
        for payload in cases:
            with self.subTest(payload=payload[:12]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / REPORT_NAME
                result = self._run({"report_path": str(path)}, payload)
            self.assertEqual(result.returncode, 0)
            self._no_decision(result)

    def test_oversized_stdin_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            # Well past the bound: a very large non-JSON input must not hang
            # and must produce no decision.
            result = self._run({"report_path": str(path)}, "x" * 2_000_000)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    # --- the real payload is large; the bound is in bytes ---------------------

    def test_large_realistic_payload_blocks_when_report_missing(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload(message_size=20_000, marker=marker)
            self.assertGreater(len(payload.encode("utf-8")), 8_192)
            result = self._run({"report_path": str(path)}, payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._decision(result.stdout).get("decision"), "block")
        self.assertNotIn(marker.encode("utf-8"), result.stdout + result.stderr)

    def test_large_realistic_payload_allows_stop_with_valid_report(self) -> None:
        marker = "SENTINEL-9c3f1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            path.write_text("Findings:\n- observed A\n", encoding="utf-8")
            payload = self._realistic_stop_payload(message_size=20_000, marker=marker)
            result = self._run({"report_path": str(path)}, payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)
        self.assertEqual(result.stderr, b"")

    def test_large_payload_stop_hook_active_still_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload(message_size=20_000, stop_hook_active=True)
            result = self._run({"report_path": str(path)}, payload)
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_stdin_at_exact_bound_still_decides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / REPORT_NAME
            payload = self._realistic_stop_payload_of_size(report_stop_hook.MAX_STDIN_BYTES)
            self.assertEqual(len(payload.encode("utf-8")), report_stop_hook.MAX_STDIN_BYTES)
            result = self._run({"report_path": str(path)}, payload)
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
            result = self._run({"report_path": str(path)}, payload)
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
            result = self._run({"report_path": str(path)}, payload)
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
            result = self._run({"report_path": str(path)}, payload)
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

    # --- the reason is the worker's repair instruction ------------------------

    def test_block_reason_names_the_required_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                {"report_path": str(Path(directory) / REPORT_NAME)}, self._stop_payload())
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
                {"report_path": str(root / REPORT_NAME)},
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
            result = self._run({"report_path": str(path)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)

    def test_report_is_matched_by_name_after_resolution(self) -> None:
        """A relative or aliased path still resolves to the fixed report name."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / REPORT_NAME
            path.write_text("Findings", encoding="utf-8")
            result = self._run({"report_path": str(root / "." / REPORT_NAME)}, self._stop_payload())
        self.assertEqual(result.returncode, 0)
        self._no_decision(result)


class ReportValidityApiTests(unittest.TestCase):
    """The CLI reuses the helper's exact validity rule for its final gate."""

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


if __name__ == "__main__":
    unittest.main()
