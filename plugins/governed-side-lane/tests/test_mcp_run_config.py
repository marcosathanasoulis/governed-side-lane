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
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from side_lane import mcp_run_config as mrc
from side_lane.adapters import claude, codex, devin
from side_lane.governance import tool_policy

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

# The `omniroute-mcp` account's sibling shape: same contract as the aws
# registration, distinct names throughout. The endpoint is a placeholder —
# public fixtures never carry a real internal hostname.
OMNIROUTE_SHAPED = {
    "mcpServers": {
        "omniroute": {
            "type": "http",
            "url": "https://omniroute.example.invalid/mcp",
            "headers": {"Authorization": "Bearer ${CLAUDE_TAG_OMNIROUTE_MCP_TOKEN}"},
        }
    }
}

OMNIROUTE_SERVERS = {"omniroute": mrc.McpRunServer(
    name="omniroute", url="https://omniroute.example.invalid/mcp",
    headers=(("Authorization", "Bearer", "CLAUDE_TAG_OMNIROUTE_MCP_TOKEN"),),
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

    def test_accepts_the_omniroute_sibling_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            servers = mrc.load_run_mcp_config(
                write_config(Path(directory), OMNIROUTE_SHAPED))
        self.assertEqual(mrc.audit_names(servers), ("omniroute",))
        server = servers["omniroute"]
        self.assertEqual(server.url, "https://omniroute.example.invalid/mcp")
        self.assertEqual(server.bearer_env(), "CLAUDE_TAG_OMNIROUTE_MCP_TOKEN")
        # Capability narrowing mirrors aws-read: only the granted capability
        # admits its server, and a merged document needs both grants.
        mrc.validate_against_capabilities(servers, {"omniroute-read"})
        with self.assertRaisesRegex(mrc.McpRunConfigError, "no granted capability"):
            mrc.validate_against_capabilities(servers, {"aws-read"})
        both = {**SERVERS, **OMNIROUTE_SERVERS}
        mrc.validate_against_capabilities(both, {"aws-read", "omniroute-read"})
        with self.assertRaisesRegex(mrc.McpRunConfigError, "no granted capability"):
            mrc.validate_against_capabilities(both, {"aws-read"})

    def test_omniroute_codex_delivery_carries_url_and_env_name_only(self) -> None:
        self.assertIn(
            'mcp_servers.omniroute.url="https://omniroute.example.invalid/mcp"',
            mrc.codex_overrides(OMNIROUTE_SERVERS))
        self.assertIn(
            'mcp_servers.omniroute.bearer_token_env_var="CLAUDE_TAG_OMNIROUTE_MCP_TOKEN"',
            mrc.codex_overrides(OMNIROUTE_SERVERS))

    def test_omniroute_read_grants_exactly_the_observed_read_tools(self) -> None:
        # The canonical allowlist names the exact read-only tool IDs the
        # server's live tools/list returned: no wildcard, no mutation or
        # inference tool, and aws-read is unaffected.
        granted = set(tool_policy().allowed["omniroute-read"])
        self.assertEqual(granted - {"WaitForMcpServers"}, {
            "mcp__omniroute__omniroute_get_health",
            "mcp__omniroute__omniroute_list_models_catalog",
            "mcp__omniroute__omniroute_list_combos",
            "mcp__omniroute__omniroute_get_combo_metrics",
            "mcp__omniroute__omniroute_simulate_route",
            "mcp__omniroute__omniroute_check_quota",
            "mcp__omniroute__omniroute_get_session_snapshot",
            "mcp__omniroute__omniroute_cost_report",
            "mcp__omniroute__omniroute_tool_search",
        })
        self.assertFalse(any(rule.endswith("*") for rule in granted))
        tools = claude.allowed_tools("execute", ("omniroute-read",))
        self.assertIn("mcp__omniroute__omniroute_tool_search", tools)
        self.assertNotIn("mcp__omniroute__omniroute_create_combo", tools)
        self.assertNotIn("mcp__omniroute__omniroute_route_request", tools)
        self.assertNotIn("mcp__omniroute__*", tools)
        self.assertEqual(claude.allowed_tools("review", ("omniroute-read",)), ())

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


class SettingsProjectEntryTests(unittest.TestCase):
    """Claude's settings file is not an MCP registration source (4078853747).

    The adapter's own registration merge (``_effective_mcp_registrations``)
    reads ``projects.<path>.mcpServers`` from exactly ONE file — the user
    ``.claude.json`` — and the host's MCP scopes resolve to that file and the
    lane ``.mcp.json`` alone. The settings file was listed as a registration
    file, so every inventory read a registry out of it: its ``projects`` entry
    first (the context predicate took the filename's inequality with
    ``.mcp.json``), and its root container beside that. Both invented a name (a
    server-wide ``mcp__<server>`` rule, and a same-name launch refusal) the
    child never loads, and a non-object container there raised out of every
    inventory of the lane's registry over a file the child loads whole.
    """

    GRANTED = ("gitnexus", "playwright", "codegraph")
    PHANTOM = {"gitnexus": {"type": "http", "url": "https://phantom.invalid/m"}}

    def harness(self, root: Path) -> tuple[Path, Path, Path]:
        repo, lane, home = root / "repo", root / "lane", root / "home"
        for path in (repo, lane, home):
            path.mkdir()
            (path / ".git").mkdir()
        (home / ".claude").mkdir()
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        return repo, lane, home

    def write_settings(self, home: Path, payload: object) -> Path:
        path = home / ".claude" / "settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def adapter_names(self, lane: Path, home: Path) -> set[str]:
        """The names the adapter's own merge loads, so parity is host-anchored."""

        return {
            name for name, _scope, _path, _definition
            in claude._effective_mcp_registrations(
                host="claude", repo=lane, worktree=lane, home=home,
                granted_capabilities=self.GRANTED)
        }

    def test_a_settings_project_entry_is_never_a_registration(self) -> None:
        """The phantom name reaches no reader, and the conflict check stays clear."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, lane, home = self.harness(root)
            self.write_settings(home, {"projects": {
                str(lane): {"mcpServers": self.PHANTOM}}})
            self.assertEqual(self.adapter_names(lane, home), set())
            names = mrc.host_registered_server_names(
                "claude", lane, env={"HOME": str(home)})
            scopes = mrc.json_registration_scopes(
                "claude", home / ".claude" / "settings.json")
            conflicts = mrc.conflicting_server_names(
                SERVERS, "claude", lane, env={"HOME": str(home)})
        self.assertEqual(scopes, {})
        self.assertEqual(names, ())
        # SERVERS declares "aws"; the phantom declares "gitnexus". Neither is a
        # registration of this lane, so neither conflicts.
        self.assertEqual(conflicts, set())

    def test_a_non_object_settings_project_container_never_refuses(self) -> None:
        """A nested non-object container is data, not a shape the file is refused for."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, lane, home = self.harness(root)
            self.write_settings(home, {"projects": {
                str(lane): {"mcpServers": None}}})
            names = mrc.host_registered_server_names(
                "claude", lane, env={"HOME": str(home)})
            scopes = mrc.json_registration_scopes(
                "claude", home / ".claude" / "settings.json")
        self.assertEqual(scopes, {})
        self.assertEqual(names, ())

    def test_a_settings_root_container_is_no_registry_either(self) -> None:
        """The file carries no MCP scope, at its root or in a project entry.

        A root ``mcpServers`` key there is a settings field: reading it as a
        container named a server the adapter's own merge never loads, which put
        a server-wide ``mcp__<server>`` rule on the local-developer profile and
        made the pre-launch conflict check refuse a name the host was free.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, lane, home = self.harness(root)
            self.write_settings(home, {
                "mcpServers": self.PHANTOM,
                "projects": {str(lane): {"mcpServers": None}},
            })
            self.assertEqual(self.adapter_names(lane, home), set())
            names = mrc.host_registered_server_names(
                "claude", lane, env={"HOME": str(home)})
        self.assertEqual(names, ())

    def test_the_claude_json_project_position_is_unchanged(self) -> None:
        """The one file with the position keeps it, in every reader."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, lane, home = self.harness(root)
            (home / ".claude.json").write_text(json.dumps({"projects": {
                str(lane): {"mcpServers": self.PHANTOM},
                "/elsewhere": {"mcpServers": {
                    "playwright": {"type": "http", "url": "https://e.invalid/m"}}},
            }}), encoding="utf-8")
            self.assertEqual(self.adapter_names(lane, home), {"gitnexus"})
            names = mrc.host_registered_server_names(
                "claude", lane, env={"HOME": str(home)})
            scopes = mrc.json_registration_scopes("claude", home / ".claude.json")
            conflicts = mrc.conflicting_server_names(
                {"gitnexus": mrc.McpRunServer(
                    name="gitnexus", url="https://run.invalid/mcp")},
                "claude", lane, env={"HOME": str(home)})
        self.assertEqual(scopes["gitnexus"], {("projects", str(lane))})
        self.assertIn("playwright", scopes)
        self.assertEqual(names, ("gitnexus",))
        self.assertEqual({name for name, _s, _p in conflicts}, {"gitnexus"})


class RegistryParityTests(unittest.TestCase):
    """The inventory must be the registry the child loads, not a near one.

    Every shape below is differential: the names ``host_registered_server_names``
    reports for a fixture are compared against the Claude adapter's own
    registration reader for the same files, because that reader is what decides
    which servers the child gets. The inventory carries no capability filter
    (the local-developer profile renders a rule for every registered name), so
    the adapter's names must be a SUBSET of it; where the fixture declares only
    capability-mapped names the two are equal, which is the exact-parity claim.
    """

    GRANTED = ("gitnexus", "playwright", "codegraph")

    def harness(self, root: Path) -> tuple[Path, Path]:
        repo, lane = root / "repo", root / "lane"
        for path in (repo, lane):
            path.mkdir()
            (path / ".git").mkdir()
        home = root / "home"
        home.mkdir()
        return lane, home

    def assert_same_registry(
        self, user_text: str, worktree_text: str, expected: set[str], *, exact: bool = True
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane, home = self.harness(root)
            # ``{lane}`` stands for the worktree path a project entry is keyed
            # by; a lane's own entry is in scope and no other path's is.
            (home / ".claude.json").write_text(
                user_text.replace("{lane}", str(lane)), encoding="utf-8")
            (lane / ".mcp.json").write_text(worktree_text, encoding="utf-8")
            # ``repo=worktree`` so the adapter's project-entry key is the one
            # the inventory resolves for this lane.
            adapter_names = {
                name for name, _scope, _path, _definition
                in claude._effective_mcp_registrations(
                    host="claude", repo=lane, worktree=lane, home=home,
                    granted_capabilities=self.GRANTED,
                )
            }
            inventory = set(
                mrc.host_registered_server_names("claude", lane, env={"HOME": str(home)})
            )
        self.assertEqual(inventory, expected)
        self.assertLessEqual(adapter_names, inventory)
        if exact:
            self.assertEqual(adapter_names, inventory)

    def test_every_effective_object_shape_agrees_with_the_adapter(self) -> None:
        cases = (
            # A superseded ``null`` container is not a refusal: the host loads
            # the object that replaced it.
            ("user superseded container",
             '{"mcpServers":null,"mcpServers":{"gitnexus":{"command":"g"}}}',
             "{}", {"gitnexus"}),
            ("user non-object definition is not a registration",
             '{"mcpServers":{"gitnexus":null,"playwright":{"command":"p"}}}',
             "{}", {"playwright"}),
            # The worktree ``.mcp.json`` shape: its own top-level keys are the
            # server names (``_effective_mcp_registrations``'s fallback).
            ("worktree flat mapping",
             "{}", '{"gitnexus":{"command":"g"},"playwright":{"command":"p"}}',
             {"gitnexus", "playwright"}),
            ("worktree container",
             "{}", '{"mcpServers":{"gitnexus":{"command":"g"}}}', {"gitnexus"}),
            # A ``\u`` escape in a name is DECODED, so the inventory names the
            # server the host loads rather than the spelling in the file.
            ("worktree escaped key",
             "{}", '{"gitn\\u0065xus":{"command":"g"}}', {"gitnexus"}),
            # Scope filtering survives the flat reading: a per-project entry is
            # this lane's only when it is keyed by this lane's own path.
            ("user project entry for this lane",
             '{"projects":{"{lane}":{"mcpServers":{"codegraph":{"command":"c"}}}}}',
             "{}", {"codegraph"}),
            ("user project entry for another path",
             '{"projects":{"/elsewhere":{"mcpServers":{"gitnexus":{"command":"g"}}}}}',
             "{}", set()),
            ("worktree nested container is not a registration",
             "{}", '{"gitnexus":{"mcpServers":{"playwright":{"command":"p"}}}}',
             {"gitnexus"}),
            # A definition's OWN fields are data: the adapter records a
            # dict-valued definition without inspecting what is inside it, so a
            # nested ``mcpServers`` field is neither a refusal (its own spelling
            # may be ``null``) nor a second registration container. A scanner
            # that read the field as one refused the file outright, and the
            # worker's whole registry inventory was lost with it.
            ("worktree definition with a nested null field",
             "{}", '{"gitnexus":{"command":"g","mcpServers":null}}',
             {"gitnexus"}),
            ("user definition with a nested container field",
             '{"mcpServers":{"gitnexus":{"command":"g",'
             '"mcpServers":{"playwright":{"command":"p"}}}}}',
             "{}", {"gitnexus"}),
            # A ``projects`` key in the worktree file is data: that file's
            # reader has no project position at all, so an entry inside it is
            # neither a registration (its ``mcpServers`` spelling is never
            # judged, whatever the value) nor a scope. A scanner that applied
            # the user config's rule here raised "mcpServers must be a JSON
            # object" over a file the child loads whole, and refused a
            # capability with it.
            ("worktree container beside a nested projects entry",
             "{}", '{"mcpServers":{"gitnexus":{"command":"g"}},'
                   '"projects":{"/p":{"mcpServers":null}}}',
             {"gitnexus"}),
            ("worktree container and flat key together",
             "{}", '{"mcpServers":{"gitnexus":{"command":"g"}},"playwright":{"command":"p"}}',
             {"gitnexus"}),
            # An EMPTY container is the one shape whose flat reading also names
            # the container key itself, which the adapter's fallback re-reads as
            # a definition. The inventory reports it too (never fewer names than
            # the child loads), so this case is a superset, not an equality.
            ("worktree empty container falls back",
             "{}", '{"mcpServers":{},"playwright":{"command":"p"}}',
             {"mcpServers", "playwright"}),
        )
        for label, user_text, worktree_text, expected in cases:
            with self.subTest(label):
                self.assert_same_registry(
                    user_text, worktree_text, expected,
                    exact="mcpServers" not in expected,
                )

    def test_a_worktree_server_named_projects_is_a_name_not_a_scope(self) -> None:
        # The reported reproduction. The worktree ``.mcp.json`` is the one
        # Claude file whose reader has NO project position, so a flat
        # registration NAMED ``projects`` — a real command carrying its own
        # nested fields — is a server, not a scope. Reading the user config's
        # position rule into it let the definition's nested ``mcpServers``
        # field decide the whole file's shape: a ``null`` spelling raised
        # "mcpServers must be a JSON object" out of every inventory of the
        # lane's registry, so the gate refused a capability the child loads.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane, home = self.harness(root)
            (home / ".claude.json").write_text("{}", encoding="utf-8")
            text = '{"projects":{"command":"x","extra":{"mcpServers":null}}}'
            (lane / ".mcp.json").write_text(text, encoding="utf-8")
            inventory = set(
                mrc.host_registered_server_names("claude", lane, env={"HOME": str(home)})
            )
            scopes = mrc.json_registration_scopes("claude", lane / ".mcp.json")
        self.assertEqual(scopes, {"projects": {()}})
        self.assertEqual(inventory, {"projects"})
        # The adapter's own rule for this shape: every object-valued top-level
        # key is a server, and nothing nested inside one is (``servers = cfg``).
        self.assertEqual(
            {
                name for name, value in json.loads(text).items()
                if isinstance(value, dict)
            },
            inventory,
        )

    def test_a_conflict_is_seen_for_the_flat_worktree_mapping(self) -> None:
        # The same registry the inventory reads is the one a per-run name may
        # not shadow: a declared name the worktree ``.mcp.json`` already
        # registers fails closed before launch.
        servers = {"gitnexus": mrc.McpRunServer(
            name="gitnexus", url="https://gitnexus.example.invalid/mcp")}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane, home = self.harness(root)
            (home / ".claude.json").write_text("{}", encoding="utf-8")
            (lane / ".mcp.json").write_text(
                '{"gitnexus":{"command":"g"}}', encoding="utf-8")
            with self.assertRaisesRegex(mrc.McpRunConfigError, "already registers"):
                mrc.ensure_no_registration_conflicts(
                    servers, "claude", lane, env={"HOME": str(home)})

    def test_ambient_claude_config_dir_is_never_the_child_registry(self) -> None:
        # ``CLAUDE_CONFIG_DIR`` is on the Claude adapter's SCRUB_EXACT list, so
        # a child launched from this process never opens the ambient directory.
        # An inventory that resolved it anyway would report a registry the
        # worker does not have — and miss the one it does.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane, home = self.harness(root)
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {
                "gitnexus": {"command": "g"}}}), encoding="utf-8")
            ambient = root / "ambient"
            ambient.mkdir()
            (ambient / ".claude.json").write_text(json.dumps({"mcpServers": {
                "playwright": {"command": "p"}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {
                    "HOME": str(home), "CLAUDE_CONFIG_DIR": str(ambient)}):
                names = mrc.host_registered_server_names("claude", lane)
                paths = [path for _scope, path in mrc.registration_paths("claude", lane)]
        self.assertEqual(names, ("gitnexus",))
        self.assertNotIn(ambient / ".claude.json", paths)

    def test_an_explicit_child_env_still_resolves_the_lane_config_dir(self) -> None:
        # The routed execute lane SETS ``CLAUDE_CONFIG_DIR`` for the child, and
        # the child-scoped callers pass that environment: it must keep winning
        # over the home registry, or the conflict check would read the wrong one.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane, home = self.harness(root)
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {
                "gitnexus": {"command": "g"}}}), encoding="utf-8")
            routed = root / "routed"
            routed.mkdir()
            (routed / ".claude.json").write_text(json.dumps({"mcpServers": {
                "playwright": {"command": "p"}}}), encoding="utf-8")
            names = mrc.host_registered_server_names(
                "claude", lane, env={"HOME": str(home), "CLAUDE_CONFIG_DIR": str(routed)})
        self.assertEqual(names, ("playwright",))


if __name__ == "__main__":
    unittest.main()
