"""Extract connector names without materializing configuration values.

Every name returned here must be one the host itself would load. That is the
whole contract: the callers render ``mcp__<name>`` rules and refuse a run whose
per-run registration collides with an existing name, so a name read out of a
file the host rejects — or out of bytes the host reads differently — is a
registration nothing backs. The scanner therefore implements JSON's own
grammar (the behaviour the standard library parser has, and the one the hosts'
parsers implement) rather than a lenient superset of it:

* only NAMES are retained; a value is never materialized, returned or logged;
* a key is DECODED to the character it denotes, ``\\u006e`` included, because
  that is the key the host loads;
* only JSON's four whitespace characters separate tokens;
* a duplicated key resolves to its LAST value, as it does for the host, and a
  superseded value is held to JSON's SYNTAX only — the shape of the value that
  survives is the one that decides anything, so a replaced ``null`` container
  is not a refusal. That holds for the ENCLOSING keys too: a surviving
  ``projects`` object that replaces one carrying a malformed
  ``projects.<path>.mcpServers`` leaves nothing malformed to judge, so a
  registration-shape refusal is raised only where the object carrying it is the
  one the host itself ends up loading;
* only object-valued definitions under ``mcpServers`` are registrations — the
  rule the Claude adapter's own registration merge applies
  (``isinstance(definition, dict)``) — while every other object definition is
  left exactly as written (an empty or "disabled" body is not this scanner's
  to interpret);
* a registration file is one top-level JSON object, decoded as strict UTF-8.

It is stricter than the standard parser in exactly one place: the non-standard
``NaN``/``Infinity`` constants are refused rather than accepted, so the failure
direction is always "refuse the file", never "invent a name".

An ``mcpServers`` key is a container in exactly the positions the reader of
THAT file reads one at: the file's root object always, and a ``projects.<path>``
entry only where the host has that position. The Claude adapter's registration
merge is the rule here — it reads the user ``.claude.json``'s root container
and its per-project entries, and reads the lane worktree ``.mcp.json`` as its
root container or as its own top-level keys, never as a project entry — and the
Devin reader is root-only as well. The caller says which file context it is
reading (``project_entries``); this module does not infer one from a file name.
A reader with the position reads ONE entry — ``projects.<the directory the host
launches in>`` — so the shape refusal belongs to that entry alone: an entry
under any other path is never a registry the child opens, and its spelling
cannot make the file one the child refuses. A caller that knows which
directory its host launches in says so (``selected_project``); one that names
none has no selected entry, and every project entry then keeps the strict
judgement this scanner has always applied.

Anywhere else a field spelled ``mcpServers`` is ordinary data: held to JSON's
syntax and to nothing more. That is what keeps
this scanner on the registry the child loads in both directions — a definition
carrying ``"mcpServers": null`` costs neither the file nor the names beside it
(the host loads the server; this scanner used to refuse the whole file), and a
container-shaped field inside a definition declares no registration the host
would ever read (this scanner used to invent one).

Each name carries the key path of the object that declares it; the callers
decide which paths are in scope for the host they are configuring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

#: The only unquoted tokens JSON defines besides a number.
_JSON_LITERALS = frozenset({"true", "false", "null"})

#: The four whitespace characters JSON defines, and the only ones that may
#: separate tokens. ``str.isspace()`` is wider — it also accepts NBSP, vertical
#: tab, form feed and the Unicode line separators — so a scanner built on it
#: inventories names out of a document the host's own parser refuses.
_JSON_WHITESPACE = frozenset(" \t\n\r")

#: JSON's number grammar, exactly: no leading ``+``, no leading zero, and a
#: fraction or exponent only where digits follow.
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

#: The characters a JSON string may escape, and the hex digits a ``\u``
#: escape must supply four of.
_STRING_ESCAPES = frozenset('"\\/bfnrtu')
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: The character each non-``\u`` escape denotes.
_SIMPLE_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}

#: Surrogate code points. A ``\u`` escape naming a high surrogate pairs with a
#: following low one to name a single astral character; unpaired, each stands
#: for itself, which is what the standard parser keeps.
_HIGH_SURROGATES = range(0xD800, 0xDC00)
_LOW_SURROGATES = range(0xDC00, 0xE000)

#: Path marker for an array element: JSON arrays carry no key, so an object
#: found inside one is never the root object and never a project entry — no
#: host reads a registration out of an array, and the marker keeps a
#: ``projects`` key holding an array from being read as one. It is a marker
#: OBJECT rather than a string because a key is arbitrary decoded text: the
#: spelling ``"[]"`` made the legal project entry ``projects["[]"]`` occupy the
#: same position as an array element, and the registration it declares was
#: dropped. No decoder can produce this object, so no key can be confused with
#: an array position.
class _ArrayElement:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - a debug aid only
        return "<array element>"


_ARRAY_ELEMENT = _ArrayElement()

#: The one key whose entry is a registration scope of its own — where the file's
#: reader has that position: a host reads ``projects.<path>.mcpServers`` for the
#: directory it was launched in, beside the container at the file's root. Only
#: the Claude USER config does; a worktree ``.mcp.json`` and the Devin files do
#: not, so there the key is ordinary data (``project_entries``). Which entry of
#: that object the reader reads — one, not all — is ``selected_project``.
_PROJECT_ENTRY = "projects"

#: ``name -> {key path of the object that declares it}``. Every scope holds
#: decoded keys only: the array marker is never one, because the only positions
#: whose names are captured are the root's ``mcpServers`` and a
#: ``projects.<key>`` entry's, and an array element is neither (see
#: ``_declares_servers``).
_Names = dict[str, set[tuple[str, ...]]]


def _merge(target: _Names, found: _Names) -> None:
    for name, scopes in found.items():
        target.setdefault(name, set()).update(scopes)


@dataclass(frozen=True)
class _Value:
    """One parsed JSON value, as a names-only inventory sees it.

    ``object_shaped`` is what decides whether a key holding this value declares
    a server, and ``found`` carries the registrations declared anywhere inside
    the value. ``empty_object`` distinguishes ``{}`` from an object that has
    keys, which is the one shape difference a container's emptiness carries (see
    ``root_mapping_fallback``). Those are the three facts the enclosing object
    needs: no scalar, string, number or container content is ever retained.

    ``refusal`` is a registration-shape error this value WOULD be refused for,
    held rather than raised: the enclosing object may supersede it with a later
    value for the same key, and only the value the host ends up loading is the
    one its shape may be judged on. It surfaces at the top level, where the
    surviving object is known (``_parse_root``).
    """

    object_shaped: bool
    found: _Names = field(default_factory=dict)
    empty_object: bool = False
    refusal: str | None = None


class _JsonNames:
    """Streaming, values-discarding scan of one JSON registration file."""

    def __init__(
        self,
        stream: TextIO,
        *,
        project_entries: bool = True,
        selected_project: str | None = None,
    ) -> None:
        self.stream = stream
        self.pending = ""
        self._path: list["str | _ArrayElement"] = []
        self._project_entries = project_entries
        self._selected_project = selected_project

    def _get(self) -> str:
        if self.pending:
            value = self.pending[0]
            self.pending = self.pending[1:]
            return value
        return self.stream.read(1)

    def _nonspace(self) -> str:
        value = self._get()
        while value and value in _JSON_WHITESPACE:
            value = self._get()
        return value

    def _string(self, *, retain: bool) -> str:
        value: list[str] = []
        while True:
            char = self._get()
            if not char:
                raise ValueError("unterminated JSON string")
            if char == '"':
                return "".join(value)
            if char == "\\":
                decoded = self._escape()
                if retain:
                    value.append(decoded)
                continue
            if char < " ":
                raise ValueError("unescaped control character in JSON string")
            if retain:
                value.append(char)

    def _escape(self) -> str:
        """Consume one escape sequence, returning the character it denotes.

        Names are DECODED rather than retained as written: the host loads the
        key ``git\\u006eexus`` as ``gitnexus``, so a scanner that kept the
        spelling would inventory a server the host does not have and miss the
        one it does. Every escape JSON does not define is refused — including a
        ``\\u`` escape without four hex digits — because that is part of
        whether the file is one the host would load at all.
        """

        char = self._get()
        if char not in _STRING_ESCAPES:
            raise ValueError("invalid JSON string escape")
        if char != "u":
            return _SIMPLE_ESCAPES[char]
        code = int(self._hex_escape(), 16)
        if code not in _HIGH_SURROGATES:
            return chr(code)
        # A high surrogate names one character only together with a following
        # low ``\u`` escape. The lookahead is consumed when it IS that pair;
        # otherwise every character read is pushed back and the lone surrogate
        # stands for itself, exactly as the standard parser keeps it.
        escaped = self._get()
        if escaped == "\\":
            marker = self._get()
            if marker == "u":
                low_digits = self._hex_escape()
                low = int(low_digits, 16)
                if low in _LOW_SURROGATES:
                    return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
                self.pending = "\\u" + low_digits + self.pending
                return chr(code)
            self.pending = "\\" + marker + self.pending
            return chr(code)
        self.pending = escaped + self.pending
        return chr(code)

    def _hex_escape(self) -> str:
        """The four hex digits of a ``\\u`` escape, or a refusal."""

        digits = "".join(self._get() for _ in range(4))
        if len(digits) != 4 or any(digit not in _HEX_DIGITS for digit in digits):
            raise ValueError("invalid JSON unicode escape")
        return digits

    def _value(
        self, first: str | None = None, *, capture_names: bool = False
    ) -> _Value:
        """Parse one value, recording names when ``capture_names`` is set.

        The value's SHAPE is not judged here. A value may be superseded by a
        later occurrence of the same key, and only the value that survives is
        the one the host loads — so a superseded ``mcpServers`` need not be an
        object at all. Its JSON SYNTAX is still enforced: every branch below
        refuses a malformed token, whichever key it belongs to. The surviving
        container's shape is checked by the enclosing object once its keys are
        resolved (``_object``).
        """

        char = first or self._nonspace()
        if char == "{":
            return self._object(capture_names=capture_names)
        if char == "[":
            return self._array()
        if char == '"':
            self._string(retain=False)
            return _Value(False)
        return self._primitive(char)

    def _primitive(self, first: str) -> _Value:
        """Consume one literal or number, refusing any other unquoted token.

        Values are discarded, so this used to be a bare skip to the next
        delimiter: ``truX``, a stray word and a malformed number all read as if
        the file were valid JSON. Names may only be taken from bytes the
        host's own parser accepts, so the token is held to JSON's grammar —
        the three literals, or a number in its exact form. Only the four
        whitespace characters JSON defines end a token, as they do in JSON, and
        the terminator is pushed back for the caller to skip.
        """

        token = [first]
        char = self._get()
        while char and char not in _JSON_WHITESPACE and char not in ",]}":
            token.append(char)
            char = self._get()
        self.pending = char
        text = "".join(token)
        if text not in _JSON_LITERALS and not _JSON_NUMBER.fullmatch(text):
            raise ValueError("invalid JSON value")
        return _Value(False)

    def _array(self) -> _Value:
        found: _Names = {}
        char = self._nonspace()
        if char == "]":
            return _Value(False, found)
        while char:
            self._path.append(_ARRAY_ELEMENT)
            try:
                value = self._value(char)
            finally:
                self._path.pop()
            _merge(found, value.found)
            char = self._nonspace()
            if char == "]":
                return _Value(False, found)
            if char != ",":
                raise ValueError("invalid JSON array")
            char = self._nonspace()
        raise ValueError("unterminated JSON array")

    def _declares_servers(self) -> bool:
        """Whether the object being parsed is one a host reads servers on.

        The file's root object always is. A ``projects`` entry (the value of a
        key of the root's ``projects`` object) is, only for a file whose reader
        has that position — ``project_entries`` — because a file whose reader is
        root-only declares no registration there however the bytes are spelled.
        Every other object — a server DEFINITION above all, but also an array
        element and any other key's object — has no ``mcpServers`` container to a
        host reader, so a field spelled that way there is data rather than a
        second registry: its keys are not names, and its shape is not a
        registration file's shape to judge.
        """

        path = self._path
        if not path:
            return True
        return (
            self._project_entries
            and len(path) == 2
            and path[0] == _PROJECT_ENTRY
            and path[1] is not _ARRAY_ELEMENT
        )

    def _judges_shape(self) -> bool:
        """Whether a non-object container HERE may refuse the whole file.

        The root object's always may. A ``projects.<path>`` entry's may only
        where this scanner knows which entry the reader selects: a host reads
        the entry for the directory it was launched in and no other, so an
        entry under a different path is not a registry the child opens, and a
        malformed container there is not input that can make the file one the
        child refuses. ``selected_project`` carries that directory;
        ``None`` — no directory named, so no entry selected — leaves every
        project entry under the strict judgement, which is the conservative
        reading for a caller that cannot say which directory is in play.
        """

        if not self._path:
            return True
        if self._selected_project is None:
            return True
        return (
            len(self._path) == 2
            and self._path[0] == _PROJECT_ENTRY
            and self._path[1] == self._selected_project
        )

    def _object(self, *, capture_names: bool = False, root_mapping: bool = False) -> _Value:
        """Parse one JSON object, keeping only the registrations inside it.

        With ``capture_names`` set this object IS an ``mcpServers`` container,
        so each of its keys is a server name and is recorded when — and only
        when — the value it resolves to is an object. ``per_key`` holds one
        entry per key and is overwritten in place, so a key that appears twice
        keeps its LAST value: the value the host itself loads. That applies at
        every level, so a replaced ``mcpServers`` container, project entry or
        server definition contributes nothing of what it superseded — and the
        surviving ``mcpServers`` value is the only one whose shape is judged,
        which is why the decision is made here, from the surviving value, and
        raised only once this object is known to be one the host loads.

        The name ``mcpServers`` is read as a container only where the enclosing
        object is one a host reads it on (``_declares_servers``). Everywhere
        else the key is ordinary data: it is parsed, its JSON syntax included,
        and then nothing more — not a name, not a scope, and not a shape this
        scanner may refuse the file over.

        A container that IS read is still judged only where the file's reader
        would judge it (``_judges_shape``): at the file's root always, and at
        the ONE ``projects.<path>`` entry a reader selects — so a malformed
        container under an unrelated path declares no name, and no refusal
        either. Its names, when it holds any, are still recorded at that path
        for the caller to scope.

        With ``root_mapping`` set — the top-level object of a file whose server
        names are read from its own keys when it declares no ``mcpServers``
        container — an absent, empty or non-object ``mcpServers`` makes the
        object's own object-valued keys the registrations, at root scope, and
        nothing nested inside them is inventoried: the file is read as the flat
        name map it is, exactly as a host reading that shape does. No shape
        refusal applies, because a non-object container is precisely the case
        this reading covers.
        """

        scope = tuple(self._path[:-1]) if capture_names else ()
        declares_servers = self._declares_servers()
        per_key: dict[str, _Value] = {}
        char = self._nonspace()
        if char == "}":
            return _Value(True, empty_object=True)
        while char:
            if char != '"':
                raise ValueError("invalid JSON object key")
            key = self._string(retain=True)
            if self._nonspace() != ":":
                raise ValueError("invalid JSON object")
            first = self._nonspace()
            self._path.append(key)
            try:
                value = self._value(
                    first,
                    capture_names=(key == "mcpServers" and declares_servers),
                )
            finally:
                self._path.pop()
            per_key[key] = value
            char = self._nonspace()
            if char == "}":
                break
            if char != ",":
                raise ValueError("invalid JSON object")
            char = self._nonspace()
        else:
            raise ValueError("unterminated JSON object")
        # A field spelled ``mcpServers`` in a definition is data: it must not be
        # judged as a container here either, or the file's own loadable bytes
        # would be refused over a value the host never reads as a registry.
        container = per_key.get("mcpServers") if declares_servers else None
        # Only the SURVIVING value is judged — ``per_key`` holds one entry per
        # key — so an earlier ``null`` container that a later duplicate key
        # replaced is not a malformed file. Whether it IS one is decided here,
        # from the surviving value, and held rather than raised: this object may
        # itself be superseded by a later duplicate of the key that holds it, so
        # the refusal is only real once the host is known to load this object.
        own_refusal = None
        if (not root_mapping and container is not None
                and not container.object_shaped and self._judges_shape()):
            own_refusal = "mcpServers must be a JSON object"
        # A refusal decided deeper down rides out with the value that carries
        # it: superseded, it disappears with that value; surviving, the top
        # level refuses the file for it.
        nested_refusal = next(
            (value.refusal for value in per_key.values() if value.refusal), None
        )
        if root_mapping and (
            container is None or not container.object_shaped or container.empty_object
        ):
            return _Value(
                True,
                {key: {()} for key, value in per_key.items() if value.object_shaped},
                refusal=nested_refusal,
            )
        found: _Names = {}
        for key, value in per_key.items():
            _merge(found, value.found)
            if capture_names and value.object_shaped:
                found.setdefault(key, set()).add(scope)
        return _Value(
            True, found, empty_object=not per_key,
            refusal=own_refusal or nested_refusal,
        )

    def _require_end(self) -> None:
        """Refuse anything but whitespace after the one top-level JSON value.

        This scanner reads NAMES out of a registration file, so it only ever
        parses the first value and would otherwise ignore whatever follows it:
        a file whose bytes are ``{"mcpServers": {...}} <junk>`` — invalid JSON
        that the host itself refuses to load — would still inventory names the
        child never registers, and a name inventory that is not the registry
        the host loads is exactly what the callers of this module must not act
        on. Same fail-closed basis as an unterminated value.
        """
        if self._nonspace():
            raise ValueError("trailing data after the JSON value")

    def _parse_root(self, *, root_mapping: bool = False) -> _Names:
        """Parse the single top-level object every registration file is.

        A host registration file is a JSON object: a top-level array or any
        other value is a file the host refuses to load, and a name taken from
        it describes a registration that is not there — the file could carry a
        nested ``mcpServers`` object whose names would otherwise be read as
        root scope. Same fail-closed basis as trailing data.
        """
        if self._nonspace() != "{":
            raise ValueError("registration file must be a JSON object")
        value = self._object(root_mapping=root_mapping)
        # Every enclosing key has now resolved to its last value, so what
        # ``value`` carries is the shape of the object the host itself loads,
        # and nothing a superseded ancestor held can refuse the file.
        if value.refusal is not None:
            raise ValueError(value.refusal)
        self._require_end()
        return value.found

    def parse(self, *, root_mapping: bool = False) -> set[str]:
        return set(self._parse_root(root_mapping=root_mapping))

    def parse_scopes(self, *, root_mapping: bool = False) -> _Names:
        return self._parse_root(root_mapping=root_mapping)


def _open_registration(path: Path) -> TextIO:
    """Open a registration file as strict UTF-8.

    A byte that is not UTF-8 makes the file one the host cannot read, so
    ``errors="replace"`` — which turned such a byte into U+FFFD and scanned on
    — could report a name from a file the host refuses outright, whether or not
    the undecodable byte was anywhere near a name. The decode error is a
    ``ValueError``, which every caller already treats as unparsable.
    """

    return path.open("r", encoding="utf-8")


def json_mcp_names(
    path: Path,
    *,
    root_mapping_fallback: bool = False,
    project_entries: bool = True,
    selected_project: str | None = None,
) -> set[str]:
    """Return only object keys directly under an ``mcpServers`` container.

    A container is the file's root key or — for a file whose reader has that
    position, ``project_entries`` — a ``projects.<path>`` entry's key, so a
    field spelled ``mcpServers`` inside a server definition contributes no name.

    ``root_mapping_fallback`` reads a file whose own top-level keys are the
    registrations when it declares no effective ``mcpServers`` object, and
    ``selected_project`` names the one project entry whose container shape may
    refuse the file, exactly as :func:`json_mcp_name_scopes` documents.
    """

    with _open_registration(path) as stream:
        return _JsonNames(
            stream,
            project_entries=project_entries,
            selected_project=selected_project,
        ).parse(root_mapping=root_mapping_fallback)


def json_mcp_name_scopes(
    path: Path,
    *,
    root_mapping_fallback: bool = False,
    project_entries: bool = True,
    selected_project: str | None = None,
) -> dict[str, set[tuple[str, ...]]]:
    """Map each registered name to the key paths of the objects declaring it.

    Only object keys are retained, never values. A root-level declaration has
    the scope ``()``; a per-project declaration has
    ``("projects", "<project path>")`` — in a file whose reader has that
    position. Those two containers are the only ones read, so a field spelled
    ``mcpServers`` inside a server definition — or in an array, or under any
    other key — declares nothing here.

    ``root_mapping_fallback`` reads a file whose own top-level keys are the
    registrations when it declares no effective ``mcpServers`` object — the
    shape Claude's worktree ``.mcp.json`` uses (each such name is reported at
    root scope ``()``, and nothing nested under those keys is inventoried).
    Without it only an ``mcpServers`` container is a registration.

    ``project_entries`` says whether THIS file's reader reads a
    ``projects.<path>`` entry's ``mcpServers`` at all — True for the Claude user
    ``.claude.json``, False for the lane worktree ``.mcp.json`` and for the
    Devin registration files, whose readers are root-only. It is a fact about
    the reader, so this scanner takes it from the caller rather than inferring
    one from a file name: with it False a ``projects`` key is ordinary data —
    nothing inside it is a name, and a non-object ``mcpServers`` spelling inside
    it is not a refusal over a file the host loads whole.

    ``selected_project`` says WHICH of those entries this reader reads — the
    directory the host was launched in, which for a lane is its worktree. A
    reader reads one entry and no other, so with a directory named the shape
    rule covers that entry alone: a non-object ``mcpServers`` under an
    unrelated path declares no name and refuses nothing, because it is not a
    registry the child opens. Names such an entry DOES declare are still
    reported at ``("projects", "<that path>")`` for the caller to scope, which
    is the rule this scanner has always applied. ``None`` — the default, for a
    caller with no launch directory to name — selects no entry, so every
    project entry keeps the strict judgement.
    """

    with _open_registration(path) as stream:
        return _JsonNames(
            stream,
            project_entries=project_entries,
            selected_project=selected_project,
        ).parse_scopes(root_mapping=root_mapping_fallback)


def toml_mcp_names(path: Path) -> set[str]:
    """Return MCP table names while discarding every non-header byte.

    Read as strict UTF-8 for the same reason the JSON scanner is: an
    undecodable byte in a file the host cannot load must not yield a name.
    """

    names: set[str] = set()
    with _open_registration(path) as stream:
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
