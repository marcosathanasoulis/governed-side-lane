from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from side_lane import preferences


class PreferencesDirTests(unittest.TestCase):
    def test_posix_uses_xdg_config_home_when_set(self) -> None:
        path = preferences.preferences_dir(
            environ={"XDG_CONFIG_HOME": "/tmp/xdg"}, home=Path("/home/dev"), system="Linux"
        )
        self.assertEqual(path, Path("/tmp/xdg/governed-side-lane"))

    def test_posix_defaults_to_dot_config(self) -> None:
        path = preferences.preferences_dir(environ={}, home=Path("/home/dev"), system="Darwin")
        self.assertEqual(path, Path("/home/dev/.config/governed-side-lane"))

    def test_windows_uses_appdata(self) -> None:
        path = preferences.preferences_dir(
            environ={"APPDATA": r"C:\Users\dev\AppData\Roaming"},
            home=Path(r"C:\Users\dev"), system="Windows",
        )
        self.assertEqual(path, Path(r"C:\Users\dev\AppData\Roaming") / "governed-side-lane")

    def test_windows_falls_back_without_appdata(self) -> None:
        path = preferences.preferences_dir(environ={}, home=Path("/home/dev"), system="Windows")
        self.assertEqual(path, Path("/home/dev/AppData/Roaming/governed-side-lane"))


class PreferencesStoreTests(unittest.TestCase):
    def test_missing_file_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            self.assertEqual(preferences.load_preferences(path), {})

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "preferences.json"
            preferences.save_usage("claude", "extra-usage", path)
            preferences.save_usage("codex", "included-oauth", path)
            self.assertEqual(
                preferences.load_preferences(path),
                {"claude": "extra-usage", "codex": "included-oauth"},
            )

    def test_saved_file_is_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            preferences.save_usage("claude", "unknown", path)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_rewriting_one_host_preserves_the_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            preferences.save_usage("claude", "extra-usage", path)
            preferences.save_usage("claude", "included-oauth", path)
            self.assertEqual(preferences.load_preferences(path), {"claude": "included-oauth"})

    def test_unsupported_host_or_state_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with self.assertRaisesRegex(preferences.PreferencesError, "unsupported host"):
                preferences.save_usage("devin", "unknown", path)
            with self.assertRaisesRegex(preferences.PreferencesError, "unsupported usage state"):
                preferences.save_usage("claude", "on-the-house", path)

    def test_malformed_file_reads_as_empty_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text("not json at all", encoding="utf-8")
            self.assertEqual(preferences.load_preferences(path), {})

    def test_merge_explicit_wins_over_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            preferences.save_usage("claude", "extra-usage", path)
            preferences.save_usage("codex", "included-oauth", path)
            merged = preferences.merge_host_cost_state({"claude": "included-oauth"}, path=path)
            self.assertEqual(merged, {"claude": "included-oauth", "codex": "included-oauth"})

    def test_merge_with_no_saved_preferences_returns_explicit_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            merged = preferences.merge_host_cost_state({"claude": "extra-usage"}, path=path)
            self.assertEqual(merged, {"claude": "extra-usage"})


if __name__ == "__main__":
    unittest.main()
