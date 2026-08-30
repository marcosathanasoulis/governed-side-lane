from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class WindowsScriptTests(unittest.TestCase):
    def test_installer_uses_manifest_and_refuses_unrelated_destinations(self) -> None:
        text = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("install-manifest.json", text)
        self.assertIn("Refusing unrelated destination", text)
        self.assertIn("$KeepRunner", text)
        self.assertIn(".codex\\skills\\side-lane", text)
        self.assertIn("Get-ManagedHash", text)
        self.assertIn("schema_version = 2", text)
        self.assertIn("Refusing to remove modified managed destination", text)
        self.assertIn("Remove-Item -LiteralPath $item.Destination", text)
        self.assertIn("prompt-it-side-lane-routing", text)

    def test_credential_script_uses_native_vault_and_secure_prompt(self) -> None:
        text = (ROOT / "scripts" / "credential.ps1").read_text(encoding="utf-8")
        self.assertIn("CredWriteW", text)
        self.assertIn("-AsSecureString", text)
        self.assertNotIn("Write-Output $secure", text)

if __name__ == "__main__":
    unittest.main()
