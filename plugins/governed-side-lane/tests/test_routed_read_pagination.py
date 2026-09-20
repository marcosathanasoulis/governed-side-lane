from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from side_lane import routed_read_pagination as hook


class RoutedReadPaginationTests(unittest.TestCase):
    def run_hook(self, payload: object, config: object = None,
                 *, config_text: str | None = None,
                 stdin_text: str | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "routed-read-pagination.json"
            if config_text is not None:
                config_path.write_text(config_text, encoding="utf-8")
            else:
                config_path.write_text(json.dumps(config or {}), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(
                    stdin_text if stdin_text is not None else json.dumps(payload))), \
                    mock.patch("sys.stdout", stdout):
                exit_code = hook.main([str(config_path)])
        return exit_code, stdout.getvalue()

    def emitted(self, payload: object, config: object = None,
                *, config_text: str | None = None) -> dict:
        exit_code, stdout = self.run_hook(
            payload, config, config_text=config_text)
        self.assertEqual(exit_code, 0)
        self.assertTrue(stdout.strip())
        return json.loads(stdout)

    def test_read_without_limit_is_bounded_and_preserves_input(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {
            "file_path": "/lane/docs/big.md", "offset": 5}}
        output = self.emitted(payload)
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["updatedInput"],
                         {"file_path": "/lane/docs/big.md", "offset": 5,
                          "limit": hook.DEFAULT_LINE_BOUND})

    def test_read_with_oversized_limit_is_clamped(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py", "offset": 40, "limit": 5000,
            "extra": "kept"}}
        output = self.emitted(payload)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["limit"], hook.DEFAULT_LINE_BOUND)
        self.assertEqual(updated["offset"], 40)
        self.assertEqual(updated["extra"], "kept")

    def test_read_with_small_or_exact_limit_is_untouched(self) -> None:
        for limit in (1, 50, hook.DEFAULT_LINE_BOUND):
            with self.subTest(limit=limit):
                exit_code, stdout = self.run_hook(
                    {"tool_name": "Read", "tool_input": {
                        "file_path": "/lane/big.py", "limit": limit}})
                self.assertEqual(exit_code, 0)
                self.assertEqual(stdout, "")

    def test_rewrite_never_reports_a_permission_decision(self) -> None:
        output = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py"}})
        self.assertNotIn("decision", output)
        self.assertNotIn("reason", output)
        specific = output["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", specific)
        self.assertNotIn("allow", specific)
        self.assertNotIn("deny", specific)

    def test_additional_context_names_the_next_range(self) -> None:
        output = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py"}})
        context = output["hookSpecificOutput"]["additionalContext"]
        # Continuation arguments and a line count only: an inclusive
        # first-to-last range would be off by one under one of the two
        # offset conventions.
        self.assertIn("offset=1 limit=200", context)
        self.assertIn("offset=201 limit=200", context)
        self.assertIn("200 lines", context)
        self.assertNotIn("1-200", context)
        self.assertNotIn("bytes", context)
        self.assertIn("stays reachable", context)

    def test_repeated_calls_chain_through_the_whole_file(self) -> None:
        # Each bounded call names the next offset, and a follow-up call at
        # that offset is itself bounded — the remainder stays reachable.
        first = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py"}})
        second = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py", "offset": 201}})
        third = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py", "offset": 401, "limit": 900}})
        self.assertEqual(
            first["hookSpecificOutput"]["updatedInput"]["limit"], 200)
        self.assertIn("offset=201",
                      first["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(
            second["hookSpecificOutput"]["updatedInput"],
            {"file_path": "/lane/big.py", "offset": 201, "limit": 200})
        self.assertIn("offset=401",
                      second["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(
            third["hookSpecificOutput"]["updatedInput"]["limit"], 200)
        self.assertIn("offset=601",
                      third["hookSpecificOutput"]["additionalContext"])

    def test_non_text_read_paths_are_untouched(self) -> None:
        for path in ("/lane/paper.pdf", "/lane/diagram.png",
                     "/lane/photo.JPEG", "/lane/notebook.ipynb"):
            for tool_input in ({"file_path": path},
                               {"file_path": path, "limit": 99999}):
                with self.subTest(tool_input=tool_input):
                    exit_code, stdout = self.run_hook(
                        {"tool_name": "Read", "tool_input": tool_input})
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(stdout, "")

    def test_non_read_tool_is_untouched(self) -> None:
        for name in ("Bash", "Write", "read"):
            with self.subTest(name=name):
                exit_code, stdout = self.run_hook(
                    {"tool_name": name, "tool_input": {
                        "file_path": "/lane/big.py"}})
                self.assertEqual(exit_code, 0)
                self.assertEqual(stdout, "")

    def test_configured_bound_overrides_the_default(self) -> None:
        output = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py"}}, config={"limit": 40})
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["updatedInput"]["limit"], 40)
        self.assertIn("offset=41", specific["additionalContext"])

    def test_malformed_input_exits_zero_silently(self) -> None:
        for stdin_text in ("not json{", "", "42", '"text"'):
            with self.subTest(stdin_text=stdin_text):
                exit_code, stdout = self.run_hook(None, stdin_text=stdin_text)
                self.assertEqual(exit_code, 0)
                self.assertEqual(stdout, "")
        for payload in ({"tool_name": "Read"},
                        {"tool_name": "Read", "tool_input": "oops"},
                        {"tool_input": {"file_path": "/lane/big.py"}}):
            with self.subTest(payload=payload):
                exit_code, stdout = self.run_hook(payload)
                self.assertEqual(exit_code, 0)
                self.assertEqual(stdout, "")

    def test_missing_or_invalid_config_falls_back_to_default_bound(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py"}}
        for config_text in ("not json{", '{"limit": 0}', '["list"]',
                            '{"limit": "many"}'):
            with self.subTest(config_text=config_text):
                output = self.emitted(payload, config_text=config_text)
                self.assertEqual(
                    output["hookSpecificOutput"]["updatedInput"]["limit"],
                    hook.DEFAULT_LINE_BOUND)
        stdout = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", stdout):
            exit_code = hook.main(["/nonexistent/pagination-config.json"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["hookSpecificOutput"]
            ["updatedInput"]["limit"], hook.DEFAULT_LINE_BOUND)

    def test_malformed_tool_input_leaves_read_unchanged(self) -> None:
        # The tool's own input validation owns these; the hook must emit
        # nothing and exit 0 so it never interferes with that or with
        # permission checking. A non-finite offset must never reach int().
        for label, tool_input in (
                ("nan_offset", {"file_path": "/lane/big.py",
                                "offset": float("nan")}),
                ("inf_offset", {"file_path": "/lane/big.py",
                                "offset": float("inf")}),
                ("negative_inf_offset", {"file_path": "/lane/big.py",
                                         "offset": float("-inf")}),
                ("negative_offset", {"file_path": "/lane/big.py",
                                     "offset": -5}),
                ("fractional_offset", {"file_path": "/lane/big.py",
                                       "offset": 3.5}),
                ("string_offset", {"file_path": "/lane/big.py",
                                   "offset": "10"}),
                ("boolean_offset", {"file_path": "/lane/big.py",
                                    "offset": True}),
                ("missing_file_path", {"offset": 1}),
                ("empty_file_path", {"file_path": ""}),
                ("none_file_path", {"file_path": None}),
                ("non_string_file_path", {"file_path": 17}),
                ("zero_limit", {"file_path": "/lane/big.py", "limit": 0}),
                ("negative_limit", {"file_path": "/lane/big.py",
                                    "limit": -300}),
                ("string_limit", {"file_path": "/lane/big.py",
                                  "limit": "900"}),
                ("boolean_limit", {"file_path": "/lane/big.py",
                                   "limit": True}),
                ("fractional_limit", {"file_path": "/lane/big.py",
                                      "limit": 900.5}),
                ("nan_limit", {"file_path": "/lane/big.py",
                               "limit": float("nan")}),
                ("inf_limit", {"file_path": "/lane/big.py",
                               "limit": float("inf")}),
        ):
            with self.subTest(label):
                exit_code, stdout = self.run_hook(
                    {"tool_name": "Read", "tool_input": tool_input})
                self.assertEqual(exit_code, 0)
                self.assertEqual(stdout, "")

    def test_integral_float_offset_and_limit_are_normalised(self) -> None:
        output = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py", "offset": 401.0, "limit": 900.0}})
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["limit"], hook.DEFAULT_LINE_BOUND)
        self.assertEqual(updated["offset"], 401)
        self.assertIsInstance(updated["offset"], int)
        self.assertNotIsInstance(updated["offset"], float)
        self.assertIn(
            "offset=601", output["hookSpecificOutput"]["additionalContext"])

    def test_zero_offset_is_valid_and_preserved(self) -> None:
        output = self.emitted({"tool_name": "Read", "tool_input": {
            "file_path": "/lane/big.py", "offset": 0}})
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["updatedInput"]["offset"], 0)
        self.assertIn("offset=200 limit=200", specific["additionalContext"])

    def test_evaluate_event_returns_none_without_rewrite(self) -> None:
        self.assertIsNone(hook.evaluate_event(None))
        self.assertIsNone(hook.evaluate_event(
            {"tool_name": "Read", "tool_input": {
                "file_path": "/lane/big.py", "limit": 10}}))
        output = hook.evaluate_event(
            {"tool_name": "Read", "tool_input": {"file_path": "/lane/big.py"}},
            bound=25)
        self.assertEqual(
            output["hookSpecificOutput"]["updatedInput"]["limit"], 25)


if __name__ == "__main__":
    unittest.main()
