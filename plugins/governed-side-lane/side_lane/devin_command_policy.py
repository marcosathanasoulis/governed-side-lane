"""Command-policy helpers used by the native Devin pre-tool hook."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import sys
from typing import NamedTuple, Sequence


EXEC_TOOL_NAME = "exec"
# Devin's documented file-mutating tools: the permissions reference names the
# "file edits via the `edit`/`write` tools", and str_replace is the
# str_replace-style edit variant some CLI builds expose. All three take the
# target path as their primary tool-input key.
FILE_MUTATING_TOOL_NAMES = ("write", "edit", "str_replace")
COVERED_TOOL_NAMES = (EXEC_TOOL_NAME, *FILE_MUTATING_TOOL_NAMES)
WRITE_TARGET_KEYS = ("file_path", "path", "file")

# Minimal redirection-operator set used by the per-component tokenizer.
_OUTPUT_REDIRECT_OPS = frozenset({">", ">>", ">|", "&>", "&>>"})
_INPUT_REDIRECT_OPS = frozenset({"<", "<<<", "<>"})
# ``<<``/``<<-`` here-documents are data redirection: the body is consumed as
# literal lines by the lexer rather than evaluated as commands, and only a
# quoted delimiter is accepted so the body cannot undergo shell expansion.
_HEREDOC_OPS = frozenset({"<<", "<<-"})
_DUP_REDIRECT_OPS = frozenset({">&", "<&"})

# A leading ``NAME=value`` word is an environment-assignment prefix, not the
# command itself; the name must be an unquoted POSIX identifier.
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_FETCH_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

# Token categories produced by the bounded lexer below. ``separator`` is a
# statement terminator (``;``), ``newline`` a line break, ``connector`` an
# operator that joins commands (``&&``, ``||``, ``|``, ``|&``), and
# ``background`` a bare ``&``, which is rejected outright.
_WORD_TOKEN = "word"
_REDIRECT_TOKEN = "redirect"
_SEPARATOR_TOKEN = "separator"
_NEWLINE_TOKEN = "newline"
_CONNECTOR_TOKEN = "connector"
_BACKGROUND_TOKEN = "background"

# Longest-first operator table; ``<``/``>`` also start the process-substitution
# and grouping forms that are rejected before this table is consulted.
_OPERATORS = (
    ("&>>", _REDIRECT_TOKEN),
    ("&>", _REDIRECT_TOKEN),
    ("&&", _CONNECTOR_TOKEN),
    ("&", _BACKGROUND_TOKEN),
    ("||", _CONNECTOR_TOKEN),
    ("|&", _CONNECTOR_TOKEN),
    ("|", _CONNECTOR_TOKEN),
    ("<<<", _REDIRECT_TOKEN),
    ("<<-", _REDIRECT_TOKEN),
    ("<<", _REDIRECT_TOKEN),
    ("<>", _REDIRECT_TOKEN),
    ("<&", _REDIRECT_TOKEN),
    ("<", _REDIRECT_TOKEN),
    (">>", _REDIRECT_TOKEN),
    (">|", _REDIRECT_TOKEN),
    (">&", _REDIRECT_TOKEN),
    (">", _REDIRECT_TOKEN),
    (";", _SEPARATOR_TOKEN),
)

# Characters that end a word only while unquoted.
_WORD_BREAK_CHARS = frozenset(" \t\n\r;&|<>()")

# A file-descriptor prefix is a single POSIX digit written against the operator.
_FD_DIGITS = frozenset("0123456789")

# A redirection target containing one of these would be rewritten by the shell
# (pathname expansion, brace expansion) after this check runs, so one that is
# written unquoted is refused rather than resolved.  Quoting or escaping the
# character keeps it literal, and the resolved path is still contained to the
# worktree either way.
_EXPANSION_CHARS = frozenset("*?[]{}")


class _Token(NamedTuple):
    """One lexed shell token together with the provenance the validators need.

    ``kind`` is one of the ``_*_TOKEN`` categories, ``text`` the word value with
    quoting and escapes removed (or the operator text).  ``quoted`` records that
    part of a word came from a quoted or escaped region, so a target that merely
    looks like an operator or a file-descriptor prefix can be told apart from a
    real one.  ``unquoted_expansion`` records that an expansion character was
    written unquoted *anywhere* in the word -- ``"out"*`` still globs, so this
    cannot be inferred from ``quoted``.  ``leading_space`` records whether
    unquoted whitespace preceded the token, which is what distinguishes the
    file-descriptor prefix of ``2>`` from the ``2`` argument of ``2 >``.
    """

    kind: str
    text: str
    quoted: bool = False
    leading_space: bool = True
    unquoted_expansion: bool = False


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

    The lexer above keeps ``$HOME``, ``` `pwd` ```, ``$(pwd)`` and a leading
    ``~`` as literal characters, so a target built from one of these resolves
    as an in-worktree-looking path here while the real shell (or a provider
    that re-parses the string) expands it to something else, e.g. the user's
    home directory. Any such token must be rejected before path resolution
    rather than trusted.
    """

    if not token:
        return False
    if token[0] == "~":
        return True
    return "$" in token or "`" in token


