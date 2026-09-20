"""Coordinator-supplied read-only directory roots for execute lanes.

Devin's file-tool grants are scoped to its worktree; execute lanes are not
OS sandboxes, and other hosts retain their existing filesystem access. Some lanes need to
*read* files that live elsewhere: most often the shared instruction sources a
repository's own ``AGENTS.md``/``CLAUDE.md`` point at, which for a lane
worktree can live in the coordinator's main checkout. A coordinator grants
each such directory explicitly with ``--read-root``.

A grant is read-only by construction:

- a root becomes a ``Read(<root>/**)`` rule and never a ``Write(...)`` rule;
- the root must be an absolute, already existing directory;
- glob metacharacters and the rule delimiters ``(``/``)`` are rejected, so a
  root can neither widen into a wildcard nor terminate the rule early;
- the filesystem root is rejected, because ``/**`` over it *is* the broad read
  wildcard this module exists to avoid.

Both checks run twice: once against the string the coordinator typed (before
any filesystem lookup) and once against the canonical path that is actually
rendered, because resolving a symlink can introduce characters the string did
not contain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


#: Characters a glob-based permission rule acts on. A root containing one would
#: be matched as a pattern instead of a literal directory.
GLOB_METACHARACTERS = frozenset("*?[]{}")
#: Characters that delimit a rule's own syntax. A root containing one could
#: close the rule early and leave the remainder as unparsed granted text.
RULE_DELIMITERS = frozenset("()")
UNSAFE_CHARACTERS = GLOB_METACHARACTERS | RULE_DELIMITERS


class ReadRootError(ValueError):
    """A coordinator-supplied read root is unusable or unsafe to grant."""


def _unsafe(text: str) -> list[str]:
    found = {character for character in text if character in UNSAFE_CHARACTERS}
    found.update(
        character for character in text if ord(character) < 32 or ord(character) == 127
    )
    return sorted(found)


def _reject_unsafe(text: str, label: str) -> None:
    characters = _unsafe(text)
    if characters:
        rendered = ", ".join(repr(character) for character in characters)
        raise ReadRootError(
            f"{label} contains characters a permission rule cannot carry safely: {rendered}"
        )


def _is_filesystem_root(path: Path) -> bool:
    text = str(path)
    return not text or text == path.anchor


def parse_read_roots(values: "Sequence[str] | None") -> tuple[Path, ...]:
    """Validate coordinator-supplied read roots into canonical directories.

    Returns an empty tuple when nothing was requested. Duplicates collapse, and
    the result is sorted so the rendered rules and the audit record are
    deterministic. Every rejection is fatal: a lane never proceeds with a read
    root the coordinator did not successfully name.
    """

    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ReadRootError("read roots must be a list of paths")
    resolved: set[Path] = set()
    for value in values:
        if not isinstance(value, str) or not value or not value.strip():
            raise ReadRootError("read root must be a non-empty path")
        # Deliberately not stripped: leading or trailing whitespace is part of
        # a POSIX path, and trimming it would grant a different directory than
        # the coordinator named.
        _reject_unsafe(value, f"read root {value!r}")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ReadRootError(f"read root must be an absolute path: {value}")
        try:
            root = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReadRootError(
                f"read root is not an existing directory: {value}"
            ) from exc
        if not root.is_dir():
            raise ReadRootError(f"read root is not a directory: {value}")
        _reject_unsafe(str(root), f"resolved read root for {value!r}")
        if _is_filesystem_root(root):
            raise ReadRootError(f"refusing a filesystem-wide read root: {root}")
        resolved.add(root)
    return tuple(sorted(resolved, key=str))


def read_rule(root: Path) -> str:
    """Render the one exact read rule a granted root may produce.

    Re-checks the path that reaches the rule rather than trusting an earlier
    caller, so an adapter called directly with a synthetic path still fails
    closed instead of emitting a wildcard or an early-terminated rule.
    """

    text = str(root)
    _reject_unsafe(text, f"read root {text!r}")
    if not Path(text).is_absolute():
        raise ReadRootError(f"read root must be an absolute path: {text}")
    if _is_filesystem_root(Path(text)):
        raise ReadRootError(f"refusing a filesystem-wide read root: {text}")
    return f"Read({text}/**)"


def scope_note(roots: Sequence[Path]) -> str:
    """Worker-instruction text naming the coordinator's read-only scope.

    The note states the grant the coordinator made; it does not claim to be the
    enforcing control, because what each host can enforce differs (see
    ``docs/automatic-side-lane/scoped-worker-reads.md``). Empty when no root
    was requested, so an ordinary lane's instructions are unchanged.
    """

    if not roots:
        return ""
    lines = [
        "## Coordinator-granted read-only scope",
        "",
        "Write only inside your own lane worktree. The coordinator has also named",
        "these existing directories as read-only context for this task:",
        "",
    ]
    lines.extend(f"- `{root}`" for root in roots)
    lines.extend(
        [
            "",
            "They are read-only: read them for context, and never create, modify,",
            "or delete anything in them. A directory outside this list is not part",
            "of the task's scope.",
        ]
    )
    return "\n".join(lines)
