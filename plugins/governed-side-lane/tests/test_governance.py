import json
from pathlib import Path
import tempfile
import unittest
import subprocess

from side_lane import cli, devin_command_policy
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

    def test_scratch_file_rule_renders_in_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for mode in ("review", "execute"):
                prompt = lane_system_prompt(mode, repo)
                self.assertIn(".side-lane-scratch/", prompt, mode)
                self.assertIn("outside the lane worktree", prompt, mode)
                self.assertIn("end a non-interactive session", prompt, mode)

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
                            provider_config, model_config = cli.select_route(
                                models, host, mode, provider_name, model
                            )
                            worktree = lane
                            if host == "codex":
                                command = codex.build_codex_command("codex", repo, worktree, provider_name, model, provider_config, model_config, "task", mode=mode)
                            else:
                                command = claude.build_command(executable="claude", repo=repo, worktree=worktree,
                                    provider=provider_name, model=model, provider_config=provider_config,
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

    def test_execute_prompt_guides_native_env_assignment_form(self) -> None:
        prompt = lane_system_prompt("execute", Path("/repo"))
        self.assertIn("env NAME=value <already-granted-command>", prompt)
        self.assertIn("underlying command", prompt)

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
        self.assertIn("Bash(curl *)", policy.allowed["shell"])
        self.assertEqual(policy.allowed["shell"], policy.allowed["workspace-write"])
        self.assertIn("Bash(git push *)", policy.allowed["git-push"])
        self.assertEqual(policy.denied["git-push"], ("Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)", "Bash(git push * -f*)", "Bash(git push --force-with-lease*)", "Bash(git push * --force-with-lease*)", "Bash(git push --mirror*)", "Bash(git push * --mirror*)", "Bash(git push +*)", "Bash(git push * +*)"))
        self.assertTrue(policy.capabilities <= known_capabilities())
        self.assertIn("mcp__gitnexus__list_repos", policy.allowed["gitnexus"])
        self.assertNotIn("mcp__gitnexus__rename", policy.allowed["gitnexus"])
        self.assertIn("mcp__codegraph__find_callers", policy.allowed["codegraph"])
        for rules in list(policy.allowed.values()) + [policy.always]:
            for rule in rules:
                self.assertNotRegex(rule, r"gcloud|deploy|iam|secret|force|Bash\(git merge(?: |\))")
        self.assertEqual(claude.allowed_tools("execute", ("shell",)), policy.always + policy.allowed["shell"])

    def test_curl_grant_is_execute_only_and_renders_to_devin(self) -> None:
        from side_lane.governance import tool_policy
        policy = tool_policy()
        self.assertIn("Bash(curl *)", policy.allowed["shell"])
        self.assertEqual(
            devin_command_policy.devin_exec_rule("Bash(curl *)"), "Exec(curl)"
        )
        self.assertEqual(claude.allowed_tools("review", ("shell",)), ())
        execute_prompt = lane_system_prompt("execute", Path("/repo"))
        self.assertIn("task-authorized", execute_prompt)
        self.assertIn("cloud metadata", execute_prompt)
        self.assertIn("not a URL filter", execute_prompt)
        self.assertNotIn("Bash(curl *)", lane_system_prompt("review", Path("/repo")))

    def test_terraform_benign_commands_render_and_siblings_stay_rejected(self) -> None:
        from side_lane.governance import tool_policy

        policy = tool_policy()
        allowed = policy.allowed["shell"]
        for rule in ("Bash(terraform fmt *)", "Bash(terraform validate *)", "Bash(terraform version)"):
            self.assertIn(rule, allowed)
            self.assertIn(rule, claude.allowed_tools("execute", ("shell",)))
        self.assertEqual(devin_command_policy.devin_exec_rule("Bash(terraform fmt *)"), "Exec(terraform fmt)")
        self.assertEqual(devin_command_policy.devin_exec_rule("Bash(terraform validate *)"), "Exec(terraform validate)")
        self.assertEqual(devin_command_policy.devin_exec_rule("Bash(terraform version)"), "Exec(terraform version)")
        for rule in ("Bash(terraform *)", "Bash(terraform init *)", "Bash(terraform plan *)",
                     "Bash(terraform apply *)", "Bash(terraform destroy *)", "Bash(terraform import *)",
                     "Bash(terraform state *)", "Bash(terraform deploy *)"):
            self.assertNotIn(rule, allowed)

    def test_slack_read_grants_exactly_the_two_read_only_slack_tools(self) -> None:
        from side_lane.governance import tool_policy
        policy = tool_policy()
        self.assertEqual(
            policy.allowed["slack-read"],
            ("WaitForMcpServers", "mcp__slack__slack_read_thread", "mcp__slack__slack_read_channel"),
        )
        # No other rule may touch the Slack server: no wildcard, no sending,
        # editing, search, files, or membership tool, in any capability.
        for rules in list(policy.allowed.values()) + list(policy.denied.values()) + [policy.always]:
            for rule in rules:
                if rule.startswith("mcp__slack__"):
                    self.assertIn(rule, policy.allowed["slack-read"])
        self.assertNotIn("slack-read", policy.denied)

    def test_cm_services_capabilities_grant_disjoint_exact_tool_sets(self) -> None:
        from side_lane.governance import tool_policy
        policy = tool_policy()
        self.assertEqual(
            policy.allowed["asana-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__asana_get_task",
                "mcp__cm-services__asana_get_project",
                "mcp__cm-services__asana_list_project_tasks",
            ),
        )
        self.assertEqual(
            policy.allowed["drive-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__drive_file_info",
                "mcp__cm-services__drive_sheet_tabs",
                "mcp__cm-services__drive_sheet_get",
                "mcp__cm-services__drive_doc_get",
            ),
        )
        # Disjoint grants on the shared server: no capability rule anywhere
        # touches cm-services outside these two sets — no wildcard and no
        # write tool.
        exact = (
            set(policy.allowed["asana-read"])
            | set(policy.allowed["drive-read"])
            | set(policy.allowed["gcloud-read"])
            | set(policy.allowed["database-read"])
            | set(policy.allowed["algolia-read"])
            | set(policy.allowed["contentful-read"])
            | set(policy.allowed["contentful-master-read"])
            | set(policy.allowed["gateway-read"])
        )
        for rules in list(policy.allowed.values()) + list(policy.denied.values()) + [policy.always]:
            for rule in rules:
                if rule.startswith("mcp__cm-services__"):
                    self.assertIn(rule, exact)
        self.assertFalse(any(rule.endswith("*") for rule in exact - {"WaitForMcpServers"}))
        self.assertNotIn("asana-read", policy.denied)
        self.assertNotIn("drive-read", policy.denied)
        self.assertEqual(
            policy.allowed["gcloud-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__gcp_logs",
                "mcp__cm-services__gcp_run_services",
                "mcp__cm-services__gcp_run_jobs",
                "mcp__cm-services__gcp_run_job",
                "mcp__cm-services__gcp_scheduler_jobs",
                "mcp__cm-services__gcp_functions",
                "mcp__cm-services__gcp_billing_mtd",
                "mcp__cm-services__gcp_billing_daily",
                "mcp__cm-services__gcp_menu",
            ),
        )
        self.assertEqual(
            policy.allowed["database-read"],
            ("WaitForMcpServers", "mcp__cm-services__postgres_select"),
        )
        self.assertEqual(
            policy.allowed["algolia-read"],
            ("WaitForMcpServers", "mcp__cm-services__algolia_get_settings"),
        )
        self.assertEqual(
            policy.allowed["contentful-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__contentful_get_entry",
                "mcp__cm-services__contentful_search_entries",
            ),
        )
        self.assertEqual(
            policy.allowed["contentful-master-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__contentful_master_get_entry",
                "mcp__cm-services__contentful_master_search_entries",
            ),
        )
        self.assertEqual(
            policy.allowed["gateway-read"],
            (
                "WaitForMcpServers",
                "mcp__cm-services__gateway_run_status",
                "mcp__cm-services__gateway_run_report",
            ),
        )

    def test_gcloud_run_job_is_distinct_from_the_plural_execution_listing(self) -> None:
        """The canonical record separates the two Cloud Run job reads.

        ``gcp_run_job`` (singular) is job metadata for one named job; the
        plural ``gcp_run_jobs`` stays the execution listing. The document must
        state the read/argument shape and the exclusions, and both tool IDs
        must remain exactly enumerated — the singular name is a prefix of the
        plural, so the allowlist entries are compared as exact elements.
        """
        from side_lane.governance import tool_policy

        rules = tool_policy().allowed["gcloud-read"]
        self.assertIn("mcp__cm-services__gcp_run_job", rules)
        self.assertIn("mcp__cm-services__gcp_run_jobs", rules)
        self.assertFalse(any(rule.endswith("*") for rule in rules))
        # The source wraps prose at a column, so compare on collapsed
        # whitespace rather than against a line break in the document.
        prompt = " ".join(lane_system_prompt("execute", Path("/tmp/repo")).split())
        for phrase in (
            "`gcp_run_job` reads the metadata of one Cloud Run job",
            "job shortname",
            "container images",
            "condition state and reason",
            "latest execution",
            "execution count",
            "never returns the full job spec",
            "environment variables",
            "free-text condition messages",
            "`gcp_run_jobs`, which remains the execution listing",
        ):
            self.assertIn(phrase, prompt)

    def test_gateway_read_names_no_deployment_url_or_credential(self) -> None:
        """The Gateway grant is a capability boundary, not a deployment handle.

        The server-side ``gateway_run_ids`` allowlist and the GET-only
        status/report endpoints belong to the coordinator-provisioned server;
        the public core records only exact tool IDs, so no endpoint URL,
        account name, or credential argument may appear in the rule set.
        """
        from side_lane.governance import tool_policy
        rules = tool_policy().allowed["gateway-read"]
        for rule in rules:
            for forbidden in ("://", "http", "Bearer", "token", "gateway_run_ids="):
                self.assertNotIn(forbidden, rule)
        self.assertFalse(any(rule.endswith("*") for rule in rules))
        self.assertIn("gateway_run_ids", lane_system_prompt("execute", Path("/tmp/repo")))

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
            path.write_text(base.replace("- `Bash(git push * -f*)`", "- Bash(git push * -f*)"), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "malformed line"):
                tool_policy(path)
            path.write_text(base + "\n### always\n\n- `Bash(*)`\n", encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "more than once"):
                tool_policy(path)
            path.write_text(base.split("## Execute tool allowlist")[0], encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "missing sections"):
                tool_policy(path)


class NegatedLinkageTests(LinkageWordingTests):
    def test_negated_declarations_are_not_linkage(self) -> None:
        for text in ("[CLAUDE.md](./CLAUDE.md) is not the source of truth.\n",
                     "[CLAUDE.md](./CLAUDE.md) is no longer the source of truth.\n",
                     "You must never treat [CLAUDE.md](./CLAUDE.md) as authoritative.\n",
                     "You must not treat [CLAUDE.md](./CLAUDE.md) as authoritative.\n",
                     "You should not consider [CLAUDE.md](./CLAUDE.md) the source of truth.\n",
                     "Do not read [CLAUDE.md](./CLAUDE.md) as the authoritative source of truth.\n",
                     "[CLAUDE.md](./CLAUDE.md) cannot be treated as authoritative.\n",
                     "[CLAUDE.md](./CLAUDE.md) is not currently the source of truth.\n",
                     "You must under no circumstances treat [CLAUDE.md](./CLAUDE.md) as authoritative.\n",
                     "In no case is [CLAUDE.md](./CLAUDE.md) the source of truth.\n",
                     "[CLAUDE.md](./CLAUDE.md) is not really the authoritative file.\n",
                     "You can't rely on [CLAUDE.md](./CLAUDE.md) as the source of truth.\n"):
            repo = self.governed(text)
            with self.assertRaisesRegex(GovernanceError, "unambiguously"):
                validate_repository(repo)

    def test_affirmative_claim_with_trailing_negation_is_linkage(self) -> None:
        repo = self.governed("[CLAUDE.md](./CLAUDE.md) is the source of truth, not this file.\n")
        self.assertEqual(validate_repository(repo), repo.resolve())
        for text in ("You must read [CLAUDE.md](./CLAUDE.md); it is authoritative and is not optional.\n",
                     "You must read [CLAUDE.md](./CLAUDE.md); it is not optional and is authoritative.\n",
                     "You must read [CLAUDE.md](./CLAUDE.md); it is not only authoritative but required.\n",
                     "[CLAUDE.md](./CLAUDE.md) is the source of truth; do not skip it.\n"):
            repo = self.governed(text)
            self.assertEqual(validate_repository(repo), repo.resolve(), text)

    def test_known_capabilities_rejects_non_object_allowlist(self) -> None:
        from side_lane.governance import known_capabilities
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for bad in ("[]", '{"capabilities": "shell"}', '{"capabilities": ["", "x"]}', "not json"):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(GovernanceError):
                    known_capabilities(path)

    def test_adapter_type_aliases_are_real_types(self) -> None:
        import collections.abc
        import sys
        import typing
        self.assertIs(typing.get_origin(claude.Capabilities), typing.Union)
        self.assertIs(typing.get_origin(claude.Runner), collections.abc.Callable)
        self.assertEqual(claude.launch.__annotations__["runner"], "Runner")
        if sys.version_info >= (3, 10):  # PEP 604 unions in other annotations need 3.10+
            hints = typing.get_type_hints(claude.launch)
            self.assertIn("runner", hints)
            self.assertIn("capabilities", hints)


class ReportDeliverableContractTests(unittest.TestCase):
    """The canonical report contract: one override, delivered on every host."""

    def test_report_rules_are_a_reserved_non_capability_bucket(self) -> None:
        from side_lane.governance import known_capabilities, tool_policy
        policy = tool_policy()
        self.assertTrue(policy.report_denied)
        # Not a capability: nothing can be granted it, so it must not appear in
        # the capability set the runtime allowlist is checked against.
        self.assertNotIn("report-deliverable", policy.capabilities)
        self.assertTrue(policy.capabilities <= known_capabilities())
        for rule in ("Bash(git add *)", "Bash(git commit)", "Bash(git commit *)",
                     "Bash(git push)", "Bash(git push *)", "Bash(git merge *)",
                     "Bash(git reset)", "Bash(git stash *)",
                     "Bash(git fetch)", "Bash(git fetch *)"):
            self.assertIn(rule, policy.report_denied)
        # The report rules do overlap the shell grants (`git add`, `git commit`
        # and `git stash` are ordinary work): an ordinary execute lane keeps
        # them, and in a report lane the deny seam is what wins.
        self.assertIn("Bash(git commit *)", policy.allowed["shell"])
        self.assertEqual(
            claude.disallowed_tools("execute", ("shell",), report_deliverable=True),
            claude.disallowed_tools("execute", ("shell",)) + policy.report_denied,
        )

    def test_report_prompt_drops_both_write_grants(self) -> None:
        from side_lane.governance import EXECUTE_GIT_GRANT
        plain = lane_system_prompt("execute", Path("/repo"))
        report = lane_system_prompt("execute", Path("/repo"), report_deliverable=True)
        self.assertIn(EXECUTE_GIT_GRANT, plain)
        body = report.split("## Report deliverable")[0]
        self.assertNotIn(EXECUTE_GIT_GRANT, body)
        self.assertIn("## Report deliverable", report)
        self.assertIn("SIDE_LANE_REPORT.md", report)
        self.assertIn(".side-lane-scratch/", report)
        # The remainder of execute mode still applies verbatim.
        self.assertIn("## Active mode: Execute mode", body)
        self.assertIn("- Work only in the dedicated side-lane worktree and assigned lane branch.", body)
        self.assertIn("- Database access is read-only", body)
        self.assertIn("open pull requests", body)

    def test_report_prompt_drops_the_workflow_write_grant_too(self) -> None:
        from side_lane.governance import EXECUTE_GIT_GRANT, EXECUTE_WORKFLOW_GRANT
        # Both grants are compared whitespace-normalized: the workflow bullet
        # wraps in the canonical source, so only its normalized text is a
        # substring of the render.
        plain = " ".join(lane_system_prompt("execute", Path("/repo")).split())
        report = " ".join(lane_system_prompt(
            "execute", Path("/repo"), report_deliverable=True).split())
        # Both explicit write grants are ordinary execute rules ...
        self.assertIn(EXECUTE_GIT_GRANT, plain)
        self.assertIn(EXECUTE_WORKFLOW_GRANT, plain)
        body = report.split("## Report deliverable")[0]
        self.assertNotIn(EXECUTE_GIT_GRANT, body)
        # ... and neither reaches a report lane: the report artifact is the
        # deliverable, so the task-scoped workflow/messaging exemption, whose
        # only grant is that bullet, is not granted either.
        self.assertNotIn(EXECUTE_WORKFLOW_GRANT, body)
        self.assertNotIn("workflow or messaging write", body)
        self.assertIn("- Database access is read-only", body)

    def test_removed_grants_are_matched_as_whole_bullets_however_wrapped(self) -> None:
        """A reflowed bullet is still one bullet: matching normalizes whitespace.

        The grant is identified by the *complete* bullet — its continuation
        lines included — so the canonical file may re-wrap it without the
        override silently leaving the grant in a report lane's contract.
        """
        from side_lane.governance import EXECUTE_GIT_GRANT, EXECUTE_WORKFLOW_GRANT
        base = (ROOT / "config/lane-governance.md").read_text(encoding="utf-8")
        commit_source = "- " + EXECUTE_GIT_GRANT + "\n"
        workflow_source = (
            "- A workflow or messaging write is allowed only when the approved task names\n"
            "  that exact update and recipient or object. Make only that update through the\n"
            "  selected worker host's connector and report exactly what changed.\n"
        )
        self.assertIn(commit_source, base)
        self.assertIn(workflow_source, base)
        wrapped = base.replace(
            commit_source,
            "- You may inspect, edit, test, commit, and push only the\n"
            "  assigned lane branch.\n",
        ).replace(
            workflow_source,
            "- A workflow or messaging write is allowed only when the approved\n"
            "  task names that exact update and recipient or object. Make only\n"
            "  that update through the selected worker host's connector and\n"
            "  report exactly what changed.\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            path.write_text(wrapped, encoding="utf-8")
            report = " ".join(lane_system_prompt(
                "execute", Path("/repo"), report_deliverable=True, path=path).split())
            plain = " ".join(lane_system_prompt(
                "execute", Path("/repo"), path=path).split())
        # The ordinary lane still renders both grants, wrapped as they are ...
        self.assertIn(
            "You may inspect, edit, test, commit, and push only the assigned lane branch.",
            plain)
        self.assertIn(
            "A workflow or messaging write is allowed only when the approved task names "
            "that exact update and recipient or object. Make only that update through "
            "the selected worker host's connector and report exactly what changed.",
            plain)
        # ... and a report lane still drops them, continuation lines and all,
        # while every unrelated execute rule survives.
        self.assertNotIn("commit, and push only the", report)
        self.assertNotIn("A workflow or messaging write", report)
        self.assertIn("Work only in the dedicated side-lane worktree", report)
        self.assertIn("Database access is read-only", report)
        self.assertIn("Stop and report when an action exceeds these boundaries", report)
        self.assertIn("This grant is execute-only: it", report)

    def test_a_missing_or_duplicated_grant_bullet_fails_closed(self) -> None:
        """Zero or duplicate expected grants are errors, never a silent drop."""
        from side_lane.governance import EXECUTE_GIT_GRANT, EXECUTE_WORKFLOW_GRANT
        base = (ROOT / "config/lane-governance.md").read_text(encoding="utf-8")
        commit_source = "- " + EXECUTE_GIT_GRANT + "\n"
        workflow_source = (
            "- A workflow or messaging write is allowed only when the approved task names\n"
            "  that exact update and recipient or object. Make only that update through the\n"
            "  selected worker host's connector and report exactly what changed.\n"
        )
        self.assertIn(commit_source, base)
        self.assertIn(workflow_source, base)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            for label, text, pattern in (
                ("workflow grant missing",
                 base.replace(workflow_source, ""), "workflow/messaging grant"),
                ("workflow grant duplicated",
                 base.replace(workflow_source, workflow_source + workflow_source),
                 "workflow/messaging grant"),
                ("commit grant duplicated",
                 base.replace(commit_source, commit_source + commit_source),
                 "commit grant"),
            ):
                with self.subTest(case=label):
                    path.write_text(text, encoding="utf-8")
                    # An ordinary execute lane is untouched by the override and
                    # still renders, whatever the canonical file now says.
                    self.assertIn(EXECUTE_GIT_GRANT, lane_system_prompt(
                        "execute", Path("/repo"), path=path))
                    with self.assertRaisesRegex(GovernanceError, pattern):
                        lane_system_prompt("execute", Path("/repo"),
                                           report_deliverable=True, path=path)

    def test_report_bullet_ends_before_separate_indented_text(self) -> None:
        from side_lane.governance import (
            EXECUTE_GIT_GRANT, EXECUTE_WORKFLOW_GRANT, _report_execute_body,
        )
        for separator in ("\n", "unindented paragraph\n"):
            with self.subTest(separator=separator):
                unrelated = separator + "  separate indented paragraph\n"
                body = ("- " + EXECUTE_GIT_GRANT + "\n" + unrelated
                        + "- " + EXECUTE_WORKFLOW_GRANT + "\n")
                result = _report_execute_body(body)
                self.assertIn("  separate indented paragraph", result)
                self.assertNotIn(EXECUTE_GIT_GRANT, result)
                self.assertNotIn(EXECUTE_WORKFLOW_GRANT, result)

    def test_report_lanes_refuse_the_explicit_write_capabilities(self) -> None:
        """The refusal list is exactly the write grants, and only those.

        A report lane's deliverable is its report artifact, so the capabilities
        that carry explicit write authority are refused before a worker starts.
        Read capabilities and `workspace-write` — the report artifact's own
        grant, and the lane's tooling — stay available.
        """
        from side_lane.governance import (
            known_capabilities,
            report_forbidden_write_capabilities,
            report_write_capability_conflicts,
        )
        # A tuple, not a set: the canonical line's own order is the reported
        # order, so the refusals stay comparable across the doc and the code.
        self.assertEqual(report_forbidden_write_capabilities(),
                         ("git-push", "workflow-write"))
        self.assertLessEqual(set(report_forbidden_write_capabilities()),
                             known_capabilities())
        self.assertNotIn("workspace-write", report_forbidden_write_capabilities())
        self.assertEqual(
            report_write_capability_conflicts(
                ("shell", "workspace-write", "gcloud-read", "playwright")), ())
        # Canonical order, whatever order the caller names them in.
        self.assertEqual(
            report_write_capability_conflicts(("workflow-write", "shell", "git-push")),
            ("git-push", "workflow-write"))

    def test_report_capability_refusals_derive_from_the_canonical_declaration(self) -> None:
        """The refused names are read from the document, not owned by Python.

        The canonical `Report deliverable` section carries one machine-readable
        declaration line, and every reader parses it: an edited-but-valid
        declaration re-derives the refusal set, and a missing, malformed,
        repeated, or duplicated one fails closed rather than leaving the
        refusals silently narrowed to nothing or widened to something the
        document never named.
        """
        from side_lane.governance import (
            report_forbidden_write_capabilities,
            report_write_capability_conflicts,
        )
        base = (ROOT / "config/lane-governance.md").read_text(encoding="utf-8")
        line = "Report forbidden write capabilities: `git-push`, `workflow-write`\n"
        self.assertIn(line, base)
        self.assertEqual(report_forbidden_write_capabilities(),
                         ("git-push", "workflow-write"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            # A valid declaration naming something else proves derivation: the
            # refusals follow the document, not a code-side list.
            path.write_text(
                base.replace(
                    line, "Report forbidden write capabilities: `workflow-write`\n"),
                encoding="utf-8")
            self.assertEqual(report_forbidden_write_capabilities(path),
                             ("workflow-write",))
            self.assertEqual(
                report_write_capability_conflicts(
                    ("shell", "git-push", "workflow-write", "workspace-write"), path),
                ("workflow-write",))
            self.assertEqual(report_write_capability_conflicts(("git-push",), path), ())
            for label, text, pattern in (
                ("missing line", base.replace(line, ""), "exactly one line"),
                ("duplicated line", base.replace(line, line + line),
                 "exactly one line"),
                ("no name", base.replace(
                    line, "Report forbidden write capabilities:\n"),
                 "declares no report forbidden write capability"),
                ("unbackticked entry", base.replace(
                    line, "Report forbidden write capabilities: git-push\n"),
                 "malformed entry"),
                ("non-identifier name", base.replace(
                    line, "Report forbidden write capabilities: `Git Push`\n"),
                 "invalid capability name"),
                ("repeated name", base.replace(
                    line,
                    "Report forbidden write capabilities: `git-push`, `git-push`\n"),
                 "duplicate capability name"),
            ):
                with self.subTest(declaration=label):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(GovernanceError, pattern):
                        report_forbidden_write_capabilities(path)

    def test_report_section_states_the_per_host_enforcement_honestly(self) -> None:
        report = " ".join(
            lane_system_prompt("execute", Path("/repo"), report_deliverable=True).split())
        # Named seams where they exist, and no claim of an OS sandbox.
        self.assertIn("`report-deliverable (denied)` rules", report)
        self.assertIn("no deny seam", report)
        self.assertIn("after-the-fact verification", report)
        self.assertIn("not a sandbox", report)
        # The priority claim is narrowed to conflicting repository commit
        # conventions: no blanket override, and no claim about the ordering a
        # host decides for itself or about anything above this instruction.
        self.assertIn("conflicting repository commit convention", report)
        self.assertIn(
            "not a claim of precedence over a higher-priority security or system "
            "instruction", report)
        self.assertNotIn("outranks every repository instruction file", report)
        # The Codex note stays an observation of one installed version rather
        # than proof established for every version or for the cloud service.
        self.assertIn("observation of the installed version", report)
        # The parser limits are named rather than papered over.
        self.assertIn("`git -C <path> ...`", report)
        self.assertIn("compound invocation", report)
        # The canonical declaration of refused capabilities is part of the
        # section, so it reaches the worker alongside the rule it states.
        self.assertIn(
            "Report forbidden write capabilities: `git-push`, `workflow-write`",
            report)

    def test_report_contract_is_execute_only(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "execute mode only"):
            lane_system_prompt("review", Path("/repo"), report_deliverable=True)
        self.assertNotIn("## Report deliverable", lane_system_prompt("review", Path("/repo")))

    def test_report_contract_fails_closed_when_the_source_moves(self) -> None:
        from side_lane.governance import tool_policy
        base = (ROOT / "config/lane-governance.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gov.md"
            # A missing section, a missing reserved bucket, and a reworded
            # commit grant each fail closed rather than shipping a lane whose
            # contract was silently dropped.
            path.write_text(
                base.replace("## Report deliverable\n", "## Report notes\n"), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "missing sections"):
                tool_policy(path)
            head, _, tail = base.partition("### report-deliverable (denied)\n")
            path.write_text(head + "### " + tail.split("### ", 1)[1], encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "report-deliverable"):
                tool_policy(path)
            path.write_text(
                base.replace("commit, and push only the assigned lane branch",
                             "commit and push as needed"), encoding="utf-8")
            with self.assertRaisesRegex(GovernanceError, "commit grant"):
                lane_system_prompt("execute", Path("/repo"),
                                   report_deliverable=True, path=path)

    def test_report_deliverable_does_not_widen_the_artifact_namespace(self) -> None:
        # The approved scope adds no broad root artifact exemption: the one
        # deliverable path is a fixed name and the browser-report namespace and
        # its caps are untouched by this contract.
        from side_lane import cli
        table = json.loads(
            (ROOT / "tests/fixtures/report_artifact_names.json").read_text(encoding="utf-8"))
        self.assertEqual(cli.BROWSER_REPORT_ARTIFACT_RE.pattern, table["pattern"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_FILES, table["max_files"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_BYTES, table["max_bytes"])
        self.assertEqual(cli.BROWSER_ARTIFACT_MAX_TOTAL_BYTES, table["max_total_bytes"])
        self.assertEqual(cli.REPORT_ONLY_SCRATCH_PREFIX, ".side-lane-scratch/")
        self.assertNotIn("SIDE_LANE_REPORT", cli.REPORT_ONLY_SCRATCH_PREFIX)


if __name__ == "__main__":
    unittest.main()