def _path_candidate(target: str, cwd: Path) -> Path | None:
    """Anchor a spelled path at ``cwd`` without resolving it.

    A token containing shell-expansion syntax (``$HOME``, ``` `pwd` ``,
    ``$(pwd)``, a leading ``~``) has no candidate: it must fail closed since
    a real shell would expand it after this check.
    """

    if _is_shell_expandable(target):
        return None
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate


def _inside_lane_worktree(target: str, resolved_worktree: Path | None,
                          cwd: Path | None = None) -> bool:
    """Resolve a tool target and report whether it lands inside the worktree.

    Relative paths are anchored at ``cwd`` -- the command's effective working
    directory, which defaults to the lane worktree root. ``resolve()`` follows
    symlinks, so a link planted inside the worktree that points elsewhere
    resolves to its destination and fails.
    """

    if resolved_worktree is None:
        return False
    candidate = _path_candidate(target, resolved_worktree if cwd is None else cwd)
    if candidate is None:
        return False
    resolved = candidate.resolve()
    return resolved == resolved_worktree or resolved.is_relative_to(resolved_worktree)


def _contained_under_all_cwds(target: str, resolved_worktree: Path | None,
                              cwds: Sequence[Path]) -> bool:
    """Containment when the runtime cwd is one of several possibilities.

    A ``cd`` earlier in a compound command may have moved the shell, and a
    ``;``/newline successor still runs when that ``cd`` fails, so the effective
    cwd is a set. A relative target must stay contained under every candidate.
    """

    return (resolved_worktree is not None and bool(cwds)
            and all(_inside_lane_worktree(target, resolved_worktree, cwd)
                    for cwd in cwds))


# A lane-contained symlink may point at the host Python interpreter the venv
# was built from (``python``, ``python3``, ``python3.11``). Nothing else.
_VENV_INTERPRETER_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?")


