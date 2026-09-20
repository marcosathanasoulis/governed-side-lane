import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import venv

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

    def test_allows_compound_commands_when_every_component_is_allowed(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)",
                   "Bash(cat *)", "Bash(echo *)"]
        commands = (
            "git status && git diff",
            "git status || git diff",
            "git status | cat",
            "git status; git diff",
            "git status\ngit diff",
            "git status && echo 'done'",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))

    def test_canonical_read_only_sort_supports_compound_search_without_substitution(self) -> None:
        """Read-only result sorting is granted; shell substitution remains blocked."""
        from side_lane.governance import tool_policy

        allowed = tool_policy().allowed["shell"]
        command = (
            'grep -rn "pool_lease" --include="*.py" -l functions common '
            "| sort -u"
        )
        self.assertIsNone(policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))
        decision = policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {"command": "printf '%s\\n' \"$(pwd)\""}},
            allowed,
            (),
        )
        self.assertEqual(decision["decision"], "block")
        self.assertIn("command substitution", decision["reason"])
    def test_cd_native_grant_still_uses_hook_containment(self) -> None:
        allowed = ["Bash(python3.11 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "functions").mkdir(parents=True)
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {
                    "command": "cd functions && python3.11 -c 'print(1)'"}},
                allowed, (), worktree=str(worktree)))
            decision = policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {
                    "command": "cd /tmp && python3.11 -c 'print(1)'"}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

    def test_pythondontwritebytecode_pregrant_stays_limited_and_hook_authoritative(self) -> None:
        # The canonical hook strips the benign leading assignment and
        # continues to enforce the underlying grant and deny rules.
        allowed = ["Bash(python3 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {
                    "command": "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest"}},
                allowed, (), worktree=str(worktree)))
            # Deny rules still take precedence through the assignment prefix.
            decision = policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {
                    "command": "PYTHONDONTWRITEBYTECODE=1 git push --force origin main"}},
                ["Bash(git push *)"], ["Bash(git push --force*)"],
                worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("command denied by canonical rule", decision["reason"])
            # Ungranted commands with the same prefix are still denied.
            decision = policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {
                    "command": "PYTHONDONTWRITEBYTECODE=1 ls"}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("command is outside canonical capability grants", decision["reason"])

    def test_blocks_denied_or_ungranted_compound_components(self) -> None:
        allowed = ["Bash(git status *)"]
        denied = ["Bash(git push --force*)"]
        # Denied command must be rejected even when it follows an allowed one.
        decision = policy.evaluate_event(
            {"tool_name": "exec",
             "tool_input": {"command": "git status && git push --force origin main"}},
            allowed, denied)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("command denied by canonical rule", decision["reason"])

        for command in ("git status | git push --force",
                        "git status; git push --force",
                        "git status\ngit push --force"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, denied)
                self.assertEqual(decision["decision"], "block")
                self.assertIn("command denied by canonical rule", decision["reason"])

        # Ungranted later components are rejected without executing anything.
        for command in ("git status && nc -l 8080",
                        "git status | nc -l 8080",
                        "git status; nc -l 8080"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertEqual(decision["reason"],
                                 "command is outside canonical capability grants")

    def test_canonical_git_metadata_reads_and_fail_closed_negatives(self) -> None:
        """``git rev-parse HEAD``/``git branch --show-current`` pass; mutation does not."""
        from side_lane.governance import tool_policy
        allowed = tool_policy().allowed["shell"]
        denied = ["Bash(git push --force*)", "Bash(git push * --force*)",
                  "Bash(git push -f*)", "Bash(git push * -f*)"]
        for command in ("git rev-parse HEAD",
                        "git branch --show-current",
                        "git rev-parse HEAD && git branch --show-current",
                        "git rev-parse HEAD; git branch --show-current"):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    allowed, denied))
        for command in ("git branch new-lane",
                        "git branch -d old-lane",
                        "git branch -D old-lane",
                        "git branch -m a b",
                        "git branch --list",
                        "git rev-parse HEAD~1",
                        "git rev-parse --verify HEAD",
                        "git push origin main",
                        "git rev-parse HEAD && git branch -D old-lane",
                        "git branch --show-current && git push --force origin main"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    allowed, denied)
                self.assertEqual(decision["decision"], "block")
        self.assertIn("Bash(git rev-parse HEAD)", allowed)
        self.assertIn("Bash(git branch --show-current)", allowed)
        self.assertNotIn("Bash(git branch *)", allowed)

    def test_canonical_review_git_remote_reads_are_scoped(self) -> None:
        from side_lane.governance import tool_policy

        allowed = tool_policy().allowed["shell"]
        for command in (
            "git branch -r",
            "git merge-base HEAD origin/dev",
            "git fetch origin dev",
        ):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    allowed, (),
                ))
        for command in (
            "git branch -a",
            "git fetch evil dev",
            "git fetch --all",
            "git fetch origin --prune",
            "git fetch origin --upload-pack=evil dev",
        ):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    allowed, (),
                )
                self.assertEqual(decision["decision"], "block")

    def test_allows_quoted_and_escaped_separators(self) -> None:
        allowed = ["Bash(echo *)"]
        for command in ("echo 'a; b | c && d'", r"echo a\;b",
                        "echo \"a; b | c && d\""):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))

    def test_single_quote_inside_double_quotes_is_a_literal(self) -> None:
        allowed = ["Bash(echo *)", "Bash(git status *)"]
        for command in ('echo "it\'s fine" && git status',
                        'echo "it\'s fine"',
                        "echo 'a \"b\" c'",
                        'echo "\'"',
                        'echo "a \' b \' c" | git status'):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))
        # Genuinely unterminated quoting still fails closed.
        for command in ('echo "unterminated', "echo 'unterminated",
                        "echo \"a\" 'b", 'echo "a\\"', "echo trailing\\"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertIn("malformed", decision["reason"])

    def test_quoted_and_escaped_redirection_characters_stay_literal(self) -> None:
        allowed = ["Bash(echo *)", "Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".side-lane-scratch").mkdir(parents=True)
            for command in ('echo ">"', "echo '>>'", "echo \\> out",
                            'echo "a > b"', "echo '<'", 'echo "it\'s > fine"',
                            "echo \\> .side-lane-scratch/x"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))
            # A real operator beside a literal one is still enforced.
            for command in ("echo hi > /tmp/escape", "echo \\> ok > /tmp/escape"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("redirect", decision["reason"].lower())

    def test_fd_prefix_needs_adjacency_and_never_hides_an_argument(self) -> None:
        allowed = ["Bash(git status)", "Bash(nc -l *)"]
        denied = ["Bash(nc -l 8080)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".side-lane-scratch").mkdir(parents=True)
            # ``2>`` is a file-descriptor prefix: the digit is not an argument.
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "git status 2> .side-lane-scratch/err"}},
                allowed, denied, worktree=str(worktree)))
            # ``2 >`` is an ordinary argument, so the grant no longer matches.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "git status 2 > .side-lane-scratch/err"}},
                allowed, denied, worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertEqual(decision["reason"],
                             "command is outside canonical capability grants")
            # A quoted digit is an argument too.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": 'git status "2"> .side-lane-scratch/err'}},
                allowed, denied, worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertEqual(decision["reason"],
                             "command is outside canonical capability grants")
            # A numeric argument must never be swallowed as a prefix and hide a denial.
            for command in ("nc -l 8080 > .side-lane-scratch/out",
                            "nc -l 8080> .side-lane-scratch/out"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, denied, worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("command denied by canonical rule", decision["reason"])

    def test_trailing_separators_and_newlines_are_accepted(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)", "Bash(cat *)"]
        for command in ("git status;", "git status\n", "git status;\n",
                        "  git status ;  \n", "git status; git diff;",
                        "git status\ngit diff\n", "git status;\ngit diff",
                        "git status\n\ngit diff", "git status && git diff\n",
                        "git status &&\ngit diff", "git status |\ncat",
                        "git status;\n\n"):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))

    def test_incomplete_connectors_and_empty_statements_still_fail_closed(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)"]
        commands = (
            "git status &&",
            "git status ||",
            "git status |",
            "git status |&",
            "git status &&\n",
            "git status && \n",
            "&& git status",
            "git status && && git diff",
            "git status ;;",
            "git status ; ; git diff",
            "; git status",
            "   ",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertIn("malformed", decision["reason"])

    def test_unquoted_expansion_in_redirection_targets_is_rejected(self) -> None:
        allowed = ["Bash(git status *)", "Bash(echo *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".side-lane-scratch").mkdir(parents=True)
            # Globbing and brace expansion can name a path outside the worktree
            # only once the shell expands them, so an unquoted target fails closed.
            for command in ("git status > out*", "git status > out{1,2}",
                            "git status > *", "git status > out?",
                            "git status > out[12]", "git status >> {a,b}/out",
                            "git status &> .side-lane-scratch/out*",
                            "git status 2> out*",
                            # Only the metacharacter itself needs to be quoted
                            # to be literal: an unquoted one still globs.
                            'git status > "out"*',
                            "git status > \"out\"?"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("redirect", decision["reason"].lower())
            # Quoted or escaped targets are literal paths and stay usable.
            for command in ('git status > "out*"', "git status > 'out{1,2}'",
                            'git status > "out?"', "git status > out\\*",
                            'git status > out"*"', "git status > \\{a,b\\}"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))
            # Expansion-looking targets outside the worktree are still blocked.
            decision = policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {"command": "git status > /tmp/out?"}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

    def test_blocks_substitution_and_process_substitution(self) -> None:
        allowed = ["Bash(git status *)", "Bash(echo *)"]
        commands = (
            "echo `git status`",
            "echo $(git status)",
            'echo "$(git status)"',
            "echo <(git status)",
            "echo >(git status)",
            "git status $(pwd)",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertTrue(
                    "command substitution" in decision["reason"]
                    or "process substitution" in decision["reason"],
                    decision["reason"])

    def test_blocks_background_jobs(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)"]
        for command in ("git status &", "git status & cat",
                        "git status && git diff &"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertIn("background", decision["reason"])

    def test_blocks_malformed_and_incomplete_operators(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)"]
        commands = (
            "git status &&& git diff",
            "git status ||| git diff",
            "git status ; ; git diff",
            "git status &&",
            "git status ||",
            "git status |",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")
                self.assertTrue(
                    "malformed" in decision["reason"] or "background" in decision["reason"],
                    decision["reason"])

    def test_redirection_stays_inside_lane_worktree_and_blocks_cwd_bypass(self) -> None:
        allowed = ["Bash(git status *)", "Bash(git diff *)", "Bash(echo *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".side-lane-scratch").mkdir(parents=True)
            (worktree / "pkg").mkdir(parents=True)

            # Output redirection into the lane worktree or scratch is fine.
            for command in ("git status > .side-lane-scratch/out",
                            "git status >> .side-lane-scratch/out",
                            "git status >| .side-lane-scratch/out",
                            "git status 2>&1",
                            "git status 2>/dev/null",
                            "git status > /dev/null",
                            "git status > out 2>&1",
                            "git status &> .side-lane-scratch/out",
                            "git status &>> .side-lane-scratch/out"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))

            # git -C normalization and redirection are evaluated per component.
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": f"git -C {worktree} status && git -C {worktree} diff"}},
                allowed, (), worktree=str(worktree)))

            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": f"git -C {worktree} push --force origin main"}},
                ["Bash(git push *)"], ["Bash(git push --force*)"], worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("command denied by canonical rule", decision["reason"])

            # Output must not escape the lane worktree.
            for command in ("git status > /tmp/out",
                            "git status > $HOME/out",
                            "git status > ~/out",
                            "git status > \"$HOME/out\"",
                            "git status > ../out",
                            "git status 2> /tmp/err",
                            "git status > /dev/zero"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("redirect", decision["reason"].lower())

            # A symlink inside the worktree that points elsewhere must not redirect output outside.
            escape = worktree / "escape-link"
            escape.symlink_to(Path("/tmp"))
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "git status > escape-link/out"}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("redirect", decision["reason"].lower())

            # cwd-changing commands or git -C outside the worktree are not allowed.
            for command in ("cd /tmp && git status",
                            f"git -C /tmp status > out"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

            # Unsupported redirections fail closed; an unquoted here-document
            # delimiter is rejected too because the shell would expand its body.
            for command, reason in (
                    ("git status < /tmp/in", "unsupported redirection"),
                    ("git status <<< \"line\"", "unsupported redirection"),
                    ("git status << EOF\nhello\nEOF", "here-document")):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn(reason, decision["reason"])

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
        # as-is (and fail the unstripped canonical match) rather than stripped.
        allowed = ["Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
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


    def test_carriage_return_is_not_a_shell_line_continuation(self) -> None:
        for command in ("echo a" + chr(92) + "\rb", 'echo "a' + chr(92) + '\rb"'):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    ["Bash(echo *)"], ["Bash(echo ab)"]))

    def test_continued_substitution_opener_stays_blocked(self) -> None:
        continuation = chr(92) + "\n"
        for gap in (continuation, continuation * 2):
            for command in ('echo "$' + gap + '(pwd)"', 'echo $' + gap + '(pwd)'):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        ["Bash(echo *)"], ())
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("command substitution", decision["reason"])
        for command in ("echo '$" + continuation + "(pwd)'",
                        'echo "' + chr(92) + '$' + continuation + '(pwd)"'):
            with self.subTest(literal=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}},
                    ["Bash(echo *)"], ()))

    def test_backslash_newline_continuation_is_normalized_before_matching(self) -> None:
        """Backslash-newline is a line continuation; single-quoted newlines stay literal."""

        bsnl = chr(92) + "\n"

        allowed = ["Bash(git status *)", "Bash(git push *)"]
        denied = ["Bash(git push --force*)", "Bash(git push * --force*)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".side-lane-scratch").mkdir(parents=True)

            # Exact repro: a split `--force` inside unquoted and double-quoted
            # text must normalize and hit the deny rule.
            for command in (
                "git push --for" + bsnl + "ce origin main",
                'git push "--for' + bsnl + 'ce" origin main',
                "git push origin main --for" + bsnl + "ce",
            ):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, denied)
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn("command denied by canonical rule", decision["reason"])

            # Harmless continuations are still allowed and matched correctly.
            for command in (
                "git stat" + bsnl + "us",
                "git " + bsnl + "status",
                "git status >" + bsnl + ".side-lane-scratch/out",
            ):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))

            # Single-quoted backslash-newline is literal, not a continuation.
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec", "tool_input": {"command": "echo 'a" + bsnl + "b'"}},
                ["Bash(echo *)"], ()))

    def test_cd_into_lane_contained_directory_authorizes_following_commands(self) -> None:
        allowed = ["Bash(uv *)", "Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "functions" / "sideLaneWorker").mkdir(parents=True)
            # The exact worker shape: cd into the lane directory, then a
            # granted command. Quoted ';' inside the python -c argument stays
            # a literal.
            for command in (
                    "cd functions/sideLaneWorker && "
                    "uv run --frozen python -c \"import mcp, sys; print(mcp.__file__, sys.version)\"",
                    "cd functions && git status",
                    "cd . && git status",
                    f"cd {worktree} && git status",
                    "cd functions; git status"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))

    def test_cd_outside_the_lane_worktree_still_fails_closed(self) -> None:
        allowed = ["Bash(uv *)", "Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "pkg").mkdir(parents=True)
            for command in ("cd /tmp && git status",
                            "cd .. && git status",
                            "cd pkg/../.. && git status",
                            "cd \"$HOME\" && git status",
                            "cd ~ && git status",
                            "cd /tmp",
                            "cd",
                            "cd pkg extra && git status"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
            # Without a worktree there is no containment proof.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "cd pkg && git status"}},
                allowed, ())
            self.assertEqual(decision["decision"], "block")

    def test_lane_contained_executable_paths_match_basename_grants(self) -> None:
        allowed = ["Bash(python *)", "Bash(python3 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".venv" / "bin").mkdir(parents=True)
            subdir = worktree / "functions"
            subdir.mkdir()
            for tool_input in (
                    {"command": ".venv/bin/python -c \"import sys\""},
                    {"command": ".venv/bin/python -c \"import sys\"",
                     "workdir": str(worktree)},
                    {"command": ".venv/bin/python -c \"import sys\"",
                     "workdir": str(subdir)},
                    {"command": f"{worktree}/.venv/bin/python -c \"x\""},
                    {"command": "./.venv/bin/python3 -c \"x\""}):
                with self.subTest(tool_input=tool_input):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree)))
            # Arbitrary absolute executables, escapes, unknown basenames and
            # out-of-lane workdirs are all still refused.
            for tool_input in (
                    {"command": "/usr/bin/python3 -c \"x\""},
                    {"command": "../outside/python -c \"x\""},
                    {"command": ".venv/bin/nc -l 8080"},
                    {"command": ".venv/bin/python -c \"x\"", "workdir": "/tmp"},
                    {"command": ".venv/bin/python -c \"x\"", "workdir": ".."},
                    {"command": ".venv/bin/python -c \"x\"",
                     "workdir": str(worktree / ".." )},
                    {"command": ".venv/bin/python -c \"x\"", "workdir": 7}):
                with self.subTest(tool_input=tool_input):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
            # Deny precedence is preserved through the basename spelling.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": f"{worktree}/bin/git push --force origin main"}},
                ["Bash(git push *)"], ["Bash(git push --force*)"],
                worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            self.assertIn("command denied by canonical rule", decision["reason"])

    def test_venv_symlinked_interpreter_admitted_only_when_approved(self) -> None:
        """A real venv's ``.venv/bin/python`` symlinks to the host interpreter."""
        allowed = ["Bash(python *)", "Bash(python3 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / ".venv" / "bin").mkdir(parents=True)
            host = Path(directory) / "host" / "bin"
            host.mkdir(parents=True)
            interpreter = host / "python3.11"
            interpreter.write_text("#! host interpreter\n")
            interpreter.chmod(0o755)  # user-writable like a Homebrew/uv install
            (worktree / ".venv" / "bin" / "python").symlink_to(interpreter)
            (worktree / ".venv" / "bin" / "python3").symlink_to(interpreter)

            # The interpreter is trusted because the hook's own PATH names its
            # directory -- a command's env-assignment prefixes never reach the
            # hook environment, so this models user-managed Homebrew/uv and
            # root-owned system interpreters alike.
            with mock.patch.dict(os.environ, {"PATH": str(host)}):
                # The venv launcher resolves outside the lane yet is admitted
                # under the narrow interpreter rule, relative or absolute, and
                # with a contained workdir.
                for tool_input in (
                        {"command": ".venv/bin/python -c \"import sys\""},
                        {"command": ".venv/bin/python3 -c \"import sys\""},
                        {"command": f"{worktree}/.venv/bin/python -c \"x\""},
                        {"command": ".venv/bin/python -c \"import sys\"",
                         "workdir": str(worktree)}):
                    with self.subTest(tool_input=tool_input):
                        self.assertIsNone(policy.evaluate_event(
                            {"tool_name": "exec", "tool_input": tool_input},
                            allowed, (), worktree=str(worktree)))

            # The same launcher is refused when its target is not reachable
            # through a trusted host location.
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": ".venv/bin/python -c \"import sys\""}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

            # Arbitrary external symlink targets stay refused even under a
            # python-shaped spelling and a trusted PATH directory.
            busybox = host / "busybox"
            busybox.write_text("x")
            busybox.chmod(0o555)
            (worktree / ".venv" / "bin" / "python2").symlink_to(busybox)
            with mock.patch.dict(os.environ, {"PATH": str(host)}):
                decision = policy.evaluate_event(
                    {"tool_name": "exec",
                     "tool_input": {"command": ".venv/bin/python2 -c \"x\""}},
                    allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

            # A task-written external python-named script is never in the
            # trusted set, even when the planted interpreter dir is supplied
            # through a command-level PATH assignment.
            planted = Path(directory) / "planted"
            planted.mkdir()
            fake = planted / "python3"
            fake.write_text("#! planted script\n")
            fake.chmod(0o755)
            (worktree / ".venv" / "bin" / "pythonw").symlink_to(fake)
            for tool_input in (
                    {"command": ".venv/bin/pythonw -c \"x\""},
                    {"command": "PATH=.venv/bin .venv/bin/pythonw -c \"x\""}):
                with self.subTest(tool_input=tool_input):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        [*allowed, "Bash(pythonw *)"], (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

            # A dangling symlink and a symlinked *directory* escaping the lane
            # are both refused.
            (worktree / ".venv" / "bin" / "pythond").symlink_to(host / "missing")
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": ".venv/bin/pythond -c \"x\""}},
                [*allowed, "Bash(pythond *)"], (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            (worktree / "vlink").symlink_to(host)
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "vlink/bin/python3.11 -c \"x\""}},
                [*allowed, "Bash(python3.11 *)"], (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

            # An ungranted basename still fails the canonical match.
            tool = host / "tool"
            tool.write_text("x")
            tool.chmod(0o555)
            (worktree / ".venv" / "bin" / "nc").symlink_to(tool)
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": ".venv/bin/nc -l 8080"}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")

    def test_runtime_created_venv_launcher_is_admitted(self) -> None:
        """An actual ``venv``-created ``.venv/bin/python`` passes the policy."""
        allowed = ["Bash(python *)", "Bash(python3 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            venv.create(worktree / ".venv", with_pip=False, symlinks=True)
            launcher = worktree / ".venv" / "bin" / "python"
            if not launcher.exists():
                self.skipTest("venv did not produce a POSIX bin/python launcher")
            if not launcher.is_symlink():
                self.skipTest("venv copied the interpreter instead of symlinking")
            for tool_input in (
                    {"command": ".venv/bin/python -c \"import sys; "
                                "print(sys.version)\""},
                    {"command": ".venv/bin/python3 -c \"import sys\""},
                    {"command": f"{worktree}/.venv/bin/python -c \"x\""}):
                with self.subTest(tool_input=tool_input):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree)))

            # A rewritten ``home`` mapping no longer names the interpreter the
            # launcher resolves to, so the launcher is refused.
            config = worktree / ".venv" / "pyvenv.cfg"
            original = config.read_text(encoding="utf-8")
            config.write_text("home = /nonexistent-host/bin\n", encoding="utf-8")
            decision = policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": ".venv/bin/python -c \"x\""}},
                allowed, (), worktree=str(worktree))
            self.assertEqual(decision["decision"], "block")
            config.write_text(original, encoding="utf-8")
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": ".venv/bin/python -c \"x\""}},
                allowed, (), worktree=str(worktree)))

    def test_relative_paths_resolve_against_effective_workdir_and_cd(self) -> None:
        """Relative executable/redirect/git -C/env paths anchor at the real cwd."""
        allowed = ["Bash(git status *)", "Bash(echo *)", "Bash(python *)",
                   "Bash(tool *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            sub = worktree / "sub"
            sub.mkdir(parents=True)
            # ``link`` exists at the lane root as a real directory while the
            # same lexical path in the nested cwd is a symlink escape.
            (worktree / "link").mkdir()
            (sub / "link").symlink_to(Path("/tmp"))
            outside = Path(directory) / "outside-tool"
            outside.write_text("x")
            outside.chmod(0o555)
            (worktree / "tool").write_text("x")
            (worktree / "tool").chmod(0o555)
            (sub / "tool").symlink_to(outside)

            # Without a nested cwd the lexical paths are genuinely contained.
            for tool_input in (
                    {"command": "git status > link/out"},
                    {"command": "./tool run"},
                    {"command": "git -C link status"},
                    {"command": "PYTHONPATH=link python -c \"x\""}):
                with self.subTest(tool_input=tool_input):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree)))

            # With workdir=sub the same spellings hit the symlink escape and
            # must be refused.
            for tool_input in (
                    {"command": "git status > link/out", "workdir": str(sub)},
                    {"command": "./tool run", "workdir": str(sub)},
                    {"command": "git -C link status", "workdir": str(sub)},
                    {"command": "PYTHONPATH=link python -c \"x\"",
                     "workdir": str(sub)}):
                with self.subTest(tool_input=tool_input):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

            # In-compound cd state is tracked across every statement separator.
            for command in ("cd sub && git status > link/out",
                            "cd sub; git status > link/out",
                            "cd sub\ngit status > link/out",
                            "cd sub && ./tool run",
                            "cd sub && git -C link status",
                            "cd sub && PYTHONPATH=link python -c \"x\""):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

            # Contained targets under the nested cwd remain usable.
            for tool_input in (
                    {"command": "git status > out", "workdir": str(sub)},
                    {"command": "cd sub && git status > out"},
                    {"command": "cd sub && git status > ../link/out"}):
                with self.subTest(tool_input=tool_input):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": tool_input},
                        allowed, (), worktree=str(worktree)))

            # A piped ``cd`` runs in a subshell and does not move the lane cwd;
            # the redirect then anchors at the root where ``link`` is real.
            self.assertIsNone(policy.evaluate_event(
                {"tool_name": "exec",
                 "tool_input": {"command": "cd sub | git status > link/out"}},
                allowed, (), worktree=str(worktree)))

    def test_environment_assignment_prefix_before_an_approved_command(self) -> None:
        allowed = ["Bash(python *)", "Bash(python3 *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            (worktree / "src" / "pkg").mkdir(parents=True)
            for command in ("PYTHONPATH=src/pkg python -c \"import mcp\"",
                            f"PYTHONPATH={worktree}/src/pkg python3 -c \"x\"",
                            "PYTHONPATH=src/pkg:. python -c \"x\"",
                            "FOO=bar PYTHONPATH=src/pkg python -c \"x\"",
                            "PYTHONPATH= python -c \"x\"",
                            "FOO=plainliteral python -c \"x\""):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))
            # Path-bearing values must stay inside the lane; ungranted
            # commands after the prefix are still refused, as is a
            # quoted-assignment word (not an assignment in shell syntax).
            for command in ("PYTHONPATH=/etc/python python -c \"x\"",
                            "PYTHONPATH=../outside python -c \"x\"",
                            "PYTHONPATH=src:/tmp/evil python -c \"x\"",
                            "PYTHONPATH=$HOME/pkg python -c \"x\"",
                            "PYTHONPATH=~/pkg python -c \"x\"",
                            "FOO=bar nc -l 8080",
                            "\"FOO=bar\" python -c \"x\"",
                            "FOO=bar"):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")

    def test_quoted_heredoc_body_is_data_not_commands(self) -> None:
        allowed = ["Bash(python3 *)", "Bash(git status *)"]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "lane"
            worktree.mkdir()
            for command in ("python3 - <<'PY'\nprint(1)\nPY",
                            "python3 - <<'PY'\nprint(1)\nPY\n",
                            # The body is never re-parsed: substitution-looking
                            # and metacharacter content stays literal data.
                            "python3 - <<'PY'\nimport os; os.system('x')\n"
                            "$(rm -rf /) `id` ; && | > /tmp/x\nPY",
                            "python3 - <<\"PY\"\nprint(2)\nPY",
                            "python3 - <<\\PY\nprint(3)\nPY",
                            "python3 - <<-'PY'\n\tprint(4)\n\tPY",
                            "git status && python3 - <<'PY'\nx=1\nPY",
                            "python3 - <<'PY'\nPY"):
                with self.subTest(command=command):
                    self.assertIsNone(policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed, (), worktree=str(worktree)))
            for command, reason in (
                    # Unquoted delimiters let the shell expand the body.
                    ("python3 - <<PY\nprint(1)\nPY", "here-document"),
                    ("python3 - <<'PY'\nprint(1)", "here-document"),
                    ("python3 - <<'PY'", "here-document"),
                    # A line continuation after the redirection is ambiguous;
                    # fail closed rather than guess which lines are the body.
                    ("python3 - <<'PY' &&\ngit status\nx\nPY", "here-document"),
                    # The first line after the terminator is a real command
                    # again and must be granted.
                    ("python3 - <<'PY'\nx\nPY\nrm -rf /", "capability grants"),
                    ("python3 - <<'PY'\nx\nPY\ngit push --force", "denied")):
                with self.subTest(command=command):
                    decision = policy.evaluate_event(
                        {"tool_name": "exec", "tool_input": {"command": command}},
                        allowed + ["Bash(git push *)"],
                        ["Bash(git push --force*)"], worktree=str(worktree))
                    self.assertEqual(decision["decision"], "block")
                    self.assertIn(reason, decision["reason"])


    def test_terraform_benign_commands_are_allowed_and_mutating_siblings_rejected(self) -> None:
        from side_lane.governance import tool_policy

        allowed = tool_policy().allowed["shell"]
        for command in ("terraform fmt",
                        "terraform fmt -recursive",
                        "terraform fmt -diff",
                        "terraform validate",
                        "terraform validate -no-color",
                        "terraform version"):
            with self.subTest(command=command):
                self.assertIsNone(policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ()))
        for command in ("terraform",
                        "terraform init",
                        "terraform plan",
                        "terraform apply",
                        "terraform destroy",
                        "terraform import",
                        "terraform state",
                        "terraform state list",
                        "terraform version -json",
                        "terraform validate; terraform plan",
                        "terraform fmt && terraform init"):
            with self.subTest(command=command):
                decision = policy.evaluate_event(
                    {"tool_name": "exec", "tool_input": {"command": command}}, allowed, ())
                self.assertEqual(decision["decision"], "block")

if __name__ == "__main__":
    unittest.main()
