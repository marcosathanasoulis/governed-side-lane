"""Command-policy helpers used by the native Devin pre-tool hook."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import sys
from typing import Sequence


def bash_rule_pattern(rule: str) -> str | None:
    """Return the command pattern from one canonical ``Bash(...)`` rule."""

    if not rule.startswith("Bash(") or not rule.endswith(")"):
        return None
    pattern = rule[5:-1].strip()
    return pattern or None


def devin_exec_rule(rule: str) -> str | None:
    """Translate a canonical Bash rule when Devin can express the same prefix."""

    pattern = bash_rule_pattern(rule)
    if pattern is None:
        return None
    if pattern.endswith(" *"):
        pattern = pattern[:-2].rstrip()
    # Devin Exec rules are command prefixes, not globs. An embedded wildcard
    # cannot be translated without either losing the grant or widening it.
    if any(character in pattern for character in "*?["):
        return None
    return f"Exec({pattern})"


def devin_exec_deny_rule(rule: str) -> str | None:
    """Translate a deny rule when a leading Devin prefix blocks it safely."""

    pattern = bash_rule_pattern(rule)
    if pattern is None:
        return None
    # A final Claude glob means "anything continuing this prefix". Dropping
    # it for a Devin deny remains conservative because Exec is itself a prefix
    # matcher. Embedded globs still require the PreToolUse policy hook.
    if pattern.endswith("*"):
        pattern = pattern[:-1].rstrip()
    if not pattern or any(character in pattern for character in "*?["):
        return None
    return f"Exec({pattern})"


def unsafe_shell_syntax(command: str) -> str | None:
    """Find shell composition or substitution outside literal quoting."""

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote is None and character == "'":
            quote = "'"
            index += 1
            continue
        # Backticks and $(...) remain active inside double quotes. Process
        # substitutions are active only outside quotes.
        if character == "`" or (character == "$" and command[index:index + 2] == "$("):
            return "command substitution"
        if quote is None and command[index:index + 2] in {"<(", ">("}:
            return "process substitution"
        if quote is None and character in ";&|\r\n":
            return "compound shell syntax"
        index += 1
    if escaped or quote is not None:
        return "malformed shell quoting"
    return None


def matching_rule(command: object, rules: Sequence[str], *, anywhere: bool = False) -> str | None:
    """Return the first canonical rule matching a command."""

    if not isinstance(command, str):
        return None
    normalized = " ".join(command.split())
    for rule in rules:
        pattern = bash_rule_pattern(rule)
        if pattern and (fnmatchcase(normalized, pattern)
                        or (anywhere and fnmatchcase(normalized, f"*{pattern}"))):
            return rule
    return None


def evaluate_event(payload: object, allowed: Sequence[str],
                   denied: Sequence[str]) -> dict[str, str] | None:
    """Evaluate Devin's documented PreToolUse event shape."""

    if not isinstance(payload, dict) or payload.get("tool_name") != "exec":
        return {"decision": "block", "reason": "invalid exec hook event"}
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return {"decision": "block", "reason": "exec command missing from tool input"}
    unsafe_reason = unsafe_shell_syntax(command)
    if unsafe_reason is not None:
        return {"decision": "block", "reason": f"{unsafe_reason} is not permitted"}
    matched = matching_rule(command, denied, anywhere=True)
    if matched is not None:
        return {"decision": "block", "reason": f"command denied by canonical rule: {matched}"}
    if matching_rule(command, allowed) is not None:
        return None
    return {"decision": "block", "reason": "command is outside canonical capability grants"}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 2
    try:
        rules = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 2
    if not isinstance(rules, dict):
        return 2
    allowed = rules.get("allowed")
    denied = rules.get("denied")
    if (not isinstance(allowed, list) or not all(isinstance(rule, str) for rule in allowed)
            or not isinstance(denied, list) or not all(isinstance(rule, str) for rule in denied)):
        return 2
    decision = evaluate_event(payload, allowed, denied)
    if decision is not None:
        print(json.dumps(decision))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