def _trusted_interpreter_paths() -> frozenset[Path]:
    """Real paths of host Python interpreters a venv launcher may resolve to.

    The set is derived only from this hook process's own runtime: its
    interpreter (``sys.executable``/``sys._base_executable``), its prefix
    ``bin`` directories, the directories on the inherited ``PATH``, and uv's
    documented interpreter root. User-managed Homebrew and uv interpreters are
    user-writable, so writability is not the trust signal -- provenance is: a
    resolved interpreter must be the realpath of a ``python*`` executable
    found in one of these host-configured locations. A command's ``NAME=value``
    prefixes are evaluated per component and never mutate this process's
    environment, so a model-controlled ``PATH=`` assignment cannot expand the
    set, and a task-written script planted anywhere else never joins it.
    """

    trusted: set[Path] = set()

    def add_file(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved.is_file() and os.access(resolved, os.X_OK):
            trusted.add(resolved)

    def add_dir(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if _VENV_INTERPRETER_NAME.fullmatch(entry.name):
                add_file(entry)

    for attribute in ("executable", "_base_executable"):
        value = getattr(sys, attribute, "")
        if isinstance(value, str) and value and Path(value).is_absolute():
            add_file(Path(value))
    for attribute in ("base_prefix", "exec_prefix", "prefix"):
        value = getattr(sys, attribute, "")
        if isinstance(value, str) and value:
            add_dir(Path(value) / "bin")
    for element in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(element) if element else None
        if candidate is not None and candidate.is_absolute():
            add_dir(candidate)
    data_home = os.environ.get("XDG_DATA_HOME")
    uv_roots = ([Path(data_home) / "uv" / "python"]
                if isinstance(data_home, str) and data_home else [])
    uv_roots.append(Path.home() / ".local" / "share" / "uv" / "python")
    for root in uv_roots:
        try:
            installs = list(root.iterdir())
        except OSError:
            continue
        for install in installs:
            add_dir(install / "bin")
    return frozenset(trusted)


def _venv_base_matches(resolved: Path, parent: Path) -> bool:
    """Bind a ``<venv>/bin/<name>`` launcher to its ``pyvenv.cfg`` record.

    ``pyvenv.cfg`` lives inside the lane, so a task can rewrite it; the file
    is not the trust root (trusted-set membership is). When the file exists
    the mapping is still enforced -- ``home`` must resolve so that
    ``home/<resolved name>`` is exactly the interpreter the launcher resolves
    to -- so a rewritten or planted mapping fails closed instead of lending a
    planted symlink the appearance of a real venv.
    """

    if parent.name != "bin":
        return True
    config = parent.parent / "pyvenv.cfg"
    if not config.is_file():
        return True
    home: str | None = None
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "home":
            home = value.strip()
            break
    if not home or _is_shell_expandable(home):
        return False
    base = Path(home)
    if not base.is_absolute():
        return False
    try:
        return (base / resolved.name).resolve() == resolved
    except OSError:
        return False


def _approved_external_interpreter(candidate: Path, resolved_worktree: Path) -> bool:
    """Narrow exception for a venv launcher symlink to a host interpreter.

    The escape is admitted only when the spelled path's *final component* is a
    symlink whose parent chain still resolves inside the lane (a symlinked
    directory escaping the lane is not covered), and the resolved target is an
    existing, executable file named like a Python interpreter whose realpath
    is a trusted host interpreter -- one found through the hook process's own
    runtime and inherited ``PATH``, not through anything a command supplied.
    A ``bin`` launcher carrying a ``pyvenv.cfg`` must also map to that same
    interpreter. Arbitrary task-written external ``python*`` scripts are not
    in the trusted set and stay refused.
    """

    if not candidate.is_symlink():
        return False
    parent = candidate.parent.resolve()
    if not (parent == resolved_worktree or parent.is_relative_to(resolved_worktree)):
        return False
    resolved = candidate.resolve()
    if (not resolved.is_file()
            or not _VENV_INTERPRETER_NAME.fullmatch(resolved.name)
            or not os.access(resolved, os.X_OK)
            or resolved not in _trusted_interpreter_paths()):
        return False
    return _venv_base_matches(resolved, parent)


def _executable_contained(spelled: str, resolved_worktree: Path | None,
                          cwds: Sequence[Path]) -> bool:
    """Containment check for an executable written as a path.

    Under every candidate cwd the path must either resolve inside the lane or
    qualify as the narrow venv-interpreter symlink exception above.
    """

    if resolved_worktree is None or not cwds:
        return False
    for cwd in cwds:
        candidate = _path_candidate(spelled, cwd)
        if candidate is None:
            return False
        resolved = candidate.resolve()
        if (resolved != resolved_worktree
                and not resolved.is_relative_to(resolved_worktree)
                and not _approved_external_interpreter(candidate, resolved_worktree)):
            return False
    return True


def _operator_at(command: str, index: int) -> tuple[str, str] | None:
    """Return the operator text and category starting at ``index``, if any."""

    for text, kind in _OPERATORS:
        if command.startswith(text, index):
            return text, kind
    return None


def _starts_command_substitution(command: str, index: int) -> bool:
    """Recognize an unescaped dollar opener across shell line continuations."""
    if command[index] != "$":
        return False
    following = index + 1
    while command.startswith("\\\n", following):
        following += 2
    return command.startswith("(", following)


def _lex_word(command: str, index: int,
              leading_space: bool) -> tuple[_Token | None, int, str | None]:
    """Lex one word; return its token, the end index and any failure reason.

    A single quote inside double quotes is an ordinary character, while ``$``
    and backticks stay active there.  Both facts, plus whether an expansion
    character was written unquoted, are recorded on the token so the validators
    can tell a literal operator from a real one.
    """

    value: list[str] = []
    quoted = False
    unquoted_expansion = False
    length = len(command)
    while index < length:
        character = command[index]
        if character == "\\":
            if index + 1 >= length:
                return None, index, "malformed shell quoting"
            next_char = command[index + 1]
            if next_char == "\n":
                # Backslash-newline is a line continuation: the shell removes
                # both characters before the word is parsed. A carriage return
                # is not a shell line continuation and must remain literal.
                index += 2
                continue
            value.append(command[index + 1])
            quoted = True
            index += 2
            continue
        if character == "'":
            quoted = True
            index += 1
            while index < length and command[index] != "'":
                value.append(command[index])
                index += 1
            if index >= length:
                return None, index, "malformed shell quoting"
            index += 1
            continue
        if character == '"':
            quoted = True
            index += 1
            while index < length and command[index] != '"':
                inner = command[index]
                if inner == "\\":
                    if index + 1 >= length:
                        return None, index, "malformed shell quoting"
                    following = command[index + 1]
                    if following in "\"\\`$":
                        value.append(following)
                        index += 2
                        continue
                    if following == "\n":
                        # Backslash-newline is a line continuation inside
                        # double quotes too.
                        index += 2
                        continue
                    value.append(inner)
                    index += 1
                    continue
                if inner == "`" or _starts_command_substitution(command, index):
                    return None, index, "command substitution"
                value.append(inner)
                index += 1
            if index >= length:
                return None, index, "malformed shell quoting"
            index += 1
            continue
        if character == "`" or _starts_command_substitution(command, index):
            return None, index, "command substitution"
        if character in _WORD_BREAK_CHARS:
            break
        if character in _EXPANSION_CHARS:
            unquoted_expansion = True
        value.append(character)
        index += 1
    token = _Token(_WORD_TOKEN, "".join(value), quoted, leading_space,
                   unquoted_expansion)
    return token, index, None


def _heredoc_specs(line_tokens: Sequence[_Token]) -> list[tuple[str, _Token]]:
    """Return ``(operator, delimiter token)`` pairs for one finished line.

    Each ``<<``/``<<-`` redirection needs the word token that follows it as the
    delimiter; a redirection at end of line has none and is left for the
    validator's ordinary malformed-redirection rejection.
    """

    specs: list[tuple[str, _Token]] = []
    for index, token in enumerate(line_tokens[:-1]):
        if token.kind == _REDIRECT_TOKEN and token.text in _HEREDOC_OPS:
            following = line_tokens[index + 1]
            if following.kind == _WORD_TOKEN:
                specs.append((token.text, following))
    return specs


def _consume_heredoc(command: str, index: int,
                     spec: tuple[str, _Token]) -> tuple[int, str | None]:
    """Consume one here-document body starting at ``index``; it is data.

    The body runs to a line that is exactly the delimiter (with leading tabs
    stripped for ``<<-``) and is never tokenized, so its contents cannot be
    reinterpreted as commands by this policy.  Only a quoted delimiter is
    accepted: an unquoted one would let the shell perform parameter and
    command expansion on the body after this check has run.
    """

    operator, delimiter = spec
    if not delimiter.quoted or not delimiter.text:
        return index, "unquoted here-document delimiter"
    while index < len(command):
        end = command.find("\n", index)
        if end == -1:
            line, index = command[index:], len(command)
        else:
            line, index = command[index:end], end + 1
        if line.endswith("\r"):
            line = line[:-1]
        if operator == "<<-":
            line = line.lstrip("\t")
        if line == delimiter.text:
            return index, None
    return index, "unterminated here-document"


def _lex_command(command: str) -> tuple[list[_Token] | None, str | None]:
    """Tokenize a shell command, keeping quoting and adjacency provenance."""

    tokens: list[_Token] = []
    index = 0
    length = len(command)
    leading_space = True
    line_tokens: list[_Token] = []

    while index < length:
        character = command[index]
        if character in " \t":
            index += 1
            leading_space = True
            continue
        if character in "\n\r":
            if command.startswith("\r\n", index):
                index += 2
            else:
                index += 1
            # A newline ends a command only where the shell can end one; after
            # an operator that still expects input it is a line continuation.
            if tokens and tokens[-1].kind in (_CONNECTOR_TOKEN, _REDIRECT_TOKEN):
                if _heredoc_specs(line_tokens):
                    return None, ("here-document across a line continuation "
                                  "is not supported")
                leading_space = True
                continue
            tokens.append(_Token(_NEWLINE_TOKEN, "\n", False, leading_space))
            leading_space = True
            for spec in _heredoc_specs(line_tokens):
                index, reason = _consume_heredoc(command, index, spec)
                if reason is not None:
                    return None, reason
            line_tokens = []
            continue
        if command.startswith("<(", index) or command.startswith(">(", index):
            return None, "process substitution"
        if character in "()":
            return None, "subshell or grouping syntax"
        operator = _operator_at(command, index)
        if operator is not None:
            text, kind = operator
            token = _Token(kind, text, False, leading_space)
            index += len(text)
        else:
            token, index, reason = _lex_word(command, index, leading_space)
            if reason is not None:
                return None, reason
        tokens.append(token)
        line_tokens.append(token)
        leading_space = False
    if _heredoc_specs(line_tokens):
        return None, "unterminated here-document"
    return tokens, None


def _split_command(command: str) -> tuple[list[tuple[list[_Token], str]] | None,
                                        str | None]:
    """Split a shell command into top-level components and detect unsafe syntax.

    The command is split into lines at real newlines, into statements at ``;``,
    and into pipelines and and-or lists at ``&&``, ``||`` and ``|``.  Each
    resulting component is returned as ``(tokens, link)`` where ``link`` is the
    connector through which it is reached: ``"start"`` for the first component
    of a statement (it always runs once the previous statement finishes),
    otherwise the connector text (``&&``, ``||``, ``|``, ``|&``). The caller
    authorises every component and uses the link to track the effective working
    directory.  A trailing ``;`` and blank lines are ordinary shell separators;
    an incomplete ``&&``/``||``/``|``, an empty statement (``a ; ; b``) and a
    whitespace-only command are rejected.

    Rejects command substitution, process substitution, background jobs,
    subshell/grouping syntax, and malformed or unterminated quoting.
    """

    tokens, reason = _lex_command(command)
    if reason is not None:
        return None, reason

    lines: list[list[_Token]] = [[]]
    for token in tokens:
        if token.kind == _BACKGROUND_TOKEN:
            return None, "background job"
        if token.kind == _NEWLINE_TOKEN:
            lines.append([])
        else:
            lines[-1].append(token)

    components: list[tuple[list[_Token], str]] = []
    for line in lines:
        # A blank line, including the one a trailing newline leaves behind,
        # holds no command and therefore nothing to authorise.
        if not line:
            continue
        statements: list[list[_Token]] = [[]]
        for token in line:
            if token.kind == _SEPARATOR_TOKEN:
                statements.append([])
            else:
                statements[-1].append(token)
        if statements and not statements[-1]:
            # A trailing ``;`` is an ordinary statement terminator.
            statements.pop()
        for statement in statements:
            if not statement:
                return None, "malformed shell syntax"
            component: list[_Token] = []
            link = "start"
            for token in statement:
                if token.kind == _CONNECTOR_TOKEN:
                    if not component:
                        return None, "malformed shell syntax"
                    components.append((component, link))
                    link = token.text
                    component = []
                    continue
                component.append(token)
            if not component:
                return None, "malformed shell syntax"
            components.append((component, link))
    if not components:
        return None, "malformed shell syntax"
    return components, None


def _validate_file_redirect(target: _Token, worktree_path: Path | None,
                            cwds: Sequence[Path]) -> str | None:
    """Verify an output-redirection target stays inside the lane worktree."""

    # The null device is an exact discard sink, not a writable filesystem
    # target. Keep this exception literal; arbitrary /dev paths remain outside
    # the lane and are rejected below.
    if target.text == "/dev/null":
        return None
    if worktree_path is None:
        return "redirection requires the lane worktree in the policy rules"
    if target.unquoted_expansion:
        # The shell would glob or brace-expand this target -- into a different,
        # possibly existing and possibly outside-the-worktree path -- after this
        # check has run.
        return ("unsupported shell expansion in a redirection target "
                "is not permitted")
    if not _contained_under_all_cwds(target.text, worktree_path, cwds):
        return ("redirect target outside the lane worktree is not permitted; "
                f"use {worktree_path}/.side-lane-scratch/ for scratch files")
    return None


def _validate_redirect(redirect_op: str, target: _Token,
                       worktree_path: Path | None,
                       cwds: Sequence[Path]) -> str | None:
    """Check that a redirection is safe and contained."""

    if redirect_op in _HEREDOC_OPS:
        # The lexer has already consumed the body as data; the delimiter must
        # be quoted so the shell cannot expand the body at run time.
        if not target.quoted or not target.text:
            return "unquoted here-document delimiter is not permitted"
        return None
    if redirect_op in _INPUT_REDIRECT_OPS:
        return "unsupported redirection is not permitted"
    if redirect_op in _DUP_REDIRECT_OPS:
        if target.text == "-" or target.text.isdigit():
            return None
        if redirect_op == ">&":
            # Bash treats >&word as stdout+stderr to file when word is not a fd.
            return _validate_file_redirect(target, worktree_path, cwds)
        return "unsupported redirection is not permitted"
    if redirect_op in _OUTPUT_REDIRECT_OPS:
        return _validate_file_redirect(target, worktree_path, cwds)
    return "unsupported redirection is not permitted"


def _strip_git_dash_c(tokens: list[str], worktree_path: Path | None,
                      cwds: Sequence[Path]) -> list[str]:
    """Drop a leading ``git -C <lane-worktree>`` token sequence per component.

    Providers naturally qualify git with the lane path
    (``git -C /lane log --oneline -5``), which must match the canonical
    ``Bash(git log *)`` grants. Only a ``-C`` target that resolves inside the
    lane worktree under every candidate cwd is stripped; any other ``-C``
    target leaves the prefix in place so it is evaluated as-is and fails to
    match unless literally granted.
    """

    if (worktree_path is None or len(tokens) < 4
            or tokens[0] != "git" or tokens[1] != "-C"):
        return tokens
    if not _contained_under_all_cwds(tokens[2], worktree_path, cwds):
        return tokens
    return [tokens[0], *tokens[3:]]


def _env_value_contained(value: str, worktree_path: Path | None,
                         cwds: Sequence[Path]) -> bool:
    """Report whether an environment-assignment value stays lane-contained.

    Every nonempty ``:``-separated element is a path the shell resolves at the
    command's effective cwd -- a bare ``pkg`` element is ``<cwd>/pkg`` -- so it
    must resolve inside the lane worktree under every candidate cwd. An empty
    element is the shell's "current directory" convention and needs no check.
    Elements written with shell-expansion syntax (``$HOME``, backticks, a
    leading ``~``) or with glob/brace characters fail closed since a real
    shell would expand them to a different path after this check.
    """

    for element in value.split(":"):
        if not element:
            continue
        if _is_shell_expandable(element):
            return False
        if any(character in _EXPANSION_CHARS for character in element):
            return False
        if not _contained_under_all_cwds(element, worktree_path, cwds):
            return False
    return True


def _validate_component(component: list[_Token], allowed: Sequence[str],
                        denied: Sequence[str], worktree_path: Path | None,
                        cwds: Sequence[Path]
                        ) -> tuple[dict[str, str] | None, set[Path] | None]:
    """Validate one top-level shell component, including its redirections.

    Returns ``(decision, cd_targets)``: ``decision`` is a block verdict or
    ``None`` when the component is authorized, and ``cd_targets`` carries the
    resolved per-cwd destinations when the component is an authorized ``cd``,
    so the caller can track the effective working directory.
    """

    # Leading ``NAME=value`` words are environment-assignment prefixes on the
    # command that follows, not the command itself. Only unquoted identifiers
    # qualify (``"FOO=x"`` is an ordinary word, and a quoted value is left to
    # fail the grant match rather than widened here); each assignment value is
    # constrained to the lane worktree or to literals with no path reference.
    while (component and component[0].kind == _WORD_TOKEN
           and not component[0].quoted
           and _ENV_ASSIGNMENT.match(component[0].text)):
        value = component[0].text.split("=", 1)[1]
        if not _env_value_contained(value, worktree_path, cwds):
            return ({"decision": "block",
                     "reason": "environment assignment value outside the lane "
                               "worktree is not permitted"}, None)
        component = component[1:]

    argv: list[str] = []
    index = 0
    token_count = len(component)
    while index < token_count:
        token = component[index]
        if token.kind == _REDIRECT_TOKEN:
            if index + 1 >= token_count or component[index + 1].kind != _WORD_TOKEN:
                return {"decision": "block", "reason": "malformed redirection"}, None
            reason = _validate_redirect(token.text, component[index + 1],
                                        worktree_path, cwds)
            if reason is not None:
                return {"decision": "block", "reason": reason}, None
            index += 2
            continue
        # An unquoted single digit written against the operator is a
        # file-descriptor prefix (``2>``), not an argument, so it is not part of
        # the command string.  ``2 >`` keeps the digit as an argument, and a
        # longer or quoted digit run stays an argument too, so it still reaches
        # the deny/allow checks below instead of being swallowed here.
        if (token.kind == _WORD_TOKEN and not token.quoted
                and token.text in _FD_DIGITS and index + 1 < token_count
                and component[index + 1].kind == _REDIRECT_TOKEN
                and not component[index + 1].leading_space):
            index += 1
            continue
        argv.append(token.text)
        index += 1

    argv = _strip_git_dash_c(argv, worktree_path, cwds)

    # Review lanes may refresh refs from the configured repository remote, but
    # must not turn `git fetch` into an arbitrary option/command launcher.
    # Keep the grant to the literal `origin` remote and plain ref names; flags
    # such as --upload-pack and --config remain outside the capability.
    if len(argv) >= 2 and argv[:2] == ["git", "fetch"]:
        if len(argv) < 3 or argv[2] != "origin" or any(
            not _SAFE_FETCH_REF.fullmatch(value) for value in argv[3:]
        ):
            return ({"decision": "block",
                     "reason": "git fetch is limited to plain refs from origin"}, None)

    # ``cd`` is a shell builtin, so no canonical ``Bash(...)`` grant can name
    # it. A bare ``cd <dir>`` component is authorized only when the target
    # resolves inside the lane worktree under every candidate cwd; anything
    # else falls through to the ordinary grant match, which blocks it.
    if argv and argv[0] == "cd" and len(argv) == 2 and worktree_path is not None:
        targets: set[Path] = set()
        for cwd in cwds:
            candidate = _path_candidate(argv[1], cwd)
            if candidate is None:
                targets = set()
                break
            resolved = candidate.resolve()
            if not (resolved == worktree_path
                    or resolved.is_relative_to(worktree_path)):
                targets = set()
                break
            targets.add(resolved)
        if targets:
            return None, targets

    spellings = [" ".join(argv)]
    if argv and "/" in argv[0]:
        # An executable written as a path (``.venv/bin/python``) is authorized
        # only when it resolves inside the lane worktree under every candidate
        # cwd -- or, narrowly, when it is a lane-contained symlink to a
        # system-installed Python interpreter, as a standard venv creates --
        # and is matched against the grants both as spelled
        # (``./node_modules/.bin/*`` is a path grant) and by basename so a
        # contained interpreter matches its canonical ``Bash(<name> *)`` rule.
        if not _executable_contained(argv[0], worktree_path, cwds):
            return ({"decision": "block",
                     "reason": "executable outside the lane worktree is not permitted"},
                    None)
        basename = Path(argv[0]).name
        if basename:
            spellings.append(" ".join([basename, *argv[1:]]))
    for spelling in spellings:
        matched = matching_rule(spelling, denied, anywhere=True)
        if matched is not None:
            return ({"decision": "block",
                     "reason": f"command denied by canonical rule: {matched}"}, None)
    for spelling in spellings:
        if matching_rule(spelling, allowed) is not None:
            return None, None
    return ({"decision": "block",
             "reason": "command is outside canonical capability grants"}, None)


def _evaluate_exec(payload: dict, allowed: Sequence[str], denied: Sequence[str],
                   worktree: str | None) -> dict[str, str] | None:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return {"decision": "block", "reason": "exec command missing from tool input"}
    components, reason = _split_command(command)
    if reason is not None:
        return {"decision": "block", "reason": f"{reason} is not permitted"}
    worktree_path = Path(worktree).resolve() if worktree else None
    # A caller-supplied ``workdir`` shifts the command's starting working
    # directory; it is authorized only inside the lane worktree. Relative
    # executable, redirect, ``git -C`` and environment-path targets are then
    # anchored at that directory rather than the lane root.
    workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
    cwds: set[Path] = set()
    if worktree_path is not None:
        cwds.add(worktree_path)
    if workdir is not None:
        candidate = (_path_candidate(workdir, worktree_path)
                     if isinstance(workdir, str) and workdir.strip()
                     and worktree_path is not None else None)
        if candidate is None:
            return {"decision": "block",
                    "reason": "exec workdir outside the lane worktree is not permitted"}
        resolved = candidate.resolve()
        if not (resolved == worktree_path
                or resolved.is_relative_to(worktree_path)):
            return {"decision": "block",
                    "reason": "exec workdir outside the lane worktree is not permitted"}
        cwds = {resolved}
    # The effective cwd is a set: a ``cd`` that cannot be proven to have run
    # leaves the previous cwd reachable. ``&&`` reaches the next component only
    # when the preceding ``cd`` succeeded (cwd is its target alone); ``||``
    # only when it failed (cwd unchanged); ``;``/newline unconditionally
    # (either is possible); ``|`` runs the ``cd`` in a pipeline subshell, which
    # never moves the parent cwd.
    previous_cd: set[Path] | None = None
    previous_entry: set[Path] = set(cwds)
    for index, (component, link) in enumerate(components):
        if previous_cd is None:
            entry = set(cwds)
        elif link == "&&":
            entry = set(previous_cd)
        elif link in ("||", "|", "|&"):
            entry = set(previous_entry)
        else:  # "start": unconditional continuation
            entry = cwds | previous_cd
        piped = (link in ("|", "|&")
                 or (index + 1 < len(components)
                     and components[index + 1][1] in ("|", "|&")))
        decision, cd_targets = _validate_component(
            component, allowed, denied, worktree_path, sorted(entry))
        if decision is not None:
            return decision
        previous_entry = entry
        if cd_targets is not None and not piped:
            previous_cd = cd_targets
            cwds |= cd_targets
        else:
            previous_cd = None
    return None


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
