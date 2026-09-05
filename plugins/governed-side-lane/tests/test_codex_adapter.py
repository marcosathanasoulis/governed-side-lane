from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane.adapters import codex


class CodexAdapterTests(unittest.TestCase):
    provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
    execute = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex"}
    review = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex-readonly"}

    def repo(self, root: Path, name: str) -> Path:
        path = root / name
        path.mkdir()
        (path / ".git").mkdir()
        return path

    def test_native_execute_and_review_commands_inject_canonical_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            execute = codex.build_codex_command("codex", repo, lane, "openai", "gpt-5.6-terra", self.provider, self.execute, "task")
            review = codex.build_codex_command("codex", repo, lane, "openai", "gpt-5.6-terra", self.provider, self.review, "review", mode="review")
        self.assertEqual(execute[execute.index("-s") + 1], "danger-full-access")
        self.assertEqual(review[review.index("-s") + 1], "read-only")
        self.assertIn("mcp_servers={}", review)
        self.assertIn("Injected canonical side-lane governance", execute[-1])
        self.assertNotIn("INPROCESS.md", execute[-1])

    def test_rejects_non_native_or_model_substitution_and_main_checkout_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(Path(directory), "repo")
            with self.assertRaises(codex.CodexAdapterError):
                codex.build_codex_command("codex", repo, repo, "glm", "gpt-5.6-terra", self.provider, self.execute, "task")
            with self.assertRaises(codex.CodexAdapterError):
                codex.build_codex_command("codex", repo, repo, "openai", "gpt-5.6-terra", self.provider, {**self.execute, "runtime_model": "gpt-5.6-sol"}, "task")
            with self.assertRaisesRegex(codex.CodexAdapterError, "dedicated worktree"):
                codex.build_codex_command("codex", repo, repo, "openai", "gpt-5.6-terra", self.provider, self.execute, "task")

    def test_oauth_environment_scrubs_all_provider_keys(self) -> None:
        child = codex.build_child_env({"PATH": "/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/adc", "OPENAI_API_KEY": "x", "ANTHROPIC_CUSTOM": "y", "GLM_API_KEY": "z"})
        self.assertEqual(child, {"PATH": "/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/adc"})

    def test_mocked_run_returns_normalized_result_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.repo(root, "repo"), self.repo(root, "lane")
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
            result = codex.run_codex(executable="codex", repo=repo, worktree=lane,
                provider="openai", model="gpt-5.6-terra", provider_config=self.provider,
                model_config=self.execute, prompt="task", env={"OPENAI_API_KEY": "never"}, runner=runner)
        self.assertEqual(result.gateway, "native-codex")
        self.assertEqual(result.auth_method, "oauth")
        self.assertFalse(result.billable)
        self.assertNotIn("OPENAI_API_KEY", runner.call_args.kwargs["env"])



class CodexSupportDirTests(unittest.TestCase):
    execute = {"runtime_model": "gpt-5.6-sol", "protocol": "native-codex"}
    native = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}

    def test_run_codex_prepends_support_dir_to_child_path(self) -> None:
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = root / "repo", root / "lane"
            for path in (repo, lane):
                path.mkdir()
                (path / ".git").mkdir()
            codex.run_codex(executable="/bundle/codex", repo=repo, worktree=lane, provider="openai",
                model="gpt-5.6-sol", provider_config=self.native, model_config=self.execute,
                prompt="task", env={"PATH": "/usr/bin", "OPENAI_API_KEY": "never"},
                support_dir="/bundle", runner=runner)
        child = runner.call_args.kwargs["env"]
        self.assertEqual(child["PATH"], "/bundle:/usr/bin")
        self.assertNotIn("OPENAI_API_KEY", child)


if __name__ == "__main__":
    unittest.main()
