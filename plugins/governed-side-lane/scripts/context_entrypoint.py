#!/usr/bin/env python3
"""Reversibly install shared-context pointers without clobbering user files."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


START = "<!-- governed-side-lane:shared-context:start -->"
END = "<!-- governed-side-lane:shared-context:end -->"


def block(source: Path) -> str:
    return (
        f"{START}\n"
        "## Shared Codex/Claude project context\n\n"
        f"Read and follow [{source.name}]({source.as_posix()}) for the shared "
        "cross-host context protocol. Repository `AGENTS.md` and its required "
        "authoritative `CLAUDE.md` remain controlling.\n"
        f"{END}"
    )


def managed_text(text: str, source: Path, operation: str) -> tuple[str, bool]:
    start = text.find(START)
    end = text.find(END)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise ValueError("malformed managed shared-context block")
    replacement = block(source)
    if start >= 0:
        finish = end + len(END)
        if operation == "uninstall":
            updated = (text[:start].rstrip() + "\n" + text[finish:].lstrip()).strip()
            return (updated + "\n" if updated else ""), True
        updated = text[:start] + replacement + text[finish:]
        return updated, updated != text
    if operation == "uninstall":
        return text, False
    separator = "\n\n" if text.strip() else ""
    return text.rstrip() + separator + replacement + "\n", True


def target(home: Path, host: str, codex_home: Path | None = None) -> Path:
    if host == "codex":
        return (codex_home or (home / ".codex")) / "AGENTS.md"
    return home / ".claude/CLAUDE.md"


def act(operation: str, host: str, source: Path, home: Path, codex_home: Path | None = None) -> int:
    destinations = [target(home, host, codex_home)] if host != "both" else [target(home, "codex", codex_home), target(home, "claude", codex_home)]
    problems = 0
    for destination in destinations:
        existing = destination.read_text(encoding="utf-8") if destination.is_file() else ""
        try:
            updated, changed = managed_text(existing, source, operation)
        except ValueError as exc:
            print(f"conflict {destination}: {exc}", file=sys.stderr)
            problems += 1
            continue
        if operation == "check":
            expected, _ = managed_text(existing, source, "install")
            ok = START in existing and END in existing and expected == existing
            print(f"{'ok' if ok else 'missing'}    shared context in {destination}")
            problems += 0 if ok else 1
        elif changed:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(updated, encoding="utf-8")
            print(f"{'updated' if operation == 'install' else 'removed'}  shared context in {destination}")
        else:
            print(f"unchanged shared context in {destination}")
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("install", "check", "uninstall"))
    parser.add_argument("host", choices=("codex", "claude", "both"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"shared context source is absent: {source}")
    codex_home = args.codex_home.expanduser().resolve() if args.codex_home else None
    raise SystemExit(act(args.operation, args.host, source, args.home.expanduser().resolve(), codex_home))


if __name__ == "__main__":
    main()
