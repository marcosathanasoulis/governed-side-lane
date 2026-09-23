from pathlib import Path
import tempfile
import unittest

from side_lane.connector_metadata import json_mcp_name_scopes, json_mcp_names, toml_mcp_names


class ConnectorMetadataTests(unittest.TestCase):
    def test_json_extracts_only_connector_keys_without_retaining_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"secret":"never-retain","mcpServers":'
                '{"gitnexus":{"env":{"TOKEN":"also-never"}},"asana":{"command":"x"}}}',
                encoding="utf-8",
            )
            self.assertEqual(json_mcp_names(path), {"gitnexus", "asana"})
            self.assertNotIn("also-never", str((json_mcp_name_scopes(path))))

    def test_json_ignores_a_container_field_inside_a_server_definition(self) -> None:
        # A definition's own fields are data: the adapter's registration merge
        # records a dict-valued definition without inspecting it, so a field
        # spelled ``mcpServers`` there is neither a second registry nor a
        # reason to refuse the file — the local profile and the launch gate
        # read this inventory to decide whether the child loads a server.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".mcp.json"
            path.write_text(
                '{"gitnexus":{"command":"x","mcpServers":null}}', encoding="utf-8"
            )
            self.assertEqual(json_mcp_name_scopes(path), {})
            self.assertEqual(
                json_mcp_name_scopes(path, root_mapping_fallback=True),
                {"gitnexus": {()}},
            )
            path.write_text(
                '{"mcpServers":{"gitnexus":{"command":"x",'
                '"mcpServers":{"phantom":{"command":"p"}}}}}',
                encoding="utf-8",
            )
            self.assertEqual(json_mcp_name_scopes(path), {"gitnexus": {()}})

    def test_json_scopes_distinguish_root_from_per_project_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.json"
            path.write_text(
                '{"mcpServers":{"gitnexus":{"command":"g"}},'
                '"projects":{"/home/me/other":{"mcpServers":{"gitnexus":{"env":{"TOKEN":"never"}},"zoom":{"command":"z"}}},'
                '"/home/me/repo":{"mcpServers":{}}}}',
                encoding="utf-8",
            )
            scopes = json_mcp_name_scopes(path)
        self.assertEqual(scopes["gitnexus"], {(), ("projects", "/home/me/other")})
        self.assertEqual(scopes["zoom"], {("projects", "/home/me/other")})
        self.assertNotIn("TOKEN", scopes)
        self.assertNotIn("never", str(scopes))

    def test_only_the_effective_mcp_servers_value_has_its_shape_judged(self) -> None:
        # ``json.loads`` keeps the LAST value of a duplicated key, so a
        # superseded ``null`` container is not a malformed registry: the host
        # loads the object that replaced it. Judging every occurrence would
        # abort a valid file before the replacement was even read.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.json"
            path.write_text(
                '{"mcpServers":null,"mcpServers":{"jira":{"command":"j"}}}',
                encoding="utf-8",
            )
            self.assertEqual(json_mcp_name_scopes(path), {"jira": {()}})
            # The converse order keeps last-value-wins and refuses the file the
            # reader has no container for, exactly as before.
            path.write_text(
                '{"mcpServers":{"jira":{"command":"j"}},"mcpServers":null}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "mcpServers must be a JSON object"):
                json_mcp_name_scopes(path)

    def test_a_shape_refusal_is_decided_on_the_object_the_host_loads(self) -> None:
        # The refusal is deferred past every enclosing key, not just the
        # container key: the value that decides it is the one that survives
        # last-value-wins, so a superseded project entry or ``projects`` object
        # cannot refuse a file the host loads (4079018504).
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.json"
            for text, expected in (
                ('{"projects":{"/lane":{"mcpServers":null}},"projects":{}}', {}),
                ('{"projects":{"/lane":{"mcpServers":null},"/lane":{}}}', {}),
                ('{"projects":{"/lane":{"mcpServers":null},'
                 '"/lane":{"mcpServers":{"kept":{"command":"k"}}}}}',
                 {"kept": {("projects", "/lane")}}),
            ):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    self.assertEqual(json_mcp_name_scopes(path), expected)
            # An invalid entry the host DOES load still refuses the file, and
            # the refusal is the shape error alone — no key, no value.
            for text in (
                '{"projects":{"/lane":{"mcpServers":{"gone":{}}},'
                '"/lane":{"mcpServers":null}}}',
                '{"projects":{"/lane":{"env":{"TOKEN":"never-retain"},'
                '"mcpServers":null}}}',
            ):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "^mcpServers must be a JSON object$"
                    ) as raised:
                        json_mcp_name_scopes(path)
                    self.assertNotIn("never-retain", str(raised.exception))

    def test_a_superseded_value_is_still_held_to_json_syntax(self) -> None:
        # Deferring the SHAPE decision must not defer the SYNTAX one: a token
        # the host's parser rejects is refused wherever it appears, so no name
        # is ever taken from bytes the host would not load.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.json"
            for text in (
                # superseded value: an unquoted token JSON does not define
                '{"mcpServers":{"a":{}},"mcpServers":truX}',
                # superseded value: an array with a trailing comma
                '{"mcpServers":[1,],"mcpServers":{"b":{}}}',
                # superseded value: an escape JSON does not define
                '{"mcpServers":{"a":{}},"mcpServers":{"b":{"\\q":1}}}',
                # the effective value is fine; the trailing bytes are not
                '{"mcpServers":null,"mcpServers":{"a":{}}} trailing',
            ):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        json_mcp_name_scopes(path)

    def test_root_mapping_fallback_reads_the_files_own_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".mcp.json"
            path.write_text(
                '{"gitnexus":{"command":"g","token":"never-retain"},'
                '"playwright":{"command":"p"},"disabled":"scalar"}',
                encoding="utf-8",
            )
            # Only the opt-in reading sees this shape: the default one looks
            # for an ``mcpServers`` container and finds none.
            self.assertEqual(json_mcp_name_scopes(path), {})
            scopes = json_mcp_name_scopes(path, root_mapping_fallback=True)
        self.assertEqual(scopes, {"gitnexus": {()}, "playwright": {()}})
        self.assertNotIn("never-retain", str(scopes))

    def test_root_mapping_fallback_yields_to_an_effective_container(self) -> None:
        # An ``mcpServers`` container the host reads replaces the flat reading,
        # whatever is beside it; absent, empty or non-object, the flat reading
        # is the one the host uses. Names nested under a flat key are not
        # registrations at all — the file is read as the name map it is.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".mcp.json"
            cases = {
                '{"mcpServers":{"a":{}},"b":{}}': {"a": {()}},
                '{"mcpServers":{},"b":{}}': {"b": {()}, "mcpServers": {()}},
                '{"mcpServers":null,"b":{}}': {"b": {()}},
                '{"mcpServers":"nope","b":{}}': {"b": {()}},
                '{"a":{"mcpServers":{"b":{}}}}': {"a": {()}},
                '{"mcpServers":null,"mcpServers":{"a":{}}}': {"a": {()}},
            }
            for text, expected in cases.items():
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    self.assertEqual(
                        json_mcp_name_scopes(path, root_mapping_fallback=True), expected
                    )

    def test_root_mapping_fallback_is_refused_for_a_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".mcp.json"
            path.write_text('[{"a":{}}]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                json_mcp_name_scopes(path, root_mapping_fallback=True)

    def test_project_entries_are_read_only_in_a_file_that_has_them(self) -> None:
        # ``projects.<path>.mcpServers`` is a container in the Claude USER
        # config only; the worktree ``.mcp.json`` reader reads that file's
        # ``mcpServers`` container or its own top-level keys, and the Devin
        # reader is root-only. That is a fact about the READER, so the caller
        # supplies it (``project_entries``) rather than the scanner guessing one
        # from a file name — the same bytes are read differently for the two,
        # and in a root-only file a ``projects`` key is ordinary data.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".mcp.json"
            path.write_text(
                '{"mcpServers":{"gitnexus":{"command":"g"}},'
                '"projects":{"/p":{"mcpServers":null}}}',
                encoding="utf-8",
            )
            self.assertEqual(
                json_mcp_name_scopes(path, project_entries=False),
                {"gitnexus": {()}},
            )
            # The user-config reading of the same bytes DOES refuse: a project
            # entry's container is judged there, and a non-object one is a file
            # that position cannot yield a registry from.
            with self.assertRaisesRegex(ValueError, "mcpServers must be a JSON object"):
                json_mcp_name_scopes(path)
            # A server NAMED ``projects``: with no root container the file is
            # read flat, so the definition is one name and its own nested
            # ``mcpServers`` field is data rather than a position.
            path.write_text(
                '{"projects":{"command":"x","extra":{"mcpServers":null}}}',
                encoding="utf-8",
            )
            self.assertEqual(
                json_mcp_name_scopes(
                    path, root_mapping_fallback=True, project_entries=False
                ),
                {"projects": {()}},
            )

    def test_toml_extracts_only_mcp_table_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'api_key = "never-retain"\n[mcp_servers.gitnexus]\nenv = { TOKEN = "also-never" }\n',
                encoding="utf-8",
            )
            self.assertEqual(toml_mcp_names(path), {"gitnexus"})


if __name__ == "__main__":
    unittest.main()
