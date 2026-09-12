from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from side_lane.adapters import claude


class ClaudeAdapterTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    glm = {"gateway": "direct-zai", "auth_method": "provider-key", "billable": True, "base_url": "https://api.z.ai/api/anthropic"}

    def test_bounded_process_accepts_subprocess_run_capture_kwargs(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c", "print('captured')"], timeout=5,
            capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "captured")
        self.assertEqual(completed.stderr, "")

    def test_bounded_process_timeout_preserves_real_partial_output(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(10)"],
            timeout=0.1, capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertIn("partial", completed.stdout)
        self.assertIn("process group stopped", completed.stderr)

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        return path

    def test_native_review_is_strict_no_mcp_and_execute_retains_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            review = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude-readonly"}, prompt="review", mode="review")
            execute = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}, prompt="task")
        self.assertIn("--safe-mode", review)
        self.assertNotIn("--bare", review)
        self.assertIn("Read,Glob,Grep", review)
        self.assertIn('{"mcpServers":{}}', review)
        self.assertIn("acceptEdits", execute)
        self.assertIn("user,project,local", execute)
        self.assertIn("Injected canonical side-lane governance", execute[-1])

    def test_native_environment_scrubs_keys_and_rejects_secret(self) -> None:
        config = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        child = claude.build_transport_environment({"PATH": "/bin", "ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y"},
            provider="claude", model="claude-sonnet-5", provider_config=self.native, model_config=config, mode="execute")
        self.assertEqual(child, {"PATH": "/bin"})
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "must not receive"):
            claude.build_transport_environment({}, provider="claude", model="claude-sonnet-5",
                provider_config=self.native, model_config=config, mode="execute", secret="never")

    def test_native_environment_scrubs_backend_selectors(self) -> None:
        child = claude.scrub_environment({
            "PATH": "/bin", "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999999",
        })
        self.assertEqual(child, {"PATH": "/bin"})

    def test_direct_candidate_requires_identity_contract_and_pins_nonsecret_settings(self) -> None:
        direct = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                  "base_url": "https://api.kimi.com/coding/"}
        config = {"runtime_model": "k3-256k", "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": "k3-256k", "resolved_model": "k3-256k",
                                        "settings_precedence": "verified"}, "reasoning_effort": "high",
                  "max_budget_usd": 2}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            command = claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="kimi", model="k3-256k", provider_config=direct, model_config=config,
                prompt="task", mode="execute")
        settings = command[command.index("--settings") + 1]
        self.assertIn('"enabled":false', settings)
        self.assertIn('"CLAUDE_CODE_SUBAGENT_MODEL":"k3-256k"', settings)
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.0")
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "identity contract"):
            claude.build_transport_environment({}, provider="kimi", model="k3-256k",
                provider_config=direct, model_config={"runtime_model": "k3-256k", "protocol": "anthropic-compatible"},
                mode="execute", secret="selected")
        wrong_endpoint = {**direct, "base_url": "http://api.moonshot.ai/anthropic"}
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "endpoint"):
            claude.build_transport_environment({}, provider="kimi", model="k3-256k",
                provider_config=wrong_endpoint, model_config=config, mode="execute", secret="selected")
        runner = mock.Mock()
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "transport qualification"):
            claude.launch(executable="claude", repo="/not-used", worktree="/not-used", provider="kimi",
                model="k3-256k", provider_config=direct, model_config=config, prompt="task",
                secret="selected", runner=runner)
        runner.assert_not_called()

    def test_glm_requires_explicit_secret_and_uses_direct_gateway(self) -> None:
        config = {"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"}
        with self.assertRaisesRegex(claude.ClaudeAdapterError, "credential is absent"):
            claude.build_transport_environment({}, provider="glm", model="glm-5.3", provider_config=self.glm, model_config=config, mode="execute")
        child = claude.build_transport_environment({"OPENROUTER_API_KEY": "old"}, provider="glm",
            model="glm-5.3", provider_config=self.glm, model_config=config, mode="execute", secret="selected")
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "selected")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertNotIn("OPENROUTER_API_KEY", child)

    def test_mocked_launch_redacts_billable_secret_and_normalizes_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 7, "selected", "selected"))
            result = claude.launch(executable="claude", repo=repo, worktree=lane, provider="glm",
                model="glm-5.3", provider_config=self.glm,
                model_config={"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"},
                prompt="task", secret="selected", runner=runner)
        self.assertEqual(result.gateway, "direct-zai")
        self.assertTrue(result.billable)
        self.assertNotIn("selected", result.stdout + result.stderr)

    def test_qualified_direct_launch_captures_stream_identity_and_usage(self) -> None:
        model = "kimi-k2.7-code"
        provider = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                    "base_url": "https://api.moonshot.cn/anthropic"}
        config = {"runtime_model": model, "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": model, "resolved_model": model,
                                        "settings_precedence": "verified"},
                  "qualification": {"verified": True, "verified_on": "2026-09-11",
                                    "source": "mocked transport report"}}
        stdout = '\n'.join((json.dumps({"type": "system", "model": model}),
                            json.dumps({"type": "result", "usage": {"input_tokens": 9}})))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="kimi", model=model, provider_config=provider, model_config=config,
                prompt="task", secret="selected",
                runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")))
        self.assertEqual(result.resolved_model, model)
        self.assertEqual(result.usage, {"input_tokens": 9})
        self.assertIn("stream-json", result.argv)

    def test_qualified_direct_launch_fails_closed_on_unattested_or_wrong_identity(self) -> None:
        model = "kimi-k2.7-code"
        provider = {"gateway": "direct-kimi", "auth_method": "provider-key", "billable": True,
                    "base_url": "https://api.moonshot.cn/anthropic"}
        config = {"runtime_model": model, "protocol": "anthropic-compatible",
                  "identity_contract": {"requested_model": model, "resolved_model": model,
                                        "settings_precedence": "verified"},
                  "qualification": {"verified": True, "verified_on": "2026-09-11",
                                    "source": "mocked transport report"}}
        cases = ((json.dumps({"type": "result", "usage": {"input_tokens": 1}}),
                  "identity-unverified"),
                 (json.dumps({"type": "system", "model": "another-model"}),
                  "identity-mismatch"),
                 ("\n".join((json.dumps({"model": model}),
                              json.dumps({"model": "another-model"}))),
                  "identity-unverified"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            for stdout, error in cases:
                result = claude.launch(executable="claude", repo=repo, worktree=lane,
                    provider="kimi", model=model, provider_config=provider, model_config=config,
                    prompt="task", secret="selected",
                    runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")))
                self.assertEqual(result.returncode, 65)
                self.assertIn(error, result.stderr)

    def test_native_route_without_identity_contract_preserves_legacy_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude-readonly"},
                prompt="review", mode="review",
                runner=mock.Mock(return_value=subprocess.CompletedProcess([], 0, "review complete", "")))
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.resolved_model)

    def test_partial_stream_usage_deduplicates_messages_and_final_result_wins(self) -> None:
        partial = "\n".join((
            json.dumps({"message": {"id": "a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 3}}}),
            json.dumps({"message": {"id": "a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 7}}}),
            json.dumps({"message": {"id": "b", "usage": {"input_tokens": 4, "output_tokens": 2}}}),
        ))
        self.assertEqual(claude._stream_metadata(partial)[1], {
            "observation": "partial-stream", "input_tokens": 14,
            "cache_read_input_tokens": 7, "output_tokens": 2,
        })
        complete = partial + "\n" + json.dumps({"type": "result", "usage": {"input_tokens": 99}})
        self.assertEqual(claude._stream_metadata(complete)[1], {"input_tokens": 99})

    def test_glm_quota_pause_is_normalized_without_retry_or_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                [], 9, "", "quota limit reached; available again in 3 hours"
            ))
            result = claude.launch(executable="claude", repo=repo, worktree=lane,
                provider="glm", model="glm-5.3", provider_config=self.glm,
                model_config={"runtime_model": "glm-5.3", "protocol": "anthropic-compatible"},
                prompt="task", secret="selected", runner=runner)
        self.assertEqual(result.availability, "temporarily-unavailable")
        runner.assert_called_once()

    @mock.patch("side_lane.adapters.claude.os.killpg")
    @mock.patch("side_lane.adapters.claude.subprocess.Popen")
    def test_bounded_launch_timeout_stops_process_group(self, popen: mock.Mock,
                                                        killpg: mock.Mock) -> None:
        process = popen.return_value
        process.pid = 77
        process.communicate.side_effect = [subprocess.TimeoutExpired("claude", 1),
                                           ("partial", "diagnostic")]
        completed = claude._bounded_process(["claude"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        killpg.assert_called_once_with(77, claude.signal.SIGTERM)



class AllowedToolsTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}

    def command(self, mode: str, capabilities=()) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixed"
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir(parents=True)
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            protocol = "native-claude-readonly" if mode == "review" else "native-claude"
            return claude.build_command(executable="claude", repo=repo, worktree=lane,
                provider="claude", model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": protocol},
                prompt="task", mode=mode, capabilities=capabilities)

    def test_review_never_receives_an_allowlist(self) -> None:
        self.assertEqual(claude.allowed_tools("review", ("shell", "git-push")), ())
        self.assertEqual(claude.disallowed_tools("review", ("git-push",)), ())
        command = self.command("review", ("shell",))
        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("--disallowedTools", command)
        strip = lambda argv: [item for item in argv if not item.startswith("/")]
        self.assertEqual(strip(command), strip(self.command("review")))

    def test_execute_without_shell_gets_only_file_tools(self) -> None:
        tools = claude.allowed_tools("execute", ())
        self.assertEqual(tools, ("Read", "Edit", "Write", "Glob", "Grep"))
        self.assertFalse(any(tool.startswith("Bash(") for tool in tools))

    def test_graph_capabilities_grant_read_only_mcp_tools_in_execute(self) -> None:
        gitnexus = tuple(f"mcp__gitnexus__{name}" for name in (
            "api_impact", "check", "context", "cypher", "detect_changes", "explain",
            "group_list", "impact", "list_repos", "pdg_query", "query", "route_map",
            "shape_check", "tool_map", "trace"))
        codegraph = tuple(f"mcp__codegraph__{name}" for name in (
            "find_symbol", "find_callers", "find_callees", "find_importers",
            "neighbors", "impact_of", "path_between"))
        base = claude.allowed_tools("execute", ())
        self.assertEqual(claude.allowed_tools("execute", ("gitnexus",)), base + gitnexus)
        self.assertEqual(claude.allowed_tools("execute", ("codegraph",)), base + codegraph)
        self.assertEqual(claude.allowed_tools("execute", ("gitnexus", "codegraph")), base + gitnexus + codegraph)
        # Index-mutating GitNexus tools stay ungranted; no wildcard grants.
        for tool in claude.allowed_tools("execute", ("gitnexus", "codegraph")):
            self.assertNotRegex(tool, r"rename|group_sync|analyze|clean|__\*$")
        self.assertFalse(any(tool.startswith("mcp__") for tool in base))
        self.assertEqual(claude.allowed_tools("review", ("gitnexus", "codegraph")), ())
        self.assertEqual(claude.disallowed_tools("execute", ("gitnexus", "codegraph")), ())

    def test_shell_or_workspace_write_adds_ordinary_dev_commands_not_push(self) -> None:
        for capability in ("shell", "workspace-write"):
            tools = claude.allowed_tools("execute", (capability,))
            for expected in ("Bash(pnpm *)", "Bash(npx *)", "Bash(npm *)", "Bash(node *)",
                             "Bash(./node_modules/.bin/*)", "Bash(uv *)", "Bash(uvx *)",
                             "Bash(python3 *)", "Bash(python3.11 *)", "Bash(git add *)",
                             "Bash(git commit *)", "Bash(ln *)", "Bash(cp *)", "Bash(mkdir *)",
                             "Bash(ls *)", "Bash(cat *)", "Bash(gh pr view *)", "Bash(gh pr list *)",
                             "Bash(gh pr diff *)", "Bash(gh run view *)"):
                self.assertIn(expected, tools)
            self.assertNotIn("Bash(git push *)", tools)
            self.assertFalse(any("gcloud" in tool or "gh pr merge" in tool or "deploy" in tool for tool in tools))
        self.assertEqual(claude.allowed_tools("execute", ("shell",)), claude.allowed_tools("execute", ("shell", "shell")))

    def test_git_push_requires_shell_and_denies_force(self) -> None:
        tools = claude.allowed_tools("execute", ("git-push",))
        self.assertIn("Bash(git push *)", tools)
        self.assertIn("Bash(git add *)", tools)
        self.assertEqual(claude.disallowed_tools("execute", ("git-push",)),
                         ("Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)", "Bash(git push * -f*)", "Bash(git push --force-with-lease*)", "Bash(git push * --force-with-lease*)", "Bash(git push --mirror*)", "Bash(git push * --mirror*)", "Bash(git push +*)", "Bash(git push * +*)"))
        self.assertEqual(claude.disallowed_tools("execute", ("shell",)), ())

    def test_unknown_capability_fails_closed(self) -> None:
        for mode in ("execute", "review"):
            with self.assertRaisesRegex(claude.ClaudeAdapterError, "unknown capability"):
                claude.allowed_tools(mode, ("sudo",))

    def test_execute_argv_carries_each_tool_separately(self) -> None:
        command = self.command("execute", ("shell", "git-push"))
        allowed = [command[index + 1] for index, flag in enumerate(command) if flag == "--allowedTools"]
        self.assertEqual(tuple(allowed), claude.allowed_tools("execute", ("shell", "git-push")))
        denied = [command[index + 1] for index, flag in enumerate(command) if flag == "--disallowedTools"]
        self.assertEqual(tuple(denied), claude.disallowed_tools("execute", ("git-push",)))
        self.assertIn("acceptEdits", command)
        self.assertIn("Injected canonical side-lane governance", command[-1])

    def test_launch_forwards_capabilities(self) -> None:
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="ok", stderr=""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
            result = claude.launch(executable="claude", repo=repo, worktree=lane, provider="claude",
                model="claude-sonnet-5", provider_config=self.native,
                model_config={"runtime_model": "claude-sonnet-5", "protocol": "native-claude"},
                prompt="task", mode="execute", capabilities=("shell",), env={"PATH": "/bin"}, runner=runner)
        self.assertIn("Bash(pnpm *)", result.argv)
        self.assertEqual(result.allowed_tools, claude.allowed_tools("execute", ("shell",)))
        self.assertIn("allowed_tools", result.as_dict())


if __name__ == "__main__":
    unittest.main()
