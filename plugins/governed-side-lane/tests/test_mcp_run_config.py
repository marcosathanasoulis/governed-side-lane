"""Per-run MCP run-config delivery: validation, schemes, merges, conflicts.

Regression suite for the reviewed delivery rules (see
``side_lane/mcp_run_config.py``): loopback is exact literals only, the Codex
host preserves the Bearer scheme exactly or fails closed, the Devin
local-scope merge preserves valid empty/metadata shapes, and a declared
server name any host scope already registers fails closed before launch.
Offline only — every host process is a stub.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import mcp_run_config as mrc
from side_lane.adapters import claude, codex, devin

GCF_SHAPED = {
    "mcpServers": {
        "aws": {
            "type": "http",
            "url": "https://bridge.example.invalid/mcp",
            "headers": {"Authorization": "Bearer ${CLAUDE_TAG_AWS_MCP_TOKEN}"},
        }
    }
}

SERVERS = {"aws": mrc.McpRunServer(
    name="aws", url="https://bridge.example.invalid/mcp",
    headers=(("Authorization", "Bearer", "CLAUDE_TAG_AWS_MCP_TOKEN"),),
)}


def write_config(root: Path, payload: object, name: str = "run-mcp.json") -> Path:
    path = root / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    return path


class ValidationTests(unittest.TestCase):
    def test_accepts_the_gcf_shaped_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            servers = mrc.load_run_mcp_config(write_config(Path(directory), GCF_SHAPED))
        self.assertEqual(mrc.audit_names(servers), ("aws",))
        self.assertEqual(servers["aws"].bearer_env(), "CLAUDE_TAG_AWS_MCP_TOKEN")

    def test_literal_credentials_and_stdio_entries_are_rejected(self) -> None:
        literal = json.loads(json.dumps(GCF_SHAPED))
        literal["mcpServers"]["aws"]["headers"]["Authorization"] = "Bearer sk-live-1"
        stdio = {"mcpServers": {"aws": {"type": "http", "url": "https://x.invalid/m",
                                        "command": "/bin/sh"}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, payload in (("literal", literal), ("stdio", stdio)):
                with self.subTest(label):
                    with self.assertRaises(mrc.McpRunConfigError) as raised:
                        mrc.load_run_mcp_config(write_config(root, payload, f"{label}.json"))
                    self.assertNotIn("sk-live-1", str(raised.exception))

    def test_loopback_is_exact_literals_not_a_localhost_prefix(self) -> None:
        accepted = ("http://127.0.0.1:8931/mcp", "http://localhost:8931/mcp",
                    "http://[::1]:8931/mcp")
        rejected = ("http://localhost.attacker.tld/mcp", "http://localhost.evil.invalid/mcp")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, url in enumerate(accepted):
                servers = mrc.load_run_mcp_config(write_config(root, {"mcpServers": {"aws": {
                    "type": "http", "url": url,
                    "headers": {"Authorization": "Bearer ${T}"}}}}, f"ok{index}.json"))
                self.assertEqual(servers["aws"].url, url)
            for index, url in enumerate(rejected):
                with self.subTest(url=url):
                    with self.assertRaisesRegex(mrc.McpRunConfigError, "https outside loopback"):
                        mrc.load_run_mcp_config(write_config(root, {"mcpServers": {"aws": {
                            "type": "http", "url": url,
                            "headers": {"Authorization": "Bearer ${T}"}}}}, f"bad{index}.json"))

    def test_codex_delivery_preserves_the_auth_scheme_or_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, value in (("Basic", "Basic ${A}"), ("no scheme", "${A}")):
                servers = mrc.load_run_mcp_config(write_config(root, {"mcpServers": {"aws": {
                    "type": "http", "url": "https://x.invalid/m",
                    "headers": {"Authorization": value}}}}, f"{label}.json"))
                self.assertIsNone(servers["aws"].bearer_env())
                with self.subTest(label):
                    with self.assertRaisesRegex(mrc.McpRunConfigError, "scheme"):
                        mrc.codex_overrides(servers)
        self.assertIn('mcp_servers.aws.bearer_token_env_var="CLAUDE_TAG_AWS_MCP_TOKEN"',
                      mrc.codex_overrides(SERVERS))


class DevinMergeTests(unittest.TestCase):
    def test_local_scope_merge_preserves_empty_and_top_level_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / ".devin").mkdir()
            path = worktree / ".devin" / "mcp_config.local.json"
            path.write_text("{}", encoding="utf-8")
            merged_path, original = devin._merge_local_mcp_config(worktree, SERVERS)
            self.assertEqual(original, "{}")
            self.assertIn("aws", json.loads(merged_path.read_text(encoding="utf-8"))["mcpServers"])
            metadata = {"version": 3, "mcpServers": {
                "other": {"url": "https://o.invalid/m", "transport": "http"}}, "note": "kept"}
            path.write_text(json.dumps(metadata), encoding="utf-8")
            merged_path, _original = devin._merge_local_mcp_config(worktree, SERVERS)
            kept = json.loads(merged_path.read_text(encoding="utf-8"))
            self.assertEqual(kept["version"], 3)
            self.assertEqual(kept["note"], "kept")
            self.assertIn("other", kept["mcpServers"])
            self.assertIn("aws", kept["mcpServers"])

    def test_local_scope_merge_fails_closed_on_a_name_clash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / ".devin").mkdir()
            (worktree / ".devin" / "mcp_config.local.json").write_text(json.dumps(
                {"mcpServers": {"aws": {"url": "https://other.invalid/m", "transport": "http"}}}),
                encoding="utf-8")
            with self.assertRaisesRegex(devin.DevinAdapterError, "already registers"):
                devin._merge_local_mcp_config(worktree, SERVERS)


class NameConflictTests(unittest.TestCase):
    """A declared name any host scope already registers fails before launch."""

    def harness(self, root: Path) -> tuple[Path, Path]:
        repo, lane = root / "repo", root / "lane"
        for path in (repo, lane):
            path.mkdir()
            (path / ".git").mkdir()
        return repo, lane

    def write_user_config(self, root: Path, relative: str, text: str) -> Path:
        home = root / "home"
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return home

    def test_conflicting_names_are_detected_per_host_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, lane = self.harness(root)
            home = self.write_user_config(root, ".claude.json", json.dumps({"mcpServers": {
                "aws": {"type": "http", "url": "https://user.invalid/m"}}}))
            conflicts = mrc.conflicting_server_names(
                SERVERS, "claude", lane, env={"HOME": str(home)})
            self.assertEqual({name for name, _scope, _path in conflicts}, {"aws"})
            clear = mrc.conflicting_server_names(
                SERVERS, "claude", lane, env={"HOME": str(root / "empty-home")})
            self.assertEqual(clear, set())

    def test_claude_launch_fails_closed_on_a_project_file_conflict(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
        provider = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
        execute = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.harness(root)
            (lane / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "aws": {"type": "http", "url": "https://project.invalid/m"}}}), encoding="utf-8")
            with self.assertRaisesRegex(mrc.McpRunConfigError, "already registers"):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=provider,
                    model_config=execute, prompt="task", mode="execute",
                    env={"PATH": "/bin", "HOME": str(root),
                         "CLAUDE_TAG_AWS_MCP_TOKEN": "placeholder"},
                    runner=runner, run_mcp_servers=SERVERS,
                )
        runner.assert_not_called()

    def test_claude_launch_validates_env_references_against_the_child_env(self) -> None:
        # ``GLM_*`` names are on the Claude scrub list: present in the
        # coordinator env but never in the worker child. The adapter must
        # validate the reference against the environment it actually built —
        # like the Codex and Devin adapters — not the pre-scrub coordinator
        # env, or the failure surfaces as a bridge 401 after the model starts.
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "done", ""))
        provider = {"gateway": "native-claude", "auth_method": "oauth", "billable": False}
        execute = {"runtime_model": "claude-sonnet-5", "protocol": "native-claude"}
        servers = {"aws": mrc.McpRunServer(
            name="aws", url="https://bridge.example.invalid/mcp",
            headers=(("Authorization", "Bearer", "GLM_MCP_TOKEN"),),
        )}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.harness(root)
            with self.assertRaisesRegex(mrc.McpRunConfigError, "GLM_MCP_TOKEN"):
                claude.launch(
                    executable="claude", repo=repo, worktree=lane, provider="claude",
                    model="claude-sonnet-5", provider_config=provider,
                    model_config=execute, prompt="task", mode="execute",
                    env={"PATH": "/bin", "HOME": str(root),
                         "GLM_MCP_TOKEN": "placeholder"},
                    runner=runner, run_mcp_servers=servers,
                )
        runner.assert_not_called()

    def test_codex_run_fails_closed_on_a_config_toml_conflict(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        provider = {"gateway": "native-codex", "auth_method": "oauth", "billable": False}
        execute = {"runtime_model": "gpt-5.6-terra", "protocol": "native-codex"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, lane = self.harness(root)
            home = self.write_user_config(root, ".codex/config.toml", (
                '[mcp_servers.aws]\nurl = "https://existing.invalid/m"\n'
                'bearer_token_env_var = "EXISTING_TOKEN"\n'))
            with self.assertRaisesRegex(mrc.McpRunConfigError, "already registers"):
                codex.run_codex(
                    executable="codex", repo=repo, worktree=lane, provider="openai",
                    model="gpt-5.6-terra", provider_config=provider,
                    model_config=execute, prompt="task",
                    env={"PATH": "/bin", "HOME": str(home),
                         "CLAUDE_TAG_AWS_MCP_TOKEN": "placeholder"},
                    runner=runner, run_mcp_servers=SERVERS,
                )
        runner.assert_not_called()

    def test_unreadable_registration_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self.write_user_config(root, ".claude.json", "{not json")
            with self.assertRaisesRegex(mrc.McpRunConfigError, "cannot verify"):
                mrc.ensure_no_registration_conflicts(
                    SERVERS, "claude", root / "lane", env={"HOME": str(home)})


if __name__ == "__main__":
    unittest.main()
