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
from typing import Callable, Iterable, Sequence


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
    "Publication refusal",
    "Execute tool allowlist",
    "Existing owner workspace",
)
ALLOWLIST_SECTION = "Execute tool allowlist"
REPORT_SECTION = "Report deliverable"
REPORT_DENIED_HEADING = "report-deliverable (denied)"
PUBLICATION_SECTION = "Publication refusal"
EXISTING_WORKSPACE_SECTION = "Existing owner workspace"
# The two reserved headings of the tool allowlist. Like the report bucket they
# name no capability: no grant unlocks either, and each exists so a surface a
# per-run contract selects is declared in the canonical document rather than
# hard-coded in an adapter — the commands a lane carrying the task
# no-external-publication guard must not run, and the tool surface of the local
# developer execute profile.
PUBLICATION_DENIED_HEADING = "no-external-publication (denied)"
# The third reserved heading, on the same terms: the direct git-write commands
# an existing-owner-workspace lane must not run. Its verbs are the verbs the
# ``## Existing owner workspace`` section forbids, so the prohibition a worker
# is handed and the denials each host renders come from one list — see
# :func:`existing_workspace_denied_verbs`, which reads that section's own
# enumeration back rather than keeping a second copy here.
EXISTING_WORKSPACE_DENIED_HEADING = "existing-workspace (denied)"
LOCAL_DEVELOPER_HEADING = "local-developer (granted)"
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
# The execute bullet that says where an execute lane runs. An existing owner
# workspace is not a dedicated worktree and has no assigned lane branch, so the
# bullet is false there and is dropped rather than left in force beside the
# section that replaces it. Leaving both would ship a contract that contradicts
# itself, which is the one thing the fail-closed bullet matching exists to
# prevent.
EXECUTE_WORKTREE_BOUNDARY = (
    "Work only in the dedicated side-lane worktree and assigned lane branch."
)
# The two execute bullets an existing owner workspace supersedes: the boundary
# above, and the commit/push grant over a branch that does not exist here. The
# workflow/messaging grant is kept — separately authorized, and orthogonal.
EXISTING_WORKSPACE_SUPERSEDED = (
    (EXECUTE_WORKTREE_BOUNDARY, "worktree boundary"),
    (EXECUTE_GIT_GRANT, "commit grant"),
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
# The same machine-readable mechanism for the publication guard, declared on
# one line in the `Publication refusal` section. Kept a separate declaration
# rather than a subset of the report line: a report lane and a
# publication-refusing lane refuse those capabilities for two different
# reasons, and one list would silently widen or narrow the other.
PUBLICATION_REFUSAL_PREFIX = "Publication refusal never grants these capabilities:"
CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9-]*")


class GovernanceError(ValueError):
    """Canonical or repository governance is absent or ambiguous."""


