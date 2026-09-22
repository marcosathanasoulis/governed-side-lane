"""Canonical repository and lane-governance loading.

Host-native instruction discovery differs across Codex and Claude Code.  This
module makes the effective lane contract independent of those differences by
rendering one checked-in Markdown source for every invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_PATH = PACKAGE_ROOT / "config" / "lane-governance.md"
MODELS_PATH = PACKAGE_ROOT / "config" / "models.json"
# Linkage validation is a completeness check on a repository-owned file, not a
# defence against adversarial wording: AGENTS.md is written by the repository's
# own maintainers. The negation list below catches common negative phrasings so
# an honest "not the source of truth" line is not mistaken for a linkage claim;
# it is not exhaustive and is not meant to be.
NEGATED_CLAIM = re.compile(
    # "is not currently/really/yet the source of truth": allow one qualifying
    # adverb between the negation and the claim, but never "only" ("not only
    # authoritative but required" is an affirmative claim).
    r"(?:\b(?:is|are|remains?|be|being|been)\s+(?:not|no longer)|\b(?:isn't|aren't|not))"
    r"(?:\s+(?!only\b)(?:\w+ly|yet|now|still|ever|even|anymore))?"
    r"\s+(?:the\s+|an?\s+)?(?:authoritative|source of truth)\b"
    # "never treat ... as authoritative", "must not treat ... as the source of truth",
    # "should not consider ... authoritative", "do not read ... as authoritative"
    r"|\b(?:never|cannot|can't|won't|don't|doesn't|didn't|shouldn't|couldn't|wouldn't|mustn't|shan't"
    r"|under no circumstances|in no (?:case|way)|by no means|at no (?:time|point)|no one should"
    r"|(?:must|should|shall|do|does|did|will|would|can|could|may)\s+not)\b"
    r"(?:\s+\S+){0,5}?\s+(?:as\s+)?(?:the\s+)?(?:authoritative|source of truth)\b",
    re.I,
)
REQUIRED_SECTIONS = (
    "Common",
    "Review mode",
    "Execute mode",
    "Report deliverable",
    "Execute tool allowlist",
)
ALLOWLIST_SECTION = "Execute tool allowlist"
REPORT_SECTION = "Report deliverable"
REPORT_DENIED_HEADING = "report-deliverable (denied)"
ALWAYS = "always"
DENIED_SUFFIX = " (denied)"
# The two execute-mode bullets that grant explicit write authority: the
# commit/push grant over the assigned lane branch, and the task-scoped
# workflow/messaging write exemption. A report-deliverable lane's deliverable is
# its report artifact, so both bullets are dropped from its rendered body; each
# sentence is compared after whitespace normalization, as one complete Markdown
# bullet including its continuation lines, and its absence or duplication fails
# closed rather than shipping a lane whose contract was silently changed.
EXECUTE_GIT_GRANT = "You may inspect, edit, test, commit, and push only the assigned lane branch."
EXECUTE_WORKFLOW_GRANT = (
    "A workflow or messaging write is allowed only when the approved task names "
    "that exact update and recipient or object. Make only that update through the "
    "selected worker host's connector and report exactly what changed."
)
# Each grant with the operator-facing name of the rule it states, so a
# fail-closed report names which one moved.
EXECUTE_WRITE_GRANTS = (
    (EXECUTE_GIT_GRANT, "commit grant"),
    (EXECUTE_WORKFLOW_GRANT, "workflow/messaging grant"),
)
# Capabilities that carry explicit write authority, and so can never be granted
# to a report-deliverable lane: its deliverable is its report artifact, so it
# makes no commit, no push, and no workflow or messaging write. Only explicit
# write grants are named — `workspace-write` is the report artifact's own grant
# and stays, and no read capability is ever listed here.
#
# The names themselves are NOT owned by this module. The canonical document is
# the single source for lane exceptions, so `lane-governance.md` declares them
# once, on one machine-readable line in its `Report deliverable` section, and
# every reader parses that line. A missing, malformed, or duplicated
# declaration fails closed rather than silently narrowing the refusal list to
# nothing — or refusing a capability the document never named.
REPORT_FORBIDDEN_PREFIX = "Report forbidden write capabilities:"
CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9-]*")


class GovernanceError(ValueError):
    """Canonical or repository governance is absent or ambiguous."""


def _sections(path: Path = GOVERNANCE_PATH) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GovernanceError(f"cannot load canonical lane governance: {exc}") from exc
    parts = re.split(r"^## ([^\n]+)\n", text, flags=re.M)
    sections = {
        parts[index].strip(): parts[index + 1].strip()
        for index in range(1, len(parts) - 1, 2)
    }
    missing = [name for name in REQUIRED_SECTIONS if not sections.get(name)]
    if missing:
        raise GovernanceError(
            "canonical lane governance is missing sections: " + ", ".join(missing)
        )
    return sections


def _normalized(text: str) -> str:
    """Collapse whitespace, so a reflowed bullet still compares equal."""

    return " ".join(text.split())


def _bullets(body: str) -> list[tuple[list[str], list[int]]]:
    """Top-level Markdown bullets of ``body``, with the lines each occupies.

    A bullet is a line starting with ``- `` plus every immediately following
    indented line; a blank line, a new bullet, or an unindented line ends it.
    Only the top level is parsed, so a nested list is part of its parent bullet
    — which is what dropping the parent bullet must drop.
    """

    bullets: list[tuple[list[str], list[int]]] = []
    active = False
    for index, raw in enumerate(body.splitlines()):
        if raw.startswith("- "):
            bullets.append(([raw], [index]))
            active = True
        elif raw.strip() and raw[:1].isspace() and active:
            bullets[-1][0].append(raw)
            bullets[-1][1].append(index)
        else:
            active = False
    return bullets


def _report_execute_body(execute_body: str) -> str:
    """Execute-mode rules with the explicit write grants removed.

    Only the grant bullets are dropped; every other execute rule, including the
    worktree and lane-branch boundary, still applies to a report lane. Each
    grant is matched as one complete bullet — continuation lines included, after
    whitespace normalization — and must be found exactly once: a missing grant
    would leave a rule the override exists to remove in force, and a duplicated
    one would make "which bullet is the grant" ambiguous, so both fail closed.
    """

    lines = execute_body.splitlines()
    bullets = [
        (indexes, _normalized(" ".join(parts))) for parts, indexes in _bullets(execute_body)
    ]
    dropped: set[int] = set()
    for grant, label in EXECUTE_WRITE_GRANTS:
        expected = _normalized(f"- {grant}")
        matches = [indexes for indexes, text in bullets if text == expected]
        if len(matches) != 1:
            raise GovernanceError(
                f"canonical lane governance must state the execute {label} exactly "
                "once, as one Markdown bullet, for the report-deliverable override "
                f"to drop it: found {len(matches)} matching bullet(s) for {grant!r}"
            )
        dropped.update(matches[0])
    return "\n".join(line for index, line in enumerate(lines) if index not in dropped)


def _declared_report_capabilities(section_body: str) -> tuple[str, ...]:
    """Parse the canonical declaration of the report contract's refused writes.

    The declaration is one machine-readable line in the `Report deliverable`
    section, naming each capability as one backticked identifier:

        Report forbidden write capabilities: `git-push`, `workflow-write`

    Exactly one such line must exist, it must name at least one capability, and
    each entry must be exactly one backticked identifier separated by commas —
    so a reworded or annotated line fails closed rather than being read
    approximately. Identifiers are validated against the same capability-name
    grammar the tool allowlist uses, and a name declared twice is an error.
    """

    declared = [
        line.strip()
        for line in section_body.splitlines()
        if line.strip().startswith(REPORT_FORBIDDEN_PREFIX)
    ]
    if len(declared) != 1:
        raise GovernanceError(
            "canonical lane governance must declare the report forbidden write "
            f"capabilities on exactly one line starting with "
            f"{REPORT_FORBIDDEN_PREFIX!r}: found {len(declared)}"
        )
    tail = declared[0][len(REPORT_FORBIDDEN_PREFIX):].strip()
    if not tail:
        raise GovernanceError(
            "canonical lane governance declares no report forbidden write "
            f"capability after {REPORT_FORBIDDEN_PREFIX!r}"
        )
    names: list[str] = []
    for part in tail.split(","):
        entry = part.strip()
        match = re.fullmatch(r"`([^`]+)`", entry)
        if not match:
            raise GovernanceError(
                "canonical lane governance declares report forbidden write "
                f"capabilities in a malformed entry: {entry!r}"
            )
        name = match.group(1)
        if not CAPABILITY_NAME.fullmatch(name):
            raise GovernanceError(
                "invalid capability name in the report forbidden write "
                f"declaration: {name!r}"
            )
        if name in names:
            raise GovernanceError(
                "duplicate capability name in the report forbidden write "
                f"declaration: {name!r}"
            )
        names.append(name)
    return tuple(names)


def report_forbidden_write_capabilities(
    path: Path = GOVERNANCE_PATH,
) -> tuple[str, ...]:
    """The refused write capabilities, as the canonical document declares them."""

    return _declared_report_capabilities(_sections(path)[REPORT_SECTION])


def report_write_capability_conflicts(
    capabilities: Iterable[str], path: Path = GOVERNANCE_PATH
) -> tuple[str, ...]:
    """The explicit write capabilities of ``capabilities``, in canonical order.

    A report-deliverable lane renders the report contract instead of the
    execute commit grant, so a capability whose only effect is that kind of
    explicit write authority — a push, or a workflow or messaging write — is
    refused rather than handed to a worker for a run that will never exercise
    it. Empty for every other grant, including ``workspace-write`` (the report
    artifact's own write) and every read capability.

    The refused names come from the canonical document's own declaration, so a
    report lane's refusals stay derived from the single source for lane
    exceptions rather than from a list this module maintains alongside it.
    """

    granted = set(capabilities)
    return tuple(
        name for name in report_forbidden_write_capabilities(path) if name in granted
    )


def lane_system_prompt(
    mode: str,
    repository: str | Path,
    *,
    report_deliverable: bool = False,
    path: Path = GOVERNANCE_PATH,
) -> str:
    if mode not in {"review", "execute"}:
        raise GovernanceError(f"unsupported governance mode: {mode}")
    if report_deliverable and mode != "execute":
        raise GovernanceError("the report deliverable contract is execute mode only")
    repo = Path(repository).expanduser().resolve()
    sections = _sections(path)
    active = "Review mode" if mode == "review" else "Execute mode"
    body = sections[active]
    if report_deliverable:
        body = _report_execute_body(body)
    rendered = (
        "# Injected canonical side-lane governance\n\n"
        "## Common\n\n"
        + sections["Common"]
        + f"\n\n## Active mode: {active}\n\n"
        + body
    )
    if report_deliverable:
        rendered += "\n\n## Report deliverable\n\n" + sections[REPORT_SECTION]
    return rendered.replace("{{MAIN_CHECKOUT}}", str(repo))


@dataclass(frozen=True)
class ToolPolicy:
    """Execute-lane tool rules parsed from the canonical governance document."""

    always: tuple[str, ...]
    allowed: dict[str, tuple[str, ...]]  # capability -> rules it unlocks
    denied: dict[str, tuple[str, ...]]   # capability -> rules it must deny
    # Not a capability: rules a report-deliverable lane must not run.  Excluded
    # from ``capabilities`` on purpose, since no lane can be granted them.
    report_denied: tuple[str, ...] = ()

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.allowed) | frozenset(self.denied)


def _rules(block: str, heading: str) -> tuple[str, ...]:
    """Rule bullets of one subsection; any other non-blank line is malformed."""

    rules = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^- `([^`]+)`$", line)
        if not match:
            raise GovernanceError(
                f"tool allowlist subsection {heading!r} has a malformed line: {line!r}"
            )
        rules.append(match.group(1))
    return tuple(rules)


def tool_policy(path: Path = GOVERNANCE_PATH) -> ToolPolicy:
    """Parse the ``Execute tool allowlist`` section.

    Subsection headings name comma-separated capabilities (or ``always``); a
    heading ending in ``(denied)`` lists rules that must accompany the allow
    rules for that capability. The reserved ``report-deliverable (denied)``
    heading names no capability: it lists the git-write rules a
    report-deliverable lane must not run.
    """

    section = _sections(path)[ALLOWLIST_SECTION]
    parts = re.split(r"^### ([^\n]+)\n", section, flags=re.M)
    always: tuple[str, ...] = ()
    allowed: dict[str, list[str]] = {}
    denied: dict[str, list[str]] = {}
    report_denied: tuple[str, ...] = ()
    for index in range(1, len(parts) - 1, 2):
        heading, block = parts[index].strip(), parts[index + 1]
        rules = _rules(block, heading)
        if not rules:
            raise GovernanceError(f"tool allowlist subsection has no rules: {heading}")
        if heading == ALWAYS:
            if always:
                raise GovernanceError("tool allowlist declares `always` more than once")
            always = rules
            continue
        # Reserved heading, checked before the generic `(denied)` suffix so its
        # rules land in their own bucket instead of becoming a capability.
        if heading == REPORT_DENIED_HEADING:
            if report_denied:
                raise GovernanceError(
                    f"tool allowlist declares `{REPORT_DENIED_HEADING}` more than once"
                )
            report_denied = rules
            continue
        target = denied if heading.endswith(DENIED_SUFFIX) else allowed
        names = [name.strip() for name in heading.removesuffix(DENIED_SUFFIX).split(",")]
        for name in names:
            if not CAPABILITY_NAME.fullmatch(name):
                raise GovernanceError(f"invalid capability name in tool allowlist: {name!r}")
            target.setdefault(name, []).extend(rules)
    if not always:
        raise GovernanceError("tool allowlist is missing the `always` subsection")
    if not report_denied:
        raise GovernanceError(
            f"tool allowlist is missing the `{REPORT_DENIED_HEADING}` subsection"
        )
    return ToolPolicy(
        always,
        {k: tuple(v) for k, v in allowed.items()},
        {k: tuple(v) for k, v in denied.items()},
        report_denied,
    )


def known_capabilities(path: Path = MODELS_PATH) -> frozenset[str]:
    """Capability names declared by the runtime allowlist (``config/models.json``)."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GovernanceError(f"cannot load capability allowlist: {exc}") from exc
    names = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise GovernanceError("capability allowlist is invalid")
    return frozenset(names)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def validate_repository(
    repo_argument: str | Path, *, runner: Runner = subprocess.run
) -> Path:
    repo = Path(repo_argument).expanduser().resolve()
    if not repo.is_dir():
        raise GovernanceError(f"target is not a Git repository root: {repo}")
    result = runner(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise GovernanceError(f"target is not a Git repository root: {repo}")
    try:
        top = Path(result.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        raise GovernanceError(f"target is not a Git repository root: {repo}") from None
    if top != repo:
        raise GovernanceError(f"target is not a Git repository root: {repo}")
    agents, claude = repo / "AGENTS.md", repo / "CLAUDE.md"
    if not agents.is_file() or not claude.is_file():
        raise GovernanceError("governance requires root AGENTS.md and CLAUDE.md files")
    if agents.is_symlink() or agents.resolve().parent != repo:
        raise GovernanceError("root AGENTS.md must be a regular repository file")
    text = agents.read_text(encoding="utf-8")
    authoritative_links: list[Path] = []
    for line in text.splitlines():
        # Two accepted wordings: an explicit requirement that names the file
        # authoritative ("You must read [CLAUDE.md](./CLAUDE.md); it is the
        # authoritative ..."), or the plain declaration "[CLAUDE.md](./CLAUDE.md)
        # is the source of truth". "Authoritative" alone is too weak without a
        # requirement word; "source of truth" already states the obligation.
        requires = re.search(r"\b(?:must|required)\b", line, re.I)
        authoritative = re.search(r"\bauthoritative\b", line, re.I)
        source_of_truth = re.search(r"\bsource of truth\b", line, re.I)
        if not (source_of_truth or (requires and authoritative)):
            continue
        # A negated declaration is not a linkage claim. Only a negation bound
        # to the claim itself counts ("is not the source of truth", "isn't
        # authoritative", "never ... as authoritative"); unrelated negations
        # ("not optional", "not only authoritative", "..., not this file") do
        # not disqualify an otherwise affirmative line.
        if NEGATED_CLAIM.search(line):
            continue
        for destination in re.findall(r"\[[^\]]*\]\(([^)]+)\)", line):
            target = destination.strip().strip("<>").split("#", 1)[0]
            authoritative_links.append((repo / target).resolve())
    if not authoritative_links or set(authoritative_links) != {claude.resolve()}:
        raise GovernanceError(
            "AGENTS.md must link unambiguously to root CLAUDE.md: include one line "
            "with a Markdown link to it that either says it is the source of truth, "
            'e.g. "[`CLAUDE.md`](./CLAUDE.md) is the source of truth for this repo", '
            'or requires it and names it authoritative, e.g. "You **must** read '
            '[CLAUDE.md](./CLAUDE.md); it is the authoritative source of truth." '
            "Links on such lines may point only at root CLAUDE.md."
        )
    if claude.is_symlink() or claude.resolve().parent != repo:
        raise GovernanceError(
            "root CLAUDE.md must be a regular repository file"
        )
    return repo
