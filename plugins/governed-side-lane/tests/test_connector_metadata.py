from pathlib import Path
import tempfile
import unittest

from side_lane.connector_metadata import json_mcp_names, toml_mcp_names


class ConnectorMetadataTests(unittest.TestCase):
    def test_json_extracts_only_connector_keys_without_retaining_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"secret":"never-retain","nested":{"mcpServers":{"gitnexus":{"env":{"TOKEN":"also-never"}},"asana":{"command":"x"}}}}',
                encoding="utf-8",
            )
            self.assertEqual(json_mcp_names(path), {"gitnexus", "asana"})

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
