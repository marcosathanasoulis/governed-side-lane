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
from typing import Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_PATH = PACKAGE_ROOT / "config" / "lane-governance.md"
MODELS_PATH = PACKAGE_ROOT / "config" / "models.json"
REQUIRED_SECTIONS = ("Common", "Review mode", "Execute mode", "Execute tool allowlist")
ALLOWLIST_SECTION = "Execute tool allowlist"
ALWAYS = "always"
DENIED_SUFFIX = " (denied)"


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


def lane_system_prompt(
    mode: str, repository: str | Path, *, path: Path = GOVERNANCE_PATH
) -> str:
    if mode not in {"review", "execute"}:
        raise GovernanceError(f"unsupported governance mode: {mode}")
    repo = Path(repository).expanduser().resolve()
    sections = _sections(path)
    active = "Review mode" if mode == "review" else "Execute mode"
    rendered = (
        "# Injected canonical side-lane governance\n\n"
        "## Common\n\n"
        + sections["Common"]
        + f"\n\n## Active mode: {active}\n\n"
        + sections[active]
    )
    return rendered.replace("{{MAIN_CHECKOUT}}", str(repo))


@dataclass(frozen=True)
class ToolPolicy:
    """Execute-lane tool rules parsed from the canonical governance document."""

    always: tuple[str, ...]
    allowed: dict[str, tuple[str, ...]]  # capability -> rules it unlocks
    denied: dict[str, tuple[str, ...]]   # capability -> rules it must deny

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.allowed) | frozenset(self.denied)


def _rules(block: str) -> tuple[str, ...]:
    rules = []
    for line in block.splitlines():
        match = re.match(r"^- `([^`]+)`\s*$", line.strip())
        if match:
            rules.append(match.group(1))
    return tuple(rules)


def tool_policy(path: Path = GOVERNANCE_PATH) -> ToolPolicy:
    """Parse the ``Execute tool allowlist`` section.

    Subsection headings name comma-separated capabilities (or ``always``); a
    heading ending in ``(denied)`` lists rules that must accompany the allow
    rules for that capability. Rules are single inline-code bullets.
    """

    section = _sections(path)[ALLOWLIST_SECTION]
    parts = re.split(r"^### ([^\n]+)\n", section, flags=re.M)
    always: tuple[str, ...] = ()
    allowed: dict[str, list[str]] = {}
    denied: dict[str, list[str]] = {}
    for index in range(1, len(parts) - 1, 2):
        heading, block = parts[index].strip(), parts[index + 1]
        rules = _rules(block)
        if not rules:
            raise GovernanceError(f"tool allowlist subsection has no rules: {heading}")
        if heading == ALWAYS:
            always = rules
            continue
        target = denied if heading.endswith(DENIED_SUFFIX) else allowed
        names = [name.strip() for name in heading.removesuffix(DENIED_SUFFIX).split(",")]
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                raise GovernanceError(f"invalid capability name in tool allowlist: {name!r}")
            target.setdefault(name, []).extend(rules)
    if not always:
        raise GovernanceError("tool allowlist is missing the `always` subsection")
    return ToolPolicy(always, {k: tuple(v) for k, v in allowed.items()}, {k: tuple(v) for k, v in denied.items()})


def known_capabilities(path: Path = MODELS_PATH) -> frozenset[str]:
    """Capability names declared by the runtime allowlist (``config/models.json``)."""

    try:
        names = json.loads(path.read_text(encoding="utf-8")).get("capabilities", [])
    except (OSError, ValueError) as exc:
        raise GovernanceError(f"cannot load capability allowlist: {exc}") from exc
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
        # A negated declaration is not a linkage claim.
        if re.search(r"\b(?:not|never|no longer|isn't|is not|aren't)\b", line, re.I):
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
