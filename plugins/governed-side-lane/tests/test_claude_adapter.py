from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane.adapters import claude


class ClaudeAdapterTests(unittest.TestCase):
    native = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
    glm = {"gateway": "direct-zai", "auth_method": "provider-key", "billable": True, "base_url": "https://api.z.ai/api/anthropic"}

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
        })
        self.assertEqual(child, {"PATH": "/bin"})

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
                         ("Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * --force*)"))
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
