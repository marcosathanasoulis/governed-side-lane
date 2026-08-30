"""Canonical repository and lane-governance loading.

Host-native instruction discovery differs across Codex and Claude Code.  This
module makes the effective lane contract independent of those differences by
rendering one checked-in Markdown source for every invocation.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_PATH = PACKAGE_ROOT / "config" / "lane-governance.md"
REQUIRED_SECTIONS = ("Common", "Review mode", "Execute mode")


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
        if not (
            re.search(r"\b(?:must|required)\b", line, re.I)
            and re.search(r"\b(?:authoritative|source of truth)\b", line, re.I)
        ):
            continue
        for destination in re.findall(r"\[[^\]]*\]\(([^)]+)\)", line):
            target = destination.strip().strip("<>").split("#", 1)[0]
            authoritative_links.append((repo / target).resolve())
    if authoritative_links != [claude.resolve()]:
        raise GovernanceError("AGENTS.md must link unambiguously to root CLAUDE.md")
    if claude.is_symlink() or claude.resolve().parent != repo:
        raise GovernanceError(
            "root CLAUDE.md must be a regular repository file"
        )
    return repo