def _sections(path: Path = GOVERNANCE_PATH) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GovernanceError(f"cannot load canonical lane governance: {exc}") from exc
    parts = re.split(r"^## ([^\n]+)\n", text, flags=re.M)
    headings = [parts[index].strip() for index in range(1, len(parts) - 1, 2)]
    # A required heading may appear at most once. The mapping below keeps one
    # body per name, so a second copy of a whole section would silently replace
    # the first — and for `## Publication refusal` that means the refusal list
    # comes from whichever copy happened to be last, with the guarantee the
    # document states simply gone. A repeated required heading fails closed;
    # a heading the contract does not require carries no such guarantee and is
    # not checked, so an unrequired section may still repeat.
    repeated = [name for name in REQUIRED_SECTIONS if headings.count(name) > 1]
    if repeated:
        raise GovernanceError(
            "canonical lane governance repeats required sections: "
            + ", ".join(repeated)
        )
    sections = {
        name: parts[index + 1].strip()
        for index, name in zip(range(1, len(parts) - 1, 2), headings)
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


def _drop_grant_bullets(
    body: str, grants: Sequence[tuple[str, str]], override: str
) -> str:
    """Execute-mode rules with the named write grants removed.

    Only the granted bullets are dropped; every other execute rule, including
    the worktree boundary, still applies. Each grant is matched as one complete
    bullet — continuation lines included, after whitespace normalization — and
    must be found exactly once: a missing grant would leave a rule the override
    exists to remove in force, and a duplicated one would make "which bullet is
    the grant" ambiguous, so both fail closed.
    """

    lines = body.splitlines()
    bullets = [
        (indexes, _normalized(" ".join(parts))) for parts, indexes in _bullets(body)
    ]
    dropped: set[int] = set()
    for grant, label in grants:
        expected = _normalized(f"- {grant}")
        matches = [indexes for indexes, text in bullets if text == expected]
        if len(matches) != 1:
            raise GovernanceError(
                f"canonical lane governance must state the execute {label} exactly "
                f"once, as one Markdown bullet, for the {override} override "
                f"to drop it: found {len(matches)} matching bullet(s) for {grant!r}"
            )
        dropped.update(matches[0])
    return "\n".join(line for index, line in enumerate(lines) if index not in dropped)


def _report_execute_body(execute_body: str) -> str:
    """Execute-mode rules with every explicit write grant removed."""

    return _drop_grant_bullets(
        execute_body, EXECUTE_WRITE_GRANTS, "report-deliverable"
    )


def _existing_workspace_execute_body(execute_body: str) -> str:
    """Execute-mode rules with the two bullets this mode supersedes removed.

    Both are about a lane this run did not create. The commit/push grant is
    over *the assigned lane branch*, and an existing owner workspace has none:
    it stands on the owner's own branch, which stood there before this run
    existed. Committing to it would take the owner's staged work with it — a
    plain ``git commit`` commits the index, which may hold somebody else's
    staged change — and pushing it would send the owner's commits, and everyone
    else's, to a remote as a side effect of a worker run. The worktree boundary
    is about a dedicated worktree there is not one of here.

    The replacement rules for both are in the ``## Existing owner workspace``
    section, which is rendered after the active-mode body. The task-scoped
    workflow/messaging grant is kept: it is separately authorized, orthogonal
    to the workspace, and this contract does not narrow it.
    """

    return _drop_grant_bullets(
        execute_body, EXISTING_WORKSPACE_SUPERSEDED, "existing-workspace"
    )


def _declared_capabilities(
    section_body: str, prefix: str, *, noun: str
) -> tuple[str, ...]:
    """Parse one canonical declaration of capabilities a contract refuses.

    The declaration is one machine-readable line in the section that states the
    contract, naming each capability as one backticked identifier:

        Report forbidden write capabilities: `git-push`, `workflow-write`
        Publication refusal never grants these capabilities: `git-push`

    Exactly one such line must exist, it must name at least one capability, and
    each entry must be exactly one backticked identifier separated by commas —
    so a reworded or annotated line fails closed rather than being read
    approximately. Identifiers are validated against the same capability-name
    grammar the tool allowlist uses, and a name declared twice is an error.

    ``noun`` names, in the operator-facing text, the thing a malformed
    declaration failed to supply. It is the only per-call wording: every other
    sentence is shared, so two declarations cannot drift apart in strictness.
    """

    declared = [
        line.strip()
        for line in section_body.splitlines()
        if line.strip().startswith(prefix)
    ]
    if len(declared) != 1:
        raise GovernanceError(
            f"canonical lane governance must declare {noun} on exactly one "
            f"line starting with {prefix!r}: found {len(declared)}"
        )
    tail = declared[0][len(prefix):].strip()
    if not tail:
        raise GovernanceError(
            f"canonical lane governance declares no {noun} after {prefix!r}"
        )
    names: list[str] = []
    for part in tail.split(","):
        entry = part.strip()
        match = re.fullmatch(r"`([^`]+)`", entry)
        if not match:
            raise GovernanceError(
                f"canonical lane governance declares {noun} in a malformed "
                f"entry: {entry!r}"
            )
        name = match.group(1)
        if not CAPABILITY_NAME.fullmatch(name):
            raise GovernanceError(
                f"invalid capability name in the {noun} declaration: {name!r}"
            )
        if name in names:
            raise GovernanceError(
                f"duplicate capability name in the {noun} declaration: {name!r}"
            )
        names.append(name)
    return tuple(names)


def report_forbidden_write_capabilities(
    path: Path = GOVERNANCE_PATH,
) -> tuple[str, ...]:
    """The refused write capabilities, as the canonical document declares them."""

    return _declared_capabilities(
        _sections(path)[REPORT_SECTION],
        REPORT_FORBIDDEN_PREFIX,
        noun="report forbidden write capability",
    )


def publication_refused_capabilities(path: Path = GOVERNANCE_PATH) -> tuple[str, ...]:
    """The capabilities the publication guard refuses, as the document declares them."""

    return _declared_capabilities(
        _sections(path)[PUBLICATION_SECTION],
        PUBLICATION_REFUSAL_PREFIX,
        noun="publication refusal capability",
    )


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


def publication_refusal_capability_conflicts(
    capabilities: Iterable[str], path: Path = GOVERNANCE_PATH
) -> tuple[str, ...]:
    """The explicit publication capabilities of ``capabilities``, in canonical order.

    A lane carrying the task no-external-publication guard denies the `git push`
    command family, so a capability whose whole grant is that family — a push —
    is a dead grant: it can only render an allow rule the same run denies. Such
    a capability is refused rather than handed to a worker, exactly as a report
    lane refuses the write grants it can never exercise. Every other grant is
    untouched: ``workflow-write`` is a separate authority a task may name, and
    ``workspace-write`` and every read capability stay available.

    The refused names come from the canonical document's own declaration, so the
    guard's refusals stay derived from the single source for lane exceptions
    rather than from a list this module maintains alongside it.
    """

    granted = set(capabilities)
    return tuple(
        name for name in publication_refused_capabilities(path) if name in granted
    )


def lane_system_prompt(
    mode: str,
    repository: str | Path,
    *,
    report_deliverable: bool = False,
    existing_workspace: bool = False,
    no_external_publication: bool = False,
    path: Path = GOVERNANCE_PATH,
) -> str:
    if mode not in {"review", "execute"}:
        raise GovernanceError(f"unsupported governance mode: {mode}")
    if report_deliverable and mode != "execute":
        raise GovernanceError("the report deliverable contract is execute mode only")
    if existing_workspace and mode != "execute":
        raise GovernanceError(
            "the existing owner workspace contract is execute mode only"
        )
    if no_external_publication and mode != "execute":
        raise GovernanceError(
            "the publication refusal contract is execute mode only"
        )
    if existing_workspace and report_deliverable:
        raise GovernanceError(
            "the existing owner workspace and report deliverable contracts are "
            "mutually exclusive: a report lane's verdict rejects commits and "
            "unexpected paths, which a workspace holding others' uncommitted "
            "work cannot satisfy"
        )
    repo = Path(repository).expanduser().resolve()
    sections = _sections(path)
    active = "Review mode" if mode == "review" else "Execute mode"
    body = sections[active]
    if report_deliverable:
        body = _report_execute_body(body)
    elif existing_workspace:
        body = _existing_workspace_execute_body(body)
    rendered = (
        "# Injected canonical side-lane governance\n\n"
        "## Common\n\n"
        + sections["Common"]
        + f"\n\n## Active mode: {active}\n\n"
        + body
    )
    if report_deliverable:
        rendered += "\n\n## Report deliverable\n\n" + sections[REPORT_SECTION]
    if no_external_publication:
        # Rendered after the mode body and after the report contract, so a lane
        # carrying both reads the report contract first and this refusal last.
        # The two are independent: a report lane never publishes by contract,
        # while this section states that the *task's* authority forbids
        # publication whatever the lane's own deliverable is.
        rendered += (
            "\n\n## Publication refusal\n\n" + sections[PUBLICATION_SECTION]
        )
    if existing_workspace:
        rendered += (
            "\n\n## Existing owner workspace\n\n"
            + sections[EXISTING_WORKSPACE_SECTION]
        )
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
    # Not a capability either: the publication commands a lane carrying the task
    # no-external-publication guard must not run.  Excluded from ``capabilities``
    # for the same reason — no grant unlocks it, a per-run guard selects it.
    no_publication_denied: tuple[str, ...] = ()
    # ... nor the direct git-write commands an existing-owner-workspace lane
    # must not run.  No grant unlocks them; the lane's workspace selection does.
    existing_workspace_denied: tuple[str, ...] = ()
    # Not a capability either, for the same reason again: the tool surface of
    # the local developer execute profile.  No grant unlocks it; a profile
    # selection does.
    local_developer: tuple[str, ...] = ()

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.allowed) | frozenset(self.denied)


