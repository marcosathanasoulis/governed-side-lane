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


if __name__ == "__main__":
    unittest.main()
