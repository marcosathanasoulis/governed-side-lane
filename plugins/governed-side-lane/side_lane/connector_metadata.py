"""Extract connector names without materializing configuration values."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO


class _JsonNames:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.pending = ""
        self.names: set[str] = set()

    def _get(self) -> str:
        if self.pending:
            value, self.pending = self.pending, ""
            return value
        return self.stream.read(1)

    def _nonspace(self) -> str:
        value = self._get()
        while value and value.isspace():
            value = self._get()
        return value

    def _string(self, *, retain: bool) -> str:
        value: list[str] = []
        escaped = False
        while True:
            char = self._get()
            if not char:
                raise ValueError("unterminated JSON string")
            if escaped:
                if retain:
                    value.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return "".join(value)
            elif retain:
                value.append(char)

    def _value(self, first: str | None = None, *, capture_names: bool = False) -> None:
        char = first or self._nonspace()
        if char == "{":
            self._object(capture_names=capture_names)
        elif char == "[":
            self._array()
        elif char == '"':
            self._string(retain=False)
        else:
            while char and char not in ",]}":
                char = self._get()
            self.pending = char

    def _array(self) -> None:
        char = self._nonspace()
        if char == "]":
            return
        while char:
            self._value(char)
            char = self._nonspace()
            if char == "]":
                return
            if char != ",":
                raise ValueError("invalid JSON array")
            char = self._nonspace()
        raise ValueError("unterminated JSON array")

    def _object(self, *, capture_names: bool = False) -> None:
        char = self._nonspace()
        if char == "}":
            return
        while char:
            if char != '"':
                raise ValueError("invalid JSON object key")
            key = self._string(retain=True)
            if self._nonspace() != ":":
                raise ValueError("invalid JSON object")
            first = self._nonspace()
            if capture_names:
                self.names.add(key)
            self._value(first, capture_names=(key == "mcpServers"))
            char = self._nonspace()
            if char == "}":
                return
            if char != ",":
                raise ValueError("invalid JSON object")
            char = self._nonspace()
        raise ValueError("unterminated JSON object")

    def parse(self) -> set[str]:
        self._value()
        return self.names


def json_mcp_names(path: Path) -> set[str]:
    """Return only object keys directly under any ``mcpServers`` object."""

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return _JsonNames(stream).parse()


def toml_mcp_names(path: Path) -> set[str]:
    """Return MCP table names while discarding every non-header byte."""

    names: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        at_start = True
        header: list[str] | None = None
        while char := stream.read(1):
            if char == "\n":
                at_start, header = True, None
                continue
            if at_start and char in " \t":
                continue
            if at_start:
                at_start = False
                header = [char] if char == "[" else None
                continue
            if header is not None:
                if len(header) >= 512:
                    header = None
                else:
                    header.append(char)
                    if char == "]":
                        value = "".join(header)
                        prefix = "[mcp_servers."
                        if value.startswith(prefix) and value.endswith("]"):
                            name = value[len(prefix):-1].strip().strip('"\'')
                            if name:
                                names.add(name)
                        header = None
    return names
