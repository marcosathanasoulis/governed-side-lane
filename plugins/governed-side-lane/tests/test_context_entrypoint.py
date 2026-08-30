from pathlib import Path
import tempfile
import unittest

from scripts.context_entrypoint import END, START, act


class ContextEntrypointTests(unittest.TestCase):
    def test_install_check_uninstall_preserves_unrelated_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "agent-context.md"
            source.write_text("shared\n", encoding="utf-8")
            target = home / ".codex/AGENTS.md"
            target.parent.mkdir(parents=True)
            target.write_text("# Personal\n\nKeep this.\n", encoding="utf-8")
            self.assertEqual(act("install", "codex", source, home), 0)
            installed = target.read_text(encoding="utf-8")
            self.assertIn("Keep this.", installed)
            self.assertIn(START, installed)
            self.assertEqual(act("check", "codex", source, home), 0)
            self.assertEqual(act("uninstall", "codex", source, home), 0)
            removed = target.read_text(encoding="utf-8")
            self.assertIn("Keep this.", removed)
            self.assertNotIn(START, removed)
            self.assertNotIn(END, removed)

    def test_both_hosts_receive_same_source_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "agent-context.md"
            source.write_text("shared\n", encoding="utf-8")
            self.assertEqual(act("install", "both", source, home), 0)
            codex = (home / ".codex/AGENTS.md").read_text(encoding="utf-8")
            claude = (home / ".claude/CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(source.as_posix(), codex)
            self.assertIn(source.as_posix(), claude)

    def test_source_changes_do_not_require_global_loader_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "agent-context.md"
            source.write_text("version one\n", encoding="utf-8")
            self.assertEqual(act("install", "codex", source, home), 0)
            target = home / ".codex/AGENTS.md"
            loader = target.read_text(encoding="utf-8")

            source.write_text("version two\n", encoding="utf-8")

            self.assertEqual(act("check", "codex", source, home), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), loader)
            self.assertIn("version two", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
