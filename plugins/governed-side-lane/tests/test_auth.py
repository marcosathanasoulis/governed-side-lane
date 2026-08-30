import subprocess
import unittest
from unittest import mock

from side_lane.auth import auth_status, require_native_oauth, AuthError


class AuthTests(unittest.TestCase):
    def test_codex_oauth_and_api_key_methods(self) -> None:
        oauth = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in with ChatGPT", ""))
        self.assertTrue(auth_status("codex", runner=oauth).ready)
        key = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Logged in with API key SECRET", ""))
        status = auth_status("codex", runner=key)
        self.assertEqual(status.method, "api-key")
        self.assertNotIn("SECRET", str(status.as_dict()))

    def test_claude_json_uses_only_method_metadata(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true,"authMethod":"oauth","token":"NEVER"}', ""))
        status = auth_status("claude", runner=runner)
        self.assertTrue(status.ready)
        self.assertNotIn("NEVER", str(status.as_dict()))
        ambiguous = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true}', ""))
        self.assertFalse(auth_status("claude", runner=ambiguous).ready)

    def test_missing_oauth_fails_with_refresh_only(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "signed out"))
        with self.assertRaisesRegex(AuthError, "codex login"):
            require_native_oauth("codex", runner=runner)


if __name__ == "__main__":
    unittest.main()
