import io
import json
from pathlib import Path
import re
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

    def test_bare_command_matches_trailing_wildcard_grant(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git worktree list *)", "Bash(git log *)"]
        for command in ("git status", "git worktree list", "  git   status  ",
                        "git log --oneline -10", "git log -1 --format=%H"):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))
        for command in ("git statusx", "git push", "git push origin main",
                        "git worktree", "git worktree add ../x"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertEqual(decision["reason"],
                                 "command is outside canonical capability grants")
        self.assertEqual(policy.matching_rule("git status", allowed), "Bash(git status *)")
        self.assertIsNone(policy.bare_rule_pattern("git push --force*"))
        self.assertIsNone(policy.bare_rule_pattern("./node_modules/.bin/*"))

    def test_bare_command_deny_rules_still_match_anywhere(self) -> None:
        allowed = ["Bash(git *)"]
        denied = ["Bash(git push --force*)", "Bash(git reset --hard *)"]
        for command in ("git push --force", "git push --force origin main",
                        "git reset --hard", "git reset --hard HEAD~1"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, denied)
                self.assertEqual(decision["decision"], "block")
                self.assertIn("command denied by canonical rule", decision["reason"])
        self.assertIsNone(policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {"command": "git reset --soft HEAD~1"}},
            allowed, denied))

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
        self.assertEqual(policy.evaluate_event({"tool_name": "read"}, allowed, ())["reason"],
                         "invalid hook event")

    def test_policy_hook_matcher_lists_exactly_the_covered_tools(self) -> None:
        self.assertEqual(policy.policy_hook_matcher(), "^(exec|write|edit|str_replace)$")
        matcher = re.compile(policy.policy_hook_matcher())
        for tool in policy.COVERED_TOOL_NAMES:
            self.assertIsNotNone(matcher.match(tool), tool)
        for tool in ("read", "grep", "glob", "exec-other", "mcp__github__create_issue"):
            self.assertIsNone(matcher.match(tool), tool)

    def test_write_tools_are_contained_to_the_lane_worktree(self) -> None:
        allowed = ["Bash(env)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "src").mkdir(parents=True)
            for tool in policy.FILE_MUTATING_TOOL_NAMES:
                with self.subTest(tool=tool):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": tool, "tool_input": {"file_path": str(worktree / "src" / "new.py")}},
                        allowed, (), worktree=str(worktree)))
                    decision = policy.evaluate_event(
                        {"tool_name": tool, "tool_input": {"file_path": "/tmp/pr1678_sim.py"}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertEqual(decision["reason"],
                                     "writes outside the lane worktree are not permitted; "
                                     f"use {worktree}/.side-lane-scratch/ for scratch files")
                    scratch = policy.evaluate_event(
                        {"tool_name": tool,
                         "tool_input": {"file_path": str(worktree / ".side-lane-scratch" / "sim.py")}},
                        allowed, (), worktree=str(worktree))
                    self.assertIsNone(scratch)
            # Relative targets resolve against the lane worktree, not the host cwd.
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "write", "tool_input": {"file_path": "notes.md"}},
                allowed, (), worktree=str(worktree)))
            # A symlink inside the worktree that escapes it resolves outside and blocks.
            escape = worktree / "escape-link"
            escape.symlink_to(Path("/tmp"))
            decision = policy.evaluate_event(
                {"tool_name": "write", "tool_input": {"file_path": str(escape / "x.py")}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("outside the lane worktree", decision["reason"])

    def test_write_tools_fail_closed_without_worktree_or_target(self) -> None:
        allowed = ["Bash(env)"]
        decision = policy.evaluate_event(
            {"tool_name": "write", "tool_input": {"file_path": "/tmp/x.py"}}, allowed, ())
        self.assertEqual(decision["decision"], "block")
        self.assertIn("lane worktree", decision["reason"])
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            for tool_input in ({}, {"file_path": ""}, {"file_path": "  "},
                               {"command": "touch x"}, "not-a-dict"):
                with self.subTest(tool_input=tool_input):
                    decision = policy.evaluate_event(
                        {"tool_name": "edit", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("missing", decision["reason"])

    def test_git_dash_c_inside_worktree_matches_the_bare_grant(self) -> None:
        allowed = ["Bash(git log *)", "Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "pkg").mkdir(parents=True)
            for command in (f"git -C {worktree} log --oneline -5",
                            f"git -C '{worktree}' log -1",
                            f"git -C {worktree} status --short",
                            f"git -C {worktree}/pkg status --short",
                            "git -C . status"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))
            for command in (f"git -C {worktree.parent} log --oneline -5",
                            "git -C /etc status --short",
                            f"git -C {worktree} push origin main",
                            f"git -C {worktree} log; touch /tmp/x"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
            # Deny rules still match through the stripped prefix.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": f"git -C {worktree} push --force origin main"}},
                ["Bash(git push *)"], ["Bash(git push --force*)"], worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("command denied by canonical rule", decision["reason"])

    def test_dash_c_shell_expandable_targets_fail_closed(self) -> None:
        # A literal $HOME/~/$(pwd) token must never be treated as an in-worktree
        # path here: the shell (or a re-parsing provider) expands it later to
        # something outside the worktree, so the command must be evaluated
        # as-is (and fail to match a bare grant) rather than stripped.
        allowed = ["Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            # $HOME/~ carry no shell-composition syntax, so they reach the -C
            # normalisation and must fail the (unstripped) canonical match.
            for command in ('git -C "$HOME" status', "git -C ~ status", "git -C ~/x status"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertEqual(decision["reason"],
                                     "command is outside canonical capability grants")
            # $(...) / backticks are already rejected as unsafe shell syntax
            # before the -C normalisation runs at all; still a fail-closed block.
            for command in ('git -C "$(pwd)" status', "git -C `pwd` status"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

    def test_write_targets_with_shell_expansion_fail_closed(self) -> None:
        allowed = ["Bash(env)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            for target in ("$HOME/x.py", "~/x.py", "~", "$(pwd)/x.py"):
                with self.subTest(target=target):
                    decision = policy.evaluate_event(
                        {"tool_name": "write", "tool_input": {"file_path": target}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("outside the lane worktree", decision["reason"])

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

    def test_hook_cli_reads_worktree_from_rules_for_write_containment(self) -> None:
        payload = {"tool_name": "write", "tool_input": {"file_path": "/tmp/pr1678_sim.py"}}
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            rules = Path(directory) / "rules.json"
            rules.write_text(json.dumps({"allowed": ["Bash(env)"], "denied": [],
                                         "worktree": str(worktree)}))
            stdout = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch("sys.stdout", stdout):
                self.assertEqual(policy.main([str(rules)]), 2)
            self.assertIn(".side-lane-scratch/", json.loads(stdout.getvalue())["reason"])
            for invalid_worktree in ("", "   ", 7, None):
                rules.write_text(json.dumps({"allowed": ["Bash(env)"], "denied": [],
                                             "worktree": invalid_worktree}))
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                    self.assertEqual(policy.main([str(rules)]), 2)


if __name__ == "__main__":
    unittest.main()
