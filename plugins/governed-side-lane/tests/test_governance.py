import json
from pathlib import Path
import tempfile
import unittest
import subprocess

from side_lane.governance import GovernanceError, lane_system_prompt, validate_repository
from side_lane.adapters import claude, codex


ROOT = Path(__file__).parents[1]


class GovernanceParityTests(unittest.TestCase):
    def test_every_configured_route_uses_one_canonical_renderer(self) -> None:
        models = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prompts = {mode: lane_system_prompt(mode, repo) for mode in ("review", "execute")}
        for provider in models["providers"].values():
            for mode, hosts in provider["routes"].items():
                for route in hosts.values():
                    for _model in route["models"]:
                        self.assertIn("Injected canonical side-lane governance", prompts[mode])
                        self.assertNotIn("INPROCESS.md", prompts[mode])
        self.assertIn("do not edit", prompts["review"].lower())
        self.assertIn("open pull requests", prompts["execute"])

    def test_every_allowlisted_command_contains_its_canonical_mode_prompt(self) -> None:
        models = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").mkdir()
            for provider_name, provider in models["providers"].items():
                for mode, hosts in provider["routes"].items():
                    for host, route in hosts.items():
                        for model in route["models"]:
                            model_config = {"runtime_model": model, "protocol": route["protocol"]}
                            worktree = lane
                            if host == "codex":
                                command = codex.build_codex_command("codex", repo, worktree, provider_name, model, provider, model_config, "task", mode=mode)
                            else:
                                command = claude.build_command(executable="claude", repo=repo, worktree=worktree,
                                    provider=provider_name, model=model, provider_config=provider,
                                    model_config=model_config, prompt="task", mode=mode)
                            rendered = "\n".join(command)
                            self.assertIn(lane_system_prompt(mode, repo), rendered)

    def test_adapters_do_not_carry_drifting_governance_copies(self) -> None:
        for relative in ("side_lane/cli.py", "side_lane/adapters/codex.py", "side_lane/adapters/claude.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("REVIEW_PROMPT", text)
            self.assertNotIn("EXECUTE_PROMPT", text)
            self.assertNotIn("Bash(", text, f"{relative} copies allowlist rules instead of loading governance")
        self.assertTrue((ROOT / "config/lane-governance.md").is_file())
        skill = (ROOT / "skills/side-lane/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("Bash(", skill)
        self.assertIn("Execute tool allowlist", skill)

    def test_direct_session_entrypoint_marks_private_memory_non_authoritative(self) -> None:
        text = (ROOT / "config/agent-context.md").read_text(encoding="utf-8")
        for item in ("AGENTS.md", "CLAUDE.md", "open pull requests", "Codex product memory", "Claude Code auto-memory", "not authoritative"):
            self.assertIn(item, text)
        self.assertNotIn("INPROCESS.md", text)

    def governed_repo(self, root: Path) -> Path:
        repo = root / "repo"
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        (repo / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text(
            "You must read [the rules](./CLAUDE.md); they are the authoritative source of truth.\n",
            encoding="utf-8",
        )
        return repo

    def test_repository_validation_uses_git_root_and_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.governed_repo(Path(directory))
            self.assertEqual(validate_repository(repo), repo.resolve())
            nested = repo / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(GovernanceError, "repository root"):
                validate_repository(nested)

    def test_repository_validation_rejects_detached_authority_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.governed_repo(Path(directory))
            (repo / "AGENTS.md").write_text(
                "CLAUDE.md is required and authoritative.\n[the rules](./CLAUDE.md)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "link unambiguously"):
                validate_repository(repo)
            target = repo / "AGENTS.real.md"
            target.write_text(
                "You must read [rules](./CLAUDE.md); authoritative source of truth.\n",
                encoding="utf-8",
            )
            (repo / "AGENTS.md").unlink()
            (repo / "AGENTS.md").symlink_to(target)
            with self.assertRaisesRegex(GovernanceError, "regular repository file"):
                validate_repository(repo)

    def test_repository_validation_linkage_repetition_and_foreign_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.governed_repo(Path(directory))
            (repo / "AGENTS.md").write_text(
                "You must read [the rules](./CLAUDE.md); they are the authoritative source of truth.\n"
                "Reminder: [CLAUDE.md](CLAUDE.md) is required and authoritative.\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_repository(repo), repo.resolve())
            (repo / "OTHER.md").write_text("# Other\n", encoding="utf-8")
            (repo / "AGENTS.md").write_text(
                "You must read [the rules](./CLAUDE.md); they are the authoritative source of truth.\n"
                "You must also read [other](./OTHER.md); it is authoritative too.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "link unambiguously"):
                validate_repository(repo)
            (repo / "AGENTS.md").write_text("CLAUDE.md matters.\n", encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "e.g."):
                validate_repository(repo)



class LinkageWordingTests(unittest.TestCase):
    def governed(self, agents_text: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        (repo / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
        (repo / "AGENTS.md").write_text(agents_text, encoding="utf-8")
        return repo

    def test_bold_backticked_source_of_truth_line_is_accepted(self) -> None:
        repo = self.governed(
            "# AGENTS.md\n\n**[`CLAUDE.md`](./CLAUDE.md) is the source of truth for this repo's rules.**\n"
        )
        self.assertEqual(validate_repository(repo), repo.resolve())

    def test_source_of_truth_line_still_needs_a_link_to_root_claude_md(self) -> None:
        repo = self.governed("CLAUDE.md is the source of truth.\n[the rules](./OTHER.md)\n")
        with self.assertRaisesRegex(GovernanceError, "unambiguously"):
            validate_repository(repo)
        repo = self.governed("[`CLAUDE.md`](./docs/CLAUDE.md) is the source of truth.\n")
        with self.assertRaisesRegex(GovernanceError, "unambiguously"):
            validate_repository(repo)

    def test_authoritative_alone_without_requirement_word_is_still_rejected(self) -> None:
        repo = self.governed("[CLAUDE.md](./CLAUDE.md) is authoritative.\n")
        with self.assertRaisesRegex(GovernanceError, "unambiguously"):
            validate_repository(repo)


class ToolPolicyTests(unittest.TestCase):
    def test_canonical_allowlist_parses_and_drives_the_adapter(self) -> None:
        from side_lane.governance import known_capabilities, tool_policy
        policy = tool_policy()
        self.assertEqual(policy.always[:5], ("Read", "Edit", "Write", "Glob", "Grep"))
        self.assertIn("Bash(pnpm *)", policy.allowed["shell"])
        self.assertEqual(policy.allowed["shell"], policy.allowed["workspace-write"])
        self.assertIn("Bash(git push *)", policy.allowed["git-push"])
        self.assertEqual(policy.denied["git-push"], ("Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)"))
        self.assertTrue(policy.capabilities <= known_capabilities())
        for rules in list(policy.allowed.values()) + [policy.always]:
            for rule in rules:
                self.assertNotRegex(rule, r"gcloud|deploy|merge|iam|secret|force")
        self.assertEqual(claude.allowed_tools("execute", ("shell",)), policy.always + policy.allowed["shell"])

    def test_malformed_allowlist_fails_closed(self) -> None:
        from side_lane.governance import tool_policy
        base = (ROOT / "config/lane-governance.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            path.write_text(base.replace("### always\n", "### sometimes\n"), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "always"):
                tool_policy(path)
            path.write_text(base + "\n### Shell Bad\n\n- `Bash(x)`\n", encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "invalid capability name"):
                tool_policy(path)
            path.write_text(base.split("## Execute tool allowlist")[0], encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "missing sections"):
                tool_policy(path)


class NegatedLinkageTests(LinkageWordingTests):
    def test_negated_declarations_are_not_linkage(self) -> None:
        for text in ("[CLAUDE.md](./CLAUDE.md) is not the source of truth.\n",
                     "[CLAUDE.md](./CLAUDE.md) is no longer the source of truth.\n",
                     "You must never treat [CLAUDE.md](./CLAUDE.md) as authoritative.\n"):
            repo = self.governed(text)
            with self.assertRaisesRegex(GovernanceError, "unambiguously"):
                validate_repository(repo)


if __name__ == "__main__":
    unittest.main()
