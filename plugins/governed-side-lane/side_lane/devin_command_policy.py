"""Command-policy helpers used by the native Devin pre-tool hook."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import shlex
import sys
from typing import Sequence


EXEC_TOOL_NAME = "exec"
# Devin's documented file-mutating tools: the permissions reference names the
# "file edits via the `edit`/`write` tools", and str_replace is the
# str_replace-style edit variant some CLI builds expose. All three take the
# target path as their primary tool-input key.
FILE_MUTATING_TOOL_NAMES = ("write", "edit", "str_replace")
COVERED_TOOL_NAMES = (EXEC_TOOL_NAME, *FILE_MUTATING_TOOL_NAMES)
WRITE_TARGET_KEYS = ("file_path", "path", "file")


def policy_hook_matcher() -> str:
    """Regex routing every covered tool to this policy in the hook config."""

    return "^(" + "|".join(COVERED_TOOL_NAMES) + ")$"


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


def bare_rule_pattern(pattern: str | None) -> str | None:
    """Return the argument-free command a ``<command> *`` pattern also grants."""

    if not pattern or not pattern.endswith(" *"):
        return None
    bare = " ".join(pattern[:-2].split())
    if not bare or any(character in bare for character in "*?["):
        return None
    return bare


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
        # ``Bash(git status *)`` grants the bare command too: Claude's glob
        # reads "this command with any arguments", but fnmatch requires the
        # literal space, so ``git status`` alone would otherwise fall outside
        # the grant while ``git status --short`` passes.
        bare = bare_rule_pattern(pattern)
        if bare and (normalized == bare
                     or (anywhere and fnmatchcase(normalized, f"*{bare}"))):
            return rule
    return None


def _is_shell_expandable(token: str) -> bool:
    """Report whether a raw token could expand differently once a shell runs it.

    ``shlex.split``/``Path`` treat ``$HOME``, ``` `pwd` ```, ``$(pwd)`` and a
    leading ``~`` as literal characters, so a target built from one of these
    resolves as an in-worktree-looking path here while the real shell (or a
    provider that re-parses the string) expands it to something else, e.g.
    the user's home directory. Any such token must be rejected before path
    resolution rather than trusted.
    """

    if not token:
        return False
    if token[0] == "~":
        return True
    return "$" in token or "`" in token


def _inside_lane_worktree(target: str, resolved_worktree: Path) -> bool:
    """Resolve a tool target and report whether it lands inside the worktree.

    Relative paths are anchored to the lane worktree (the CLI's working
    directory). ``resolve()`` follows symlinks, so a link planted inside the
    worktree that points elsewhere resolves to its destination and fails.
    A token containing shell-expansion syntax (``$HOME``, ``` `pwd` ``,
    ``$(pwd)``, a leading ``~``) is treated as outside the worktree: it must
    fail closed here since a real shell would expand it after this check.
    """

    if _is_shell_expandable(target):
        return False
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_worktree / candidate
    resolved = candidate.resolve()
    return resolved == resolved_worktree or resolved.is_relative_to(resolved_worktree)


def _strip_lane_worktree_dash_c(command: str, worktree: str | None) -> str:
    """Drop a leading ``git -C <lane-worktree>`` for canonical rule matching.

    Providers naturally qualify git with the lane path
    (``git -C /lane log --oneline -5``), which must match the canonical
    ``Bash(git log *)`` grants. Only a ``-C`` target that resolves inside the
    lane worktree is stripped; any other ``-C`` target keeps the prefix and is
    evaluated as-is, so it passes only under a literally matching rule.
    """

    if not worktree:
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) < 4 or tokens[0] != "git" or tokens[1] != "-C":
        return command
    if not _inside_lane_worktree(tokens[2], Path(worktree).resolve()):
        return command
    return " ".join([tokens[0], *tokens[3:]])


def _evaluate_exec(payload: dict, allowed: Sequence[str], denied: Sequence[str],
                   worktree: str | None) -> dict[str, str] | None:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return {"decision": "block", "reason": "exec command missing from tool input"}
    unsafe_reason = unsafe_shell_syntax(command)
    if unsafe_reason is not None:
        return {"decision": "block", "reason": f"{unsafe_reason} is not permitted"}
    granted_command = _strip_lane_worktree_dash_c(command, worktree)
    matched = matching_rule(granted_command, denied, anywhere=True)
    if matched is not None:
        return {"decision": "block", "reason": f"command denied by canonical rule: {matched}"}
    if matching_rule(granted_command, allowed) is not None:
        return None
    return {"decision": "block", "reason": "command is outside canonical capability grants"}


def _evaluate_write(payload: dict, worktree: str | None) -> dict[str, str] | None:
    tool_name = payload["tool_name"]
    tool_input = payload.get("tool_input")
    target: str | None = None
    if isinstance(tool_input, dict):
        for key in WRITE_TARGET_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                target = value
                break
    if target is None:
        return {"decision": "block",
                "reason": f"{tool_name} target path missing from tool input"}
    if not worktree:
        return {"decision": "block",
                "reason": f"{tool_name} requires the lane worktree in the policy rules"}
    if not _inside_lane_worktree(target, Path(worktree).resolve()):
        return {"decision": "block", "reason":
                "writes outside the lane worktree are not permitted; "
                f"use {worktree}/.side-lane-scratch/ for scratch files"}
    return None


def evaluate_event(payload: object, allowed: Sequence[str],
                   denied: Sequence[str], *, worktree: str | None = None
                   ) -> dict[str, str] | None:
    """Evaluate Devin's documented PreToolUse event shape.

    ``exec`` commands are matched against the canonical Bash rules; every
    covered file-mutating tool is contained to ``worktree``. A hook block
    returns the decision to the model, which can continue; anything the hook
    does not cover stays blocked as an invalid event.
    """

    if not isinstance(payload, dict) or payload.get("tool_name") not in COVERED_TOOL_NAMES:
        return {"decision": "block", "reason": "invalid hook event"}
    if payload["tool_name"] == EXEC_TOOL_NAME:
        return _evaluate_exec(payload, allowed, denied, worktree)
    return _evaluate_write(payload, worktree)


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
    worktree = rules.get("worktree")
    if worktree is not None and (not isinstance(worktree, str) or not worktree.strip()):
        return 2
    decision = evaluate_event(payload, allowed, denied, worktree=worktree)
    if decision is not None:
        print(json.dumps(decision))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
