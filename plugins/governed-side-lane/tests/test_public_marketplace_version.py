"""Reject stale marketplace metadata even when the plugin manifest is current."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class MarketplaceVersionTests(unittest.TestCase):
    def test_stale_marketplace_version_is_rejected(self):
        if not (ROOT / "scripts/validate_public_package.py").is_file():
            self.skipTest("public repository validator is not part of vendored plugin")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "package"
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc"))
            path = root / ".claude-plugin/marketplace.json"
            payload = json.loads(path.read_text())
            entry = next(item for item in payload["plugins"]
                         if item["name"] == "governed-side-lane")
            entry["version"] = "0.0.0"
            path.write_text(json.dumps(payload))
            result = subprocess.run(
                [sys.executable, "scripts/validate_public_package.py"],
                cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("wrong or missing Claude marketplace plugin version", result.stderr)