#: The one bullet of the `## Existing owner workspace` section that enumerates
#: the forbidden git commands. Anchored on its opening words rather than on a
#: line number, and matched after whitespace normalization, so a reflowed
#: bullet still resolves; the section's other bullets mention read-only git
#: commands (`git status`, `git log`, `git branch`) that are *not* forbidden,
#: which is why the enumeration is read from this bullet alone.
EXISTING_WORKSPACE_PROHIBITION_OPENS = "- Make no git write of any kind."

#: One backticked ``git <verb>`` mention of that bullet. The character class
#: covers the verbs it names (``cherry-pick``) and excludes anything else.
EXISTING_WORKSPACE_GIT_VERB = re.compile(r"`git ([a-z][a-z0-9-]*)`")


def existing_workspace_denied_verbs(path: Path = GOVERNANCE_PATH) -> tuple[str, ...]:
    """The git verbs the existing-owner-workspace section itself forbids.

    Read back from the prohibition bullet a worker is handed rather than kept
    as a second list here, so that bullet is the one owner of *which* commands
    are forbidden and the allowlist's rules are the one owner of how each verb
    is spelled as a rule. The two are then checked against each other, so a
    verb added to the prose and not to the rules — or the reverse — stops the
    run instead of shipping a prohibition with no seam behind it.
    """

    body = _sections(path)[EXISTING_WORKSPACE_SECTION]
    bullets = [
        _normalized(" ".join(lines)) for lines, _indexes in _bullets(body)
    ]
    prohibitions = [
        bullet for bullet in bullets
        if bullet.startswith(EXISTING_WORKSPACE_PROHIBITION_OPENS)
    ]
    if len(prohibitions) != 1:
        raise GovernanceError(
            "canonical lane governance's existing owner workspace section must "
            "state its no-git-write prohibition exactly once, as one Markdown "
            f"bullet opening {EXISTING_WORKSPACE_PROHIBITION_OPENS!r}: found "
            f"{len(prohibitions)}"
        )
    verbs = tuple(dict.fromkeys(
        EXISTING_WORKSPACE_GIT_VERB.findall(prohibitions[0])
    ))
    if not verbs:
        raise GovernanceError(
            "canonical lane governance's existing owner workspace section names "
            "no forbidden git command"
        )
    return verbs


