#!/usr/bin/env python3
"""Validate the staged public package without network or provider access."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "governed-side-lane"
BANNED = (
    "credible" + "mind",
    "project-" + "lifeview",
    "/users/" + "marcos",
    "google-cloud-" + "functions",
    "serverless-" + "functions",
)
REQUIRED = (
    ROOT / "LICENSE",
    ROOT / "SECURITY.md",
    ROOT / ".claude-plugin" / "marketplace.json",
    ROOT / ".agents" / "plugins" / "marketplace.json",
    PLUGIN / ".claude-plugin" / "plugin.json",
    PLUGIN / ".codex-plugin" / "plugin.json",
    PLUGIN / "skills" / "side-lane" / "SKILL.md",
    PLUGIN / "skills" / "prompt-it-side-lane-routing" / "SKILL.md",
    PLUGIN / "bin" / "side-lane",
    PLUGIN / "config" / "lane-governance.md",
)


def main() -> int:
    problems: list[str] = []
    for path in REQUIRED:
        if not path.is_file():
            problems.append(f"missing required file: {path.relative_to(ROOT)}")

    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        for marker in BANNED:
            if marker in lowered:
                problems.append(
                    f"private marker {marker!r}: {path.relative_to(ROOT)}"
                )

    manifests = (
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
        PLUGIN / ".claude-plugin" / "plugin.json",
        PLUGIN / ".codex-plugin" / "plugin.json",
    )
    for path in manifests:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"invalid JSON {path.relative_to(ROOT)}: {exc}")
            continue
        if path.name == "plugin.json":
            if payload.get("name") != "governed-side-lane":
                problems.append(f"wrong plugin name: {path.relative_to(ROOT)}")
            if payload.get("version") != "0.2.1":
                problems.append(f"wrong plugin version: {path.relative_to(ROOT)}")
            if payload.get("license") != "Apache-2.0":
                problems.append(f"wrong plugin license: {path.relative_to(ROOT)}")

    version_files = (ROOT / "VERSION", PLUGIN / "VERSION")
    for path in version_files:
        if path.is_file() and path.read_text(encoding="utf-8").strip() != "0.2.1":
            problems.append(f"wrong version file: {path.relative_to(ROOT)}")
    source_metadata = PLUGIN / ".side-lane-source.json"
    if source_metadata.is_file():
        try:
            source_payload = json.loads(source_metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"invalid JSON {source_metadata.relative_to(ROOT)}: {exc}")
        else:
            if source_payload.get("version") != "0.2.1":
                problems.append("wrong source metadata version")

    if problems:
        print("public package validation failed:", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("public package validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
