import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from side_lane import devin_command_policy as policy


class DevinCommandPolicyTests(unittest.TestCase):
    def test_translates_only_exactly_representable_exec_prefixes(self) -> None:
        self.assertEqual(policy.devin_exec_rule("Bash(git status *)"), "Exec(git status)")
        self.assertEqual(policy.devin_exec_rule("Bash(pwd)"), "Exec(pwd)")
        self.assertIsNone(policy.devin_exec_rule("Bash(./node_modules/.bin/*)"))
        self.assertEqual(policy.devin_exec_deny_rule("Bash(git push --force*)"),
                         "Exec(git push --force)")
        self.assertIsNone(policy.devin_exec_deny_rule("Bash(git push * --force*)"))

    def test_blocks_force_push_in_any_shell_segment(self) -> None:
        allowed_rules = ["Bash(git status *)", "Bash(git push *)", "Bash(env)"]
        rules = ["Bash(git push --force*)", "Bash(git push * --force*)"]
        allowed = {"tool_name": "exec", "tool_input": {"command": "git push origin main"}}
        denied = {"tool_name": "exec",
                  "tool_input": {"command": "git push origin main --force-with-lease"}}
        self.assertIsNone(policy.evaluate_event(allowed, allowed_rules, rules))
        decision = policy.evaluate_event(denied, allowed_rules, rules)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("git push * --force", decision["reason"])

    def test_blocks_commands_outside_grants_and_preserves_exact_rules(self) -> None:
        allowed = ["Bash(env)", "Bash(git status *)"]
        self.assertIsNone(policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {"command": "env"}}, allowed, ()))
        for command in ("env TOKEN=value", "git push origin main", None):
            decision = policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
            self.assertEqual(decision["decision"], "block")

    def test_blocks_shell_composition_and_substitution(self) -> None:
        allowed = ["Bash(git status *)", "Bash(echo *)"]
        commands = (
            "git status --short; git push origin main",
            "git status --short && git push origin main",
            "git status --short | cat",
            "git status --short\ngit push origin main",
            "echo `git status`",
            "echo $(git status)",
            'echo "$(git status)"',
            "echo <(git status)",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
        for command in ("echo 'a; b | c && d'", r"echo a\;b"):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))

    def test_blocks_malformed_or_unexpected_hook_events(self) -> None:
        allowed = ["Bash(git status *)"]
        for payload in (None, {}, {"tool_name": "read"},
                        {"tool_name": "exec"},
                        {"tool_name": "exec", "tool_input": {"command": "echo 'unterminated"}}):
            with self.subTest(payload=payload):
                decision = policy.evaluate_event(payload, allowed, ())
                self.assertEqual(decision["decision"], "block")

    def test_hook_cli_fails_closed_on_bad_policy_and_exits_two_when_blocking(self) -> None:
        payload = {"tool_name": "exec", "tool_input": {"command": "git push --force"}}
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules.json"
            rules.write_text(json.dumps({"allowed": ["Bash(git push *)"],
                                         "denied": ["Bash(git push --force*)"]}))
            stdout = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch("sys.stdout", stdout):
                self.assertEqual(policy.main([str(rules)]), 2)
            self.assertEqual(json.loads(stdout.getvalue())["decision"], "block")
            rules.write_text("{}")
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                self.assertEqual(policy.main([str(rules)]), 2)


if __name__ == "__main__":
    unittest.main()
