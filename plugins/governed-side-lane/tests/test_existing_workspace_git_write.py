"""Owner-workspace direct git writes: the guard, its seams, and its limits.

The defect this covers (Copilot review finding 4079567342): an
``--existing-workspace`` execute lane runs the operator's own checkout under
the ``local-developer`` execute profile, whose allow rule is the host's whole
native shell class. The canonical contract for that lane says "Make no git
write of any kind", and the adapters' comments said the section "drops the
commit/push grant" — but *dropping a grant is not a denial*: with the whole
shell class allowed, nothing matched ``git commit`` at all, so the write was
exactly as reachable as any other command, while the lane's own contract and
its adapters both read as though it were stopped.

What these tests pin is the repair and its edges:

* the forbidden *verbs* are read from the ``## Existing owner workspace``
  prohibition bullet a worker is handed, the *rules* from the
  ``existing-workspace (denied)`` allowlist bucket, and ``tool_policy()`` fails
  closed when the two disagree — so the sentence and the seam cannot drift;
* each host renders what it can actually do — ``--disallowedTools`` on Claude,
  the native deny list *and* the PreToolUse command policy on Devin,
  instruction only on Codex — and says so instead of claiming containment;
* the denial is a property of the workspace selection, not of a grant: it holds
  with no capabilities at all, and with ``git-push`` granted or absent, because
  the lane it protects is the one whose allow side is the whole shell class;
* nothing else moves: an ordinary dedicated-worktree lane keeps its commit and
  push grant, the read-only git commands the section names keep working in the
  owner's checkout, and the execute profile itself is not narrowed.

Everything is offline: no host process is started, no credential is read, and
no model is called.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import devin_command_policy, governance, worktrees
from side_lane.adapters import claude, codex, devin
from side_lane.governance import GovernanceError, tool_policy


# The document the code actually reads, not a path this test guesses: the
# private and public checkouts lay the tree out differently.
GOVERNANCE = governance.GOVERNANCE_PATH
NATIVE = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
NATIVE_MODEL = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}

#: The verbs the prohibition bullet names, in its own order. Kept here as the
#: expected *reading* of the document, so a verb that reaches the rules but not
#: the prose — or the reverse — fails rather than passing silently. The second
#: group is the git-state family the first repair missed: commands whose every
#: spelling is a write, and which move no path a status reading can see.
FORBIDDEN_VERBS = (
    "add", "commit", "push", "checkout", "switch", "restore", "reset",
    "stash", "clean", "rm", "mv", "apply", "am", "rebase", "merge",
    "cherry-pick", "revert",
    "update-ref", "symbolic-ref", "update-index", "read-tree", "write-tree",
    "commit-tree", "pack-refs", "replace", "filter-branch", "config",
)
DENIED_RULES = tuple(
    rule for verb in FORBIDDEN_VERBS
    for rule in (f"Bash(git {verb})", f"Bash(git {verb} *)")
)
#: Direct git writes, spelled as a worker would type them. Every one of these is
#: a command the section forbids and that a shell-class lane could otherwise run.
GIT_WRITES = (
    "git add -A", "git commit -m x", "git push origin HEAD",
    "git checkout -b other", "git switch other", "git restore .",
    "git reset --hard", "git stash", "git clean -fd", "git rm x", "git mv a b",
    "git apply p.diff", "git am p.mbox", "git rebase main", "git merge main",
    "git cherry-pick abc123", "git revert HEAD",
    "git update-ref refs/heads/main HEAD", "git symbolic-ref HEAD refs/heads/x",
    "git update-index --assume-unchanged file.txt", "git read-tree HEAD",
    "git write-tree", "git commit-tree HEAD^{tree}", "git pack-refs --all",
    "git replace -f HEAD HEAD~1", "git filter-branch --force --all",
    "git config core.hooksPath /tmp/hooks",
)
#: Read-only git commands the same section says keep working exactly as before.
#: The last four are the inspection spellings of verbs whose *write* spellings
#: are deliberately not denied: the mode keeps `git branch -a`, so it cannot
#: deny the verb, and a write through `git branch -D` is caught by the
#: after-the-fact comparison instead.
READ_ONLY = (
    "git status --short", "git log --oneline -5", "git diff HEAD",
    "git show HEAD", "git rev-parse HEAD", "git branch -a",
    "git tag --list", "git remote -v", "git worktree list", "git reflog show",
)


def normalized(text: str) -> str:
    """Collapse whitespace, so a reflowed canonical paragraph still matches."""

    return " ".join(text.split())


class OwnerWorkspaceDeclarationTests(unittest.TestCase):
    """The canonical document is the only owner of the forbidden verb list."""

    def test_the_document_states_the_rule_and_its_seams(self) -> None:
        document = GOVERNANCE.read_text(encoding="utf-8")
        self.assertIn("## Existing owner workspace", document)
        self.assertIn("### existing-workspace (denied)", document)
        text = normalized(document)
        # The rule, the sentence tying the prose to the rules, and the per-host
        # seams are all in the document the worker is handed — not only in an
        # adapter comment the worker never reads.
        self.assertIn("Make no git write of any kind", text)
        self.assertIn(
            "the command you are told not to run and the command each host is "
            "given to deny are one list", text)
        self.assertIn("on Claude they render as `--disallowedTools` deny rules", text)
        self.assertIn("in the native permission deny list", text)
        self.assertIn("PreToolUse command policy", text)
        self.assertIn("Codex carries the instruction only", text)
        # ... and so are the limits, stated rather than papered over.
        self.assertIn("not a sandbox", text)
        self.assertIn(
            "does not promise that a shell which must stay arbitrary is also a "
            "sandbox", text)
        self.assertIn("an ordinary lane keeps the commit and push grant", text)
        self.assertIn("every read-only git command", text)

    def test_the_forbidden_verbs_derive_from_the_prohibition_bullet(self) -> None:
        from side_lane.governance import existing_workspace_denied_verbs
        self.assertEqual(existing_workspace_denied_verbs(), FORBIDDEN_VERBS)
        # The read-only commands the section names in its other bullets are not
        # swept in: the enumeration is read from the prohibition bullet alone,
        # so a section that never forbids `git status` cannot deny it.
        for verb in ("status", "log", "diff", "show", "branch", "rev-parse",
                     "tag", "remote", "worktree", "reflog"):
            self.assertNotIn(verb, existing_workspace_denied_verbs())
        # A valid document naming a different verb proves the derivation: the
        # list follows the document, not a tuple kept in Python.
        base = GOVERNANCE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            path.write_text(
                base.replace("`git config`.", "`git config`, or `git notes`."),
                encoding="utf-8",
            )
            derived = existing_workspace_denied_verbs(path)
        self.assertEqual(derived[:-1], FORBIDDEN_VERBS)
        self.assertEqual(derived[-1], "notes")

    def test_the_denied_rules_are_a_reserved_bucket_not_a_capability(self) -> None:
        from side_lane.governance import EXISTING_WORKSPACE_DENIED_HEADING
        policy = tool_policy()
        self.assertEqual(policy.existing_workspace_denied, DENIED_RULES)
        self.assertEqual(len(policy.existing_workspace_denied), 2 * len(FORBIDDEN_VERBS))
        # Reserved: no grant unlocks them, so the heading never becomes a
        # capability a route could name.
        self.assertNotIn(EXISTING_WORKSPACE_DENIED_HEADING, policy.capabilities)
        self.assertNotIn("existing-workspace", policy.capabilities)
        self.assertNotIn("existing-workspace", policy.allowed)
        self.assertNotIn("existing-workspace", policy.denied)

    def test_a_missing_or_duplicated_bucket_fails_closed(self) -> None:
        base = GOVERNANCE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            # A document that never declares the bucket. The rules below the
            # removed heading fall outside every subsection, so the parser
            # reports the bucket missing rather than silently reading zero
            # rules as "nothing to deny".
            path.write_text(
                base.replace("### existing-workspace (denied)\n", "", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "existing-workspace"):
                tool_policy(path)
            # Declared twice: two buckets cannot disagree silently.
            path.write_text(
                base.replace(
                    "### existing-workspace (denied)\n",
                    "### existing-workspace (denied)\n\n- `Bash(git commit)`\n\n"
                    "### existing-workspace (denied)\n",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "more than once"):
                tool_policy(path)

    def test_a_reworded_prohibition_fails_closed(self) -> None:
        from side_lane.governance import existing_workspace_denied_verbs
        base = GOVERNANCE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            # The bullet the verbs are read from is gone, so the derivation has
            # nothing to read: fail closed instead of returning an empty list
            # that would make the coverage check below vacuous.
            path.write_text(
                base.replace("- Make no git write of any kind.", "- Make no git write."),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "exactly once"):
                existing_workspace_denied_verbs(path)

    def without_bucket_rule(self, base: str, rule: str) -> str:
        """``base`` with one rule bullet dropped from the reserved bucket.

        Anchored inside the bucket itself: the same rule spellings appear in
        the report-deliverable bucket above it, so a whole-document replace
        would silently edit the wrong subsection and test nothing.
        """

        head, marker, tail = base.partition("### existing-workspace (denied)\n")
        self.assertTrue(marker)
        body, next_heading, rest = tail.partition("\n### ")
        line = f"- `{rule}`\n"
        self.assertIn(line, body)
        return head + marker + body.replace(line, "", 1) + next_heading + rest

    def test_a_forbidden_verb_without_a_deny_rule_fails_closed(self) -> None:
        base = GOVERNANCE.read_text(encoding="utf-8")
        cases = tuple(
            (rule, verb)
            for verb in FORBIDDEN_VERBS
            for rule in (f"Bash(git {verb})", f"Bash(git {verb} *)")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            # The prose forbids the verb and one of its two rule spellings is
            # gone: a prohibition a host seam no longer carries is the exact
            # shape of the defect this guard exists for.
            for rule, verb in cases:
                with self.subTest(rule=rule):
                    path.write_text(
                        self.without_bucket_rule(base, rule), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(GovernanceError, f"git {verb}"):
                        tool_policy(path)


class ClaudeOwnerWorkspaceTests(unittest.TestCase):
    native = NATIVE
    model_config = NATIVE_MODEL

    def worktree(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /example\n", encoding="utf-8")
        return path

    def test_the_denials_are_a_property_of_the_lane_not_a_grant(self) -> None:
        # Rendered even for a lane granted nothing, and never in review mode,
        # whose argv carries no permission rule this could narrow.
        self.assertEqual(
            claude.disallowed_tools("execute", (), existing_workspace=True),
            DENIED_RULES,
        )
        self.assertEqual(
            claude.disallowed_tools("review", (), existing_workspace=True), ()
        )
        self.assertEqual(claude.disallowed_tools("execute", ()), ())

    def test_the_denials_hold_under_the_widest_allow_rule(self) -> None:
        """Deny wins in Claude Code, so the bare shell class cannot reopen it.

        The owner-workspace lane runs the ``local-developer`` profile, whose
        allow rule is the bare ``Bash`` class: every spelling is allowed at
        once, which is why the deny list is the whole control on this seam.
        """
        for capabilities in ((), ("shell",), ("shell", "workspace-write"),
                             ("shell", "workspace-write", "git-push")):
            with self.subTest(capabilities=capabilities):
                self.assertIn("Bash", claude.allowed_tools(
                    "execute", capabilities,
                    execute_profile=claude.LOCAL_DEVELOPER_PROFILE))
                denied = claude.disallowed_tools(
                    "execute", capabilities, existing_workspace=True,
                    execute_profile=claude.LOCAL_DEVELOPER_PROFILE)
                for rule in DENIED_RULES:
                    self.assertIn(rule, denied)
                # The profile's own denials are still there, so the guard adds
                # to the local-developer list rather than replacing it.
                self.assertTrue(set(claude.disallowed_tools(
                    "execute", (), execute_profile=claude.LOCAL_DEVELOPER_PROFILE
                )).issubset(denied))

    def test_the_push_denial_does_not_depend_on_the_git_push_capability(self) -> None:
        """The workspace selection denies it, not the capability grant.

        An owner-workspace lane has no assigned branch, so the push is denied
        whether or not the route also granted the capability that would
        ordinarily make it available.
        """
        without = claude.disallowed_tools(
            "execute", ("shell",), existing_workspace=True)
        granted = claude.disallowed_tools(
            "execute", ("shell", "git-push"), existing_workspace=True)
        for rule in ("Bash(git push)", "Bash(git push *)"):
            self.assertIn(rule, without)
            self.assertIn(rule, granted)
        # In both cases the rest of the git-write set is there too, so the
        # capability's presence is not what the guard hangs on.
        for rules in (without, granted):
            for rule in DENIED_RULES:
                self.assertIn(rule, rules)

    def test_an_ordinary_execute_lane_is_unchanged(self) -> None:
        ordinary = claude.disallowed_tools("execute", ("shell", "workspace-write"))
        for rule in DENIED_RULES:
            self.assertNotIn(rule, ordinary)
        # A dedicated lane is refused the workspace section entirely, and its
        # shell class is the enumeration, not the profile's bare `Bash`.
        self.assertNotIn("Bash", claude.allowed_tools("execute", ("shell",)))

    def test_an_execute_lane_carries_the_denials_and_the_instruction(self) -> None:
        capabilities = ("shell", "workspace-write")
        expected = claude.disallowed_tools(
            "execute", capabilities, existing_workspace=True,
            execute_profile=claude.LOCAL_DEVELOPER_PROFILE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            argv = list(claude.build_command(
                executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config=self.model_config, prompt="Do it", mode="execute",
                capabilities=capabilities, existing_workspace=True,
                execute_profile=claude.LOCAL_DEVELOPER_PROFILE,
            ))
        rendered = " ".join(argv)
        for rule in DENIED_RULES:
            self.assertIn(rule, rendered)
        # Paired with `--disallowedTools`, not merely appended somewhere.
        self.assertEqual(argv.count("--disallowedTools"), len(expected))
        # The profile is not narrowed: the lane still carries the bare shell
        # class the deny list is holding against.
        self.assertIn("Bash", claude.allowed_tools(
            "execute", capabilities, execute_profile=claude.LOCAL_DEVELOPER_PROFILE))
        self.assertIn("--allowedTools", argv)
        self.assertIn("## Existing owner workspace", rendered)
        self.assertIn("Make no git write of any kind", normalized(rendered))

    def test_the_workspace_lane_is_execute_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "execute mode"):
                claude.build_command(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=self.native,
                    model_config={"runtime_model": "claude-sonnet-5",
                                  "protocol": "native-claude-readonly"},
                    prompt="Review it", mode="review", existing_workspace=True,
                )


class DevinOwnerWorkspaceTests(unittest.TestCase):
    model = "swe-2-medium"
    model_config = {
        "runtime_model": "swe-2-medium",
        "protocol": "native-devin",
        "identity_contract": {
            "requested_model": "swe-2-medium",
            "resolved_model": "swe-2-medium",
            "settings_precedence": "verified",
        },
        "qualification": {
            "verified": True, "verified_on": "2026-09-11",
            "source": "mocked local report",
        },
        "timeout_seconds": 600,
    }
    provider_config = {"gateway": "native-devin", "auth_method": "oauth",
                       "billable": False}

    def hook(self, command: str, allowed, denied, worktree: str = "/tmp/owner"):
        return devin_command_policy.evaluate_event(
            {"tool_name": "exec", "tool_input": {"command": command}},
            allowed, denied, worktree=worktree,
        )

    def owner_lane_rules(self, capabilities=("shell",)):
        """The hook's rules exactly as ``launch`` computes them, plus the guard."""

        policy = tool_policy()
        allowed, denied = devin.command_policy_rules(
            policy, capabilities, execute_profile=claude.LOCAL_DEVELOPER_PROFILE
        )
        denied = list(dict.fromkeys([*denied, *policy.existing_workspace_denied]))
        return allowed, denied

    def test_the_hook_blocks_every_forbidden_write(self) -> None:
        """The shell-class lane: nothing else on this host matches the write."""

        allowed, denied = self.owner_lane_rules()
        self.assertIn("Bash", allowed, "the lane's allow side is the whole shell class")
        for command in GIT_WRITES:
            with self.subTest(command=command):
                decision = self.hook(command, allowed, denied)
                self.assertIsNotNone(decision, command)
                self.assertEqual(decision["decision"], "block")
                self.assertIn("command denied by canonical rule", decision["reason"])
        # Non-vacuous: without the guard the very same command is authorized by
        # the lane's own shell class, which is the defect this test exists for.
        self.assertIsNone(
            self.hook("git commit -m x", allowed, ()),
            "the shell-class allow rule authorizes the write once the deny set "
            "is empty",
        )

    def test_the_hook_lets_the_read_only_commands_through(self) -> None:
        allowed, denied = self.owner_lane_rules()
        for command in READ_ONLY:
            with self.subTest(command=command):
                self.assertIsNone(self.hook(command, allowed, denied), command)

    def test_the_hook_normalises_the_dash_c_spelling(self) -> None:
        """`git -C <path> <verb>` is the spelling a prefix rule alone misses."""

        allowed, denied = self.owner_lane_rules()
        for command in ("git -C /elsewhere commit -m x", 'git -C "$PWD" commit -m x',
                        "cd . && git commit -m x"):
            with self.subTest(command=command):
                decision = self.hook(command, allowed, denied)
                self.assertIsNotNone(decision, command)
                self.assertEqual(decision["decision"], "block")
        # The guard denies writes, not reads: a read-only command against any
        # checkout is still authorized.
        for command in ("git -C /elsewhere status --short", "git status"):
            self.assertIsNone(self.hook(command, allowed, denied), command)

    def test_the_native_deny_list_carries_the_canonical_denials(self) -> None:
        guard = devin._runtime_config(self.model, (), existing_workspace=True)
        plain = devin._runtime_config(self.model, ())
        for verb in FORBIDDEN_VERBS:
            with self.subTest(verb=verb):
                self.assertIn(f"Exec(git {verb})", guard["permissions"]["deny"])
                self.assertNotIn(f"Exec(git {verb})", plain["permissions"]["deny"])
        # Additive: an inherited deny rule is preserved beside the new ones, and
        # an ordinary lane's config is untouched.
        inherited = {"permissions": {"deny": ["Exec(git status)"]}}
        merged = devin._runtime_config(
            self.model, (), inherited, existing_workspace=True
        )
        self.assertIn("Exec(git status)", merged["permissions"]["deny"])
        self.assertIn("Exec(git commit)", merged["permissions"]["deny"])

    def test_the_native_push_denial_does_not_need_the_git_push_capability(self) -> None:
        without = devin._runtime_config(self.model, ("shell",),
                                        existing_workspace=True)
        granted = devin._runtime_config(self.model, ("shell", "git-push"),
                                        existing_workspace=True)
        for config in (without, granted):
            self.assertIn("Exec(git push)", config["permissions"]["deny"])
            self.assertIn("Exec(git commit)", config["permissions"]["deny"])

    def test_launch_installs_both_controls(self) -> None:
        """The native deny list and the PreToolUse policy, from one declaration.

        A real repository backs this one: an owner-workspace run's ephemeral
        runtime lives under the repository's own ``.git``, which is resolved
        with a real ``git rev-parse``. No host process runs — the per-run files
        are written before it — and no model is called.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email",
                            "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                           check=True)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"],
                           check=True, capture_output=True)
            with mock.patch.object(devin, "_run", return_value=(0, "{}", "")), \
                 mock.patch.object(devin, "_load_user_config", return_value=None):
                devin.launch(
                    executable="/opt/hosts/devin", repo=repo, worktree=repo,
                    provider="devin", model=self.model,
                    provider_config=self.provider_config,
                    model_config=self.model_config, prompt="Do it",
                    mode="execute", capabilities=("shell",),
                    user_config_path=repo.parent / "missing.json",
                    existing_workspace=True,
                )
            run_root = repo / ".git" / worktrees.SIDE_LANE_RUNTIME_DIR
            run_dirs = list(run_root.glob("repo-*"))
            self.assertEqual(len(run_dirs), 1)
            policy = json.loads(
                (run_dirs[0] / "devin-command-policy.json").read_text(encoding="utf-8")
            )
            config_text = (run_dirs[0] / "devin-config.json").read_text(encoding="utf-8")
            config = json.loads(config_text)
        # Both controls carry the same declaration, each in its own host
        # grammar: the hook policy matches Bash rules (normalising `-C`), and
        # the native permission list takes Devin's `Exec(...)` prefix form.
        for rule in DENIED_RULES:
            self.assertIn(rule, policy["denied"])
        for verb in FORBIDDEN_VERBS:
            self.assertIn(f"Exec(git {verb})", config["permissions"]["deny"])
        # The policy hook is armed for the execute lane, and the worktree
        # anchors its `-C` normalisation.
        self.assertIn("devin_command_policy", config_text)
        self.assertEqual(policy["worktree"], str(repo))
        # The runtime landed in the repository, not beside the operator's
        # checkout: an owner-workspace run leaves the parent directory alone.
        self.assertFalse((repo.parent / ".side-lane-runtime").exists())

    def test_the_worker_is_told_the_same_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)],
                           check=True, capture_output=True)
            command = devin.build_command(
                executable="devin", repo=repo, worktree=repo, provider="devin",
                model=self.model, provider_config=self.provider_config,
                model_config=self.model_config, prompt="Do it",
                export_path=repo / "run" / "export.json",
                config_path=repo / "run" / "config.json", mode="execute",
                existing_workspace=True,
            )
        text = normalized(command[-1])
        self.assertIn("## Existing owner workspace", text)
        self.assertIn("Make no git write of any kind", text)
        self.assertIn("Codex carries the instruction only", text)
        self.assertIn("not a sandbox", text)


class CodexOwnerWorkspaceTests(unittest.TestCase):
    provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    model_config = {"runtime_model": "gpt-5.4-codex", "protocol": "native-codex"}

    def worktree(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /example\n", encoding="utf-8")
        return path

    def test_the_instruction_is_carried_without_claiming_a_deny(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            command = codex.build_codex_command(
                "codex", repo, lane, "openai", "gpt-5.4-codex", self.provider,
                self.model_config, "Do it", mode="execute", existing_workspace=True,
            )
        text = normalized(command[-1])
        self.assertIn("## Existing owner workspace", text)
        self.assertIn("Make no git write of any kind", text)
        # Honest about this host: the sandbox mode is unchanged, no per-command
        # permission rule is emitted, and the workspace contract is instruction
        # plus the runner's after-the-fact comparison — never prevention, which
        # the section itself says.
        self.assertIn("danger-full-access", command)
        self.assertNotIn("Bash(git commit", " ".join(command))
        self.assertIn("Codex carries the instruction only", text)
        self.assertIn(
            "does not promise that a shell which must stay arbitrary is also a "
            "sandbox", text)

    def test_the_workspace_lane_is_execute_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, lane = self.worktree(root, "repo"), self.worktree(root, "lane")
            with self.assertRaisesRegex(codex.CodexAdapterError, "execute mode"):
                codex.build_codex_command(
                    "codex", repo, lane, "openai", "gpt-5.4-codex", self.provider,
                    self.model_config, "Review it", mode="review",
                    existing_workspace=True,
                )


if __name__ == "__main__":
    unittest.main()