def _require_existing_workspace_denials(
    rules: Sequence[str], path: Path
) -> tuple[str, ...]:
    """The forbidden verbs ``rules`` fails to deny, in the section's own order.

    A verb the section forbids but the deny rules never name is the exact shape
    of the defect this check exists for: a prohibition a worker reads and no
    host seam carries. Each verb must be denied in both canonical spellings,
    ``Bash(git <verb>)`` and ``Bash(git <verb> *)``, so neither the bare
    invocation nor one carrying arguments is the uncovered spelling. The check
    fails closed rather than trimming the section's list to match the rules.
    """

    declared = set(rules)
    return tuple(
        verb
        for verb in existing_workspace_denied_verbs(path)
        if f"Bash(git {verb})" not in declared
        or f"Bash(git {verb} *)" not in declared
    )


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
    rules for that capability. The reserved headings name no capability: the
    ``report-deliverable (denied)`` heading lists the git-write rules a
    report-deliverable lane must not run, the
    ``no-external-publication (denied)`` heading lists the publication commands
    a lane carrying the task no-external-publication guard must not run, the
    ``existing-workspace (denied)`` heading lists the direct git-write commands
    an existing-owner-workspace lane must not run, and the
    ``local-developer (granted)`` heading lists the tool surface the local
    developer execute profile selects.
    """

    section = _sections(path)[ALLOWLIST_SECTION]
    parts = re.split(r"^### ([^\n]+)\n", section, flags=re.M)
    always: tuple[str, ...] = ()
    allowed: dict[str, list[str]] = {}
    denied: dict[str, list[str]] = {}
    report_denied: tuple[str, ...] = ()
    no_publication_denied: tuple[str, ...] = ()
    existing_workspace_denied: tuple[str, ...] = ()
    local_developer: tuple[str, ...] = ()
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
        # Reserved headings, checked before the generic `(denied)` suffix so
        # their rules land in their own bucket instead of becoming a capability.
        if heading == REPORT_DENIED_HEADING:
            if report_denied:
                raise GovernanceError(
                    f"tool allowlist declares `{REPORT_DENIED_HEADING}` more than once"
                )
            report_denied = rules
            continue
        if heading == PUBLICATION_DENIED_HEADING:
            if no_publication_denied:
                raise GovernanceError(
                    f"tool allowlist declares `{PUBLICATION_DENIED_HEADING}` more "
                    "than once"
                )
            no_publication_denied = rules
            continue
        if heading == EXISTING_WORKSPACE_DENIED_HEADING:
            if existing_workspace_denied:
                raise GovernanceError(
                    f"tool allowlist declares `{EXISTING_WORKSPACE_DENIED_HEADING}` "
                    "more than once"
                )
            existing_workspace_denied = rules
            continue
        if heading == LOCAL_DEVELOPER_HEADING:
            if local_developer:
                raise GovernanceError(
                    f"tool allowlist declares `{LOCAL_DEVELOPER_HEADING}` more than once"
                )
            local_developer = rules
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
    if not no_publication_denied:
        raise GovernanceError(
            f"tool allowlist is missing the `{PUBLICATION_DENIED_HEADING}` subsection"
        )
    if not existing_workspace_denied:
        raise GovernanceError(
            f"tool allowlist is missing the `{EXISTING_WORKSPACE_DENIED_HEADING}` "
            "subsection"
        )
    if not local_developer:
        raise GovernanceError(
            f"tool allowlist is missing the `{LOCAL_DEVELOPER_HEADING}` subsection"
        )
    uncovered = _require_existing_workspace_denials(existing_workspace_denied, path)
    if uncovered:
        raise GovernanceError(
            "canonical lane governance's existing owner workspace section forbids "
            f"git command(s) the `{EXISTING_WORKSPACE_DENIED_HEADING}` rules do "
            "not deny: " + ", ".join(f"git {verb}" for verb in uncovered)
        )
    return ToolPolicy(
        always,
        {k: tuple(v) for k, v in allowed.items()},
        {k: tuple(v) for k, v in denied.items()},
        report_denied=report_denied,
        no_publication_denied=no_publication_denied,
        existing_workspace_denied=existing_workspace_denied,
        local_developer=local_developer,
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
