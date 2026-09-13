"""Redact the selected provider credential before output is parsed or retained.

Cover exact values, contiguous fragments of at least eight characters, and
prefix/suffix displays with an explicit mask and at least four known characters.
This is defense in depth, not an arbitrary-encoding or exfiltration detector.
Never deliberately print credentials, including short prefixes or suffixes.
"""
from __future__ import annotations

import re

MARKER = '[REDACTED_PROVIDER_KEY]'
MIN_FRAGMENT = 8
MASKED = re.compile(r'([A-Za-z0-9_+/=-]*)(\.{3,}|…|\*{2,})([A-Za-z0-9_+/=-]*)')


def redact_provider_secret(value: object, secret: str | None) -> str:
    text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or '')
    if not secret:
        return text
    text = text.replace(secret, MARKER)

    def masked(match: re.Match) -> str:
        left, _, right = match.groups()
        if ((not left or secret.startswith(left)) and (not right or secret.endswith(right))
                and max(len(left), len(right)) >= 4):
            return MARKER
        return match.group(0)

    text = MASKED.sub(masked, text)
    windows: dict[str, list[int]] = {}
    for offset in range(len(secret) - MIN_FRAGMENT + 1):
        windows.setdefault(secret[offset:offset + MIN_FRAGMENT], []).append(offset)

    def fragments(candidate: str) -> str:
        spans = []
        offset = 0
        while offset <= len(candidate) - MIN_FRAGMENT:
            size = 0
            for start in windows.get(candidate[offset:offset + MIN_FRAGMENT], ()):
                length = MIN_FRAGMENT
                while (offset + length < len(candidate) and start + length < len(secret)
                       and candidate[offset + length] == secret[start + length]):
                    length += 1
                size = max(size, length)
            if size:
                spans.append((offset, offset + size))
                offset += size
            else:
                offset += 1
        if not spans:
            return candidate
        pieces = []
        end = 0
        for start, stop in spans:
            pieces.extend((candidate[end:start], MARKER))
            end = stop
        pieces.append(candidate[end:])
        return ''.join(pieces)

    return fragments(text)
