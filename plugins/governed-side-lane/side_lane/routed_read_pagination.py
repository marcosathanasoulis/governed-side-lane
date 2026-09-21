"""Read-pagination helpers used by the routed Claude pre-tool hook.

A routed Claude execute lane installs this module as a ``PreToolUse`` hook so
a single native ``Read`` call cannot return an unbounded amount of text. The
hook only rewrites the ``offset``/``limit`` line range — it emits no
permission decision — so the call continues through the normal permission
flow and the remainder of the file stays reachable through further ranges.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


READ_TOOL_NAME = "Read"
# Per-call line bound applied when the per-run config does not set one. This
# is a line count, not a byte cap: a bounded read of very long lines can
# still return more text than the bound suggests.
DEFAULT_LINE_BOUND = 200

# ``Read`` dispatches these extensions to non-text handlers (PDF pages, image
# payloads, notebook cells) whose tool input carries no line range. A
# line-range rewrite must never be applied to them, so they keep their
# existing path through the tool.
NON_TEXT_EXTENSIONS = frozenset({
    ".pdf", ".ipynb",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff",
})


def hook_matcher() -> str:
    """Exact-match ``PreToolUse`` matcher routing only ``Read`` to this hook."""

    return READ_TOOL_NAME


def _integer_value(value: object, minimum: int) -> int | None:
    """Return ``value`` as an ``int`` when it is a finite integer at least ``minimum``.

    Accepts an ``int`` or a finite ``float`` whose value is integral, and
    returns ``None`` for everything else — booleans, strings, ``None``, NaN,
    infinity, and non-integral floats. No rejected value ever reaches
    ``int()``, so no conversion can raise on caller input.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= minimum else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        number = int(value)
        return number if number >= minimum else None
    return None


def _configured_bound(config: object) -> int:
    if isinstance(config, Mapping):
        bound = _integer_value(config.get("limit"), 1)
        if bound is not None:
            return bound
    return DEFAULT_LINE_BOUND


def evaluate_event(payload: object, bound: int = DEFAULT_LINE_BOUND
                   ) -> dict[str, Any] | None:
    """Return a ``PreToolUse`` ``updatedInput`` rewrite for an unbounded Read.

    ``None`` leaves the call exactly as Claude sent it — small explicit
    reads, non-text paths, non-Read tools, and malformed events are all
    untouched. A rewrite reports no permission decision, so permission checks
    behave exactly as they do without the hook; ``additionalContext`` names
    the next range so the rest of the file stays reachable.
    """

    if not isinstance(payload, dict) or payload.get("tool_name") != READ_TOOL_NAME:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if Path(file_path).suffix.lower() in NON_TEXT_EXTENSIONS:
        return None
    raw_limit = tool_input.get("limit")
    if raw_limit is not None:
        limit = _integer_value(raw_limit, 1)
        if limit is None or limit <= bound:
            return None
    raw_offset = tool_input.get("offset")
    if raw_offset is None:
        first = 1
    else:
        offset = _integer_value(raw_offset, 0)
        if offset is None:
            return None
        first = offset
    updated = dict(tool_input)
    updated["limit"] = bound
    if raw_offset is not None:
        updated["offset"] = first
    context = (
        f"This Read was bounded by the lane's pagination hook: it runs with "
        f"offset={first} limit={bound}, so it returns at most {bound} lines. "
        "The rest of the file stays reachable through further ranges — "
        f"continue with offset={first + bound} limit={bound} for the next "
        f"{bound} lines."
    )
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
        "additionalContext": context,
    }}


def main(argv: Sequence[str] | None = None) -> int:
    """Hook entrypoint: per-run JSON config path in argv, event JSON on stdin.

    Always exits 0 — a missing config, malformed event, or any other failure
    leaves the tool call exactly as Claude sent it rather than blocking a
    non-interactive lane.
    """

    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        bound = DEFAULT_LINE_BOUND
        if len(arguments) == 1:
            try:
                bound = _configured_bound(
                    json.loads(Path(arguments[0]).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        try:
            payload = json.load(sys.stdin)
        except (OSError, UnicodeDecodeError, ValueError):
            return 0
        output = evaluate_event(payload, bound)
        if output is not None:
            print(json.dumps(output))
    except Exception:  # a hook failure must never block a Read call
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
