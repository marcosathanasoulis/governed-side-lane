"""Pinned worker skill bundle: catalog, validation, and per-lane delivery.

Execute-lane workers on every host need the same task-relevant skill
instructions — the debugging/TDD/verification disciplines above all — but the
hosts discover skills from user-home and plugin-cache roots that exist only on
one machine, in one user's account, or in neither (see
``docs/automatic-side-lane`` in the source repository for the audit). This
module sidesteps discovery entirely:

- the bundle ships inside this plugin package (``skill-bundle/`` beside the
  ``side_lane`` package), so the same pinned copy exists wherever the runner
  runs — the developer checkout, a fresh cloud clone, a published install;
- delivery materializes the selected skill trees into the lane worktree's
  git-ignored ``.side-lane-scratch/skill-bundle/`` directory and adds a
  compact catalog (name, description, absolute runtime path) to the worker's
  task context. Bodies are loaded progressively: the worker opens a
  ``SKILL.md`` only when its description makes it relevant;
- nothing is written to user homes, ``CODEX_HOME``, host settings, or MCP
  configuration, and no tracked file of the target repository is touched.

Evidence boundaries (these are three different claims, kept separate):

1. *Delivery* — files materialized under the worktree and a catalog in the
   task context — is what this module provides and proves.
2. *Native discovery* — whether the host's own Skill subsystem also lists
   these skills — is neither required nor claimed. A delivered file is not
   evidence of host-native discovery.
3. *Live tool calls* remain the only proof of tool connectivity; skill text
   grants no tools and no account authority.

Fail-closed rules, mirroring ``read_roots``: every manifest problem is fatal
with a specific message; a pinned ``sha256`` that no longer matches the tree
is fatal (the pin is the review mechanism, so silent drift is the failure to
prevent); a relative reference inside a skill that escapes its tree, dangles,
or rides a symlink is fatal; a symlink anywhere in a source tree is fatal.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
import shutil

#: The bundle ships as package data one level above this module, exactly like
#: ``config/models.json``. Resolved from ``__file__`` so no absolute path is
#: ever baked into the source; the location works in a checkout, a published
#: install, and a cloud clone alike.
BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "skill-bundle"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1

#: Delivery target, inside the lane worktree's already-git-ignored scratch
#: directory. Scratch is created by the worktree layer before any lane runs
#: and is excluded from ``git status`` via ``.git/info/exclude``, so the
#: materialized skills never touch the target repository's tracked files.
DELIVERED_DIR_NAME = "skill-bundle"

#: Tags delivered to every execute lane. Other tags (``workflow``, ``meta``,
#: ``domain``) are reachable only through explicit named selection
#: (``--skill <name>``); ``delegation`` skills are never delivered at all
#: because worker lanes run without subworkers.
DEFAULT_DELIVERY_TAGS = frozenset({"discipline"})

#: Entries that explicit named selection must refuse, whatever the caller
#: asks for. Delegation skills assume a worker can spawn subworkers, which
#: no execute lane grants; coordinator routing/planning skills never enter
#: the manifest in the first place.
NON_SELECTABLE_TAGS = frozenset({"delegation"})

KNOWN_TAGS = frozenset(
    {"discipline", "workflow", "meta", "delegation", "domain"}
)
SOURCE_KINDS = frozenset({"package", "repo"})

#: A skill name is also the delivered directory name; keep it a safe single
#: path segment so a manifest can never spell a traversal.
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Catalog budget. Descriptions only, one line per skill: the default five
#: discipline skills plus the full QA closure (qa-on-demand pulls in
#: site-uat-sweep, seven entries with long absolute runtime paths) fits
#: with headroom for one more. A selected set whose compacted descriptions
#: still exceed the budget fails delivery rather than silently bloating
#: every task context — bodies are never inlined into the prompt.
MAX_CATALOG_CHARS = 4000
MAX_DESCRIPTION_CHARS = 160


class SkillBundleError(ValueError):
    """A bundle manifest, source tree, or delivery destination is unusable."""


@dataclass(frozen=True)
class BundleEntry:
    """One validated manifest row."""

    name: str
    tags: tuple[str, ...]
    source_kind: str
    source_path: str
    version: str | None
    sha256: str | None
    license: str
    origin: str


@dataclass(frozen=True)
class DeliveredSkill:
    """One skill materialized into a lane worktree."""

    name: str
    description: str
    version: str | None
    sha256: str
    license: str
    origin: str
    directory: Path
    skill_md: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "sha256": self.sha256,
            "license": self.license,
            "origin": self.origin,
            "directory": str(self.directory),
            "skill_md": str(self.skill_md),
        }


def _fail(message: str) -> "SkillBundleError":
    return SkillBundleError(message)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def load_manifest(bundle_root: Path | None = None) -> tuple[BundleEntry, ...]:
    """Parse and strictly validate the bundle manifest.

    Every structural problem is fatal with a message that names the offending
    entry and field, because a half-parsed manifest would deliver an
    unpredictable subset of the reviewed pin.
    """

    root = BUNDLE_ROOT if bundle_root is None else Path(bundle_root)
    path = root / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(f"skill bundle manifest is unreadable: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _fail(f"skill bundle manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise _fail(f"skill bundle manifest requires schema_version {MANIFEST_SCHEMA_VERSION}")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise _fail("skill bundle manifest requires a non-empty entries list")
    entries: list[BundleEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(entries_raw):
        where = f"manifest entry [{index}]"
        if not isinstance(item, Mapping):
            raise _fail(f"{where} must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.match(name):
            raise _fail(f"{where} has an invalid name: {name!r}")
        if name in seen:
            raise _fail(f"{where} duplicates name {name!r}")
        seen.add(name)
        where = f"manifest entry {name!r}"
        tags = item.get("tags")
        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(tag, str) for tag in tags)
        ):
            raise _fail(f"{where} requires a non-empty tags list")
        unknown_tags = sorted(set(tags) - KNOWN_TAGS)
        if unknown_tags:
            raise _fail(f"{where} has unknown tags: {', '.join(unknown_tags)}")
        source = item.get("source")
        if not isinstance(source, Mapping):
            raise _fail(f"{where} requires a source object")
        kind = source.get("kind")
        if kind not in SOURCE_KINDS:
            raise _fail(f"{where} has invalid source kind: {kind!r}")
        source_path = source.get("path")
        if not isinstance(source_path, str) or not source_path:
            raise _fail(f"{where} requires a non-empty source path")
        relative = Path(source_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise _fail(f"{where} source path must be relative and stay inside its root: {source_path!r}")
        version = item.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            raise _fail(f"{where} version must be a non-empty string or null")
        sha256 = item.get("sha256")
        if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
            raise _fail(f"{where} sha256 must be a 64-character hex digest or null")
        if kind == "package" and sha256 is None:
            raise _fail(f"{where} package sources must be hash-pinned")
        license_value = item.get("license")
        if not isinstance(license_value, str) or not license_value:
            raise _fail(f"{where} requires a license")
        origin = item.get("origin")
        if not isinstance(origin, str) or not origin:
            raise _fail(f"{where} requires an origin")
        entries.append(
            BundleEntry(
                name=name,
                tags=tuple(sorted(set(tags))),
                source_kind=str(kind),
                source_path=source_path,
                version=version,
                sha256=sha256,
                license=license_value,
                origin=origin,
            )
        )
    return tuple(entries)


def select_entries(
    entries: Sequence[BundleEntry], tags: Sequence[str] | frozenset[str]
) -> tuple[BundleEntry, ...]:
    """Entries sharing at least one requested tag, in manifest order."""

    wanted = set(tags)
    return tuple(entry for entry in entries if wanted & set(entry.tags))


def selectable_entries(entries: Sequence[BundleEntry]) -> tuple[BundleEntry, ...]:
    """Entries explicit named selection may choose, in manifest order."""

    return tuple(
        entry for entry in entries if not NON_SELECTABLE_TAGS & set(entry.tags)
    )


def select_named_entries(
    entries: Sequence[BundleEntry], names: Sequence[str]
) -> tuple[BundleEntry, ...]:
    """Entries chosen by exact skill name (``--skill``), in manifest order.

    Named selection is additive to the default tag selection and is the only
    route by which a non-default skill — the private QA skills above all —
    reaches a worker. An unknown name is fatal with the selectable names
    listed (the caller asked for something specific; guessing silently
    would deliver the wrong instructions), and a delegation-tagged entry is
    refused outright: no lane can honor what it assumes.
    """

    by_name = {entry.name: entry for entry in entries}
    chosen: list[BundleEntry] = []
    for name in names:
        entry = by_name.get(name)
        if entry is None:
            available = ", ".join(
                entry.name for entry in selectable_entries(entries)
            )
            raise _fail(
                f"no skill named {name!r} in the bundle manifest; selectable "
                f"skills are: {available}"
            )
        if NON_SELECTABLE_TAGS & set(entry.tags):
            raise _fail(
                f"skill {name!r} is tagged "
                f"{', '.join(sorted(NON_SELECTABLE_TAGS & set(entry.tags)))} "
                f"and can never be delivered to a worker lane"
            )
        if entry not in chosen:
            chosen.append(entry)
    return tuple(chosen)


# ---------------------------------------------------------------------------
# Source-tree validation
# ---------------------------------------------------------------------------


def first_symlink(root: Path) -> str | None:
    """First (sorted) relative symlink path under ``root``, or ``None``.

    A symlink in a source tree could point outside the reviewed bundle, so
    validation refuses them instead of dereferencing.
    """

    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                hits.append(full.relative_to(root).as_posix())
    return sorted(hits)[0] if hits else None


def tree_sha256(root: Path) -> str:
    """Deterministic content hash over a directory tree.

    Sorted relative paths and file bytes, length-prefixed, so the hash is
    stable across filesystems and copies. Symlinks are refused rather than
    dereferenced (dereferencing would hash content outside the tree).
    """

    symlink = first_symlink(root)
    if symlink is not None:
        raise _fail(f"refusing to hash a tree containing a symlink: {root}/{symlink}")
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if not p.is_dir()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse a ``---``-delimited frontmatter block into flat key/value pairs.

    Raises ``SkillBundleError`` when the block is absent or unterminated.
    """

    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise _fail("SKILL.md has no opening '---' frontmatter marker")
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    raise _fail("SKILL.md has no closing '---' frontmatter marker")


#: Markdown link/image targets: ``[label](target)``, ``![alt](target)``.
#: Fragments and URLs are skipped during reference checking.
_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
_SKIP_TARGET_SCHEMES = ("http://", "https://", "mailto:")
#: Fenced code blocks and inline code spans, stripped before link extraction:
#: a ``[label](target)`` inside code is quoted text (skill docs quote CLI
#: output and config snippets), not a reference the delivery must resolve.
#: Double-quoted spans are stripped for the same reason — skills document
#: exact sentences other files must contain, sometimes with link syntax in
#: the quoted sentence. Residual under-check: a real link written inside a
#: quoted span is skipped; that is preferred over failing every skill that
#: quotes one.
_FENCED_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
#: May span line wraps (a quoted sentence can break across lines); the
#: length cap keeps an unbalanced quote from swallowing the document.
_QUOTED_SPAN = re.compile(r'"[^"]{0,400}"')


def _markdown_targets(text: str) -> list[str]:
    stripped = _QUOTED_SPAN.sub('"', _INLINE_CODE.sub("`", _FENCED_BLOCK.sub("", text)))
    targets: list[str] = []
    for match in _LINK_TARGET.finditer(stripped):
        target = match.group(1).strip()
        if not target or target.startswith("#"):
            continue
        if target.lower().startswith(_SKIP_TARGET_SCHEMES):
            continue
        targets.append(target)
    return targets


def _referenced_skill_names(
    source_root: Path, name: str, known_names: "set[str]"
) -> "set[str]":
    """Manifest skills this tree's markdown references as siblings.

    Uses the same extraction and lexical resolution as
    ``check_relative_references``: a target normalizes to ``<skill>/<rest>``
    and only a head segment naming a known manifest entry counts. Targets
    that escape, are absolute, or name nothing in the manifest are ignored
    here — validation remains the fail-closed authority over those (a
    reference to an unlisted sibling stays a hard error, never a silent
    drop); this helper only decides what closure must *add*.
    """

    found: set[str] = set()
    for path in sorted(source_root.rglob("*.md")):
        relative = path.relative_to(source_root).as_posix()
        base_dir = posixpath.dirname(posixpath.join(name, relative))
        text = path.read_text(encoding="utf-8", errors="strict")
        for target in _markdown_targets(text):
            file_part = target.split("#", 1)[0]
            if not file_part or PurePosixPath(file_part).is_absolute():
                continue
            normalized = posixpath.normpath(posixpath.join(base_dir, file_part))
            head = normalized.partition("/")[0]
            if head in known_names and head != name:
                found.add(head)
    return found


def check_relative_references(
    skill_root: Path,
    name: str,
    delivery: Mapping[str, Path] | None = None,
) -> None:
    """Every markdown link target must resolve inside the delivery.

    ``delivery`` maps each selected skill name to its source directory (the
    delivered layout: every skill sits in a sibling directory under the
    delivery root). A target may reference the skill's own files or a
    sibling skill's files — canonical in-repo skills reference each other,
    e.g. ``../site-uat-sweep/SKILL.md`` — but never anything outside the
    selected set: an escape would dangle in the delivered copy, and a
    reference into a skill that is not part of this delivery would dangle
    too. Absolute targets are refused (a skill referencing host-specific
    absolute paths is not relocatable to a worktree). Resolution is lexical
    (``..`` is normalized without touching the filesystem), so a symlink
    cannot redirect it; symlinks are rejected separately. Only ``](...)``
    targets are machine-checkable; bare path mentions in prose are out of
    scope for this check.
    """

    siblings = delivery if delivery is not None else {name: skill_root}
    for path in sorted(skill_root.rglob("*.md")):
        relative = path.relative_to(skill_root).as_posix()
        base_dir = posixpath.dirname(posixpath.join(name, relative))
        text = path.read_text(encoding="utf-8", errors="strict")
        for target in _markdown_targets(text):
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue
            if PurePosixPath(file_part).is_absolute():
                raise _fail(
                    f"skill {name!r} references an absolute path "
                    f"({relative}: {target!r})"
                )
            normalized = posixpath.normpath(posixpath.join(base_dir, file_part))
            if normalized == ".." or normalized.startswith("../"):
                raise _fail(
                    f"skill {name!r} reference escapes the delivery "
                    f"({relative}: {target!r})"
                )
            head, _, rest = normalized.partition("/")
            source = siblings.get(head)
            if source is None:
                raise _fail(
                    f"skill {name!r} references {target!r} but skill {head!r} "
                    f"is not part of this delivery ({relative})"
                )
            if not (source / rest).is_file():
                raise _fail(
                    f"skill {name!r} references a missing file "
                    f"({relative}: {target!r})"
                )


def validate_skill(
    entry: BundleEntry, source_root: Path, delivery: Mapping[str, Path]
) -> str:
    """Validate one entry's source tree; return its frontmatter description.

    Checks: no symlinks anywhere in the tree; ``SKILL.md`` exists, is
    non-empty, parses frontmatter whose ``name`` matches the manifest name,
    and carries a non-empty ``description``; every markdown reference stays
    inside the delivery and resolves; a pinned ``sha256`` matches the tree.
    """

    if not source_root.is_dir():
        raise _fail(
            f"skill {entry.name!r} source directory is missing: {source_root}"
        )
    symlink = first_symlink(source_root)
    if symlink is not None:
        raise _fail(
            f"skill {entry.name!r} source tree contains a symlink: {symlink}"
        )
    skill_md = source_root / "SKILL.md"
    if not skill_md.is_file():
        raise _fail(f"skill {entry.name!r} has no SKILL.md: {skill_md}")
    text = skill_md.read_text(encoding="utf-8")
    if not text.strip():
        raise _fail(f"skill {entry.name!r} has an empty SKILL.md")
    meta = parse_frontmatter(text)
    frontmatter_name = meta.get("name")
    if frontmatter_name != entry.name:
        raise _fail(
            f"skill {entry.name!r} SKILL.md frontmatter name is {frontmatter_name!r}"
        )
    description = meta.get("description", "")
    if not description:
        raise _fail(f"skill {entry.name!r} SKILL.md frontmatter has no description")
    check_relative_references(source_root, entry.name, delivery)
    actual = tree_sha256(source_root)
    if entry.sha256 is not None and actual != entry.sha256:
        raise _fail(
            f"skill {entry.name!r} tree hash {actual} does not match the pinned "
            f"{entry.sha256}; the bundle pin and the shipped tree have drifted"
        )
    return description


# ---------------------------------------------------------------------------
# Vendor provenance
# ---------------------------------------------------------------------------


def verify_vendor_provenance(bundle_root: Path) -> None:
    """Verify each vendored tree's PROVENANCE.json against its content.

    The recorded ``tree_sha256`` covers exactly ``LICENSE``, ``README.md``
    and ``skills/`` (never ``PROVENANCE.json`` itself, which would be
    circular), so the hash pins license retention too: a vendored tree whose
    license file is removed no longer matches its provenance record. Any
    mismatch or unreadable record is fatal — the provenance file is the
    reviewed statement of what was vendored, and the tree must obey it.
    """

    for provenance_path in sorted((bundle_root).glob("*/PROVENANCE.json")):
        vendor_root = provenance_path.parent
        try:
            record = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _fail(f"unreadable provenance record {provenance_path}: {exc}") from exc
        if not isinstance(record, Mapping):
            raise _fail(f"provenance record {provenance_path} must be an object")
        pinned = record.get("tree_sha256")
        if not isinstance(pinned, str) or not re.fullmatch(r"[0-9a-f]{64}", pinned):
            raise _fail(f"provenance record {provenance_path} has no tree_sha256")
        digest = hashlib.sha256()
        for name in ("LICENSE", "README.md"):
            file_path = vendor_root / name
            if not file_path.is_file():
                raise _fail(f"vendored tree {vendor_root.name} is missing {name}")
            _absorb_file(digest, vendor_root, file_path)
        skills_root = vendor_root / "skills"
        if not skills_root.is_dir():
            raise _fail(f"vendored tree {vendor_root.name} has no skills/ directory")
        _absorb_tree(digest, vendor_root, skills_root)
        actual = digest.hexdigest()
        if actual != pinned:
            raise _fail(
                f"vendored tree {vendor_root.name} content hash {actual} does not "
                f"match its provenance record {pinned}"
            )


def _absorb_file(digest: "hashlib._Hash", base: Path, file_path: Path) -> None:
    relative = file_path.relative_to(base).as_posix().encode("utf-8")
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    data = file_path.read_bytes()
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _absorb_tree(digest: "hashlib._Hash", base: Path, root: Path) -> None:
    for path in sorted(p for p in root.rglob("*") if not p.is_dir()):
        _absorb_file(digest, base, path)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def resolve_repo_root(bundle_root: Path | None = None) -> Path | None:
    """Locate the dev-tools checkout root for ``repo`` sources.

    ``repo``-kind entries point at the canonical in-repo skill trees (no
    second editable copy). Those exist only when the runner executes from a
    dev-tools checkout, which is every bin/side-lane launch and every cloud
    clone; a published install alone has none, and ``None`` is returned
    rather than guessed. Identified by marker files (``bin/side-lane`` plus
    the ``public/governed-side-lane`` source tree), not by a fixed parent
    depth: the plugin package and the published repo each ship pieces that
    look like the checkout root (``bin/side-lane``, ``skills/``, a public
    ``config/models.json``), so weaker markers resolve to the wrong root.
    """

    root = BUNDLE_ROOT if bundle_root is None else Path(bundle_root)
    for parent in root.parents:
        if (
            (parent / "bin" / "side-lane").is_file()
            and (parent / "public" / "governed-side-lane").is_dir()
        ):
            return parent
    return None


def _source_root(entry: BundleEntry, bundle_root: Path, repo_root: Path | None) -> Path:
    if entry.source_kind == "package":
        return bundle_root / entry.source_path
    if repo_root is None:
        raise _fail(
            f"skill {entry.name!r} needs the dev-tools checkout (repo source "
            f"{entry.source_path!r}) but no checkout root was found next to the "
            f"bundle; repo-sourced skills are private to the dev-tools checkout — "
            f"launch from a dev-tools checkout (bin/side-lane) or start this lane "
            f"without --skill {entry.name}"
        )
    return repo_root / entry.source_path


def _checkout_commit(repo_root: Path) -> str | None:
    """HEAD commit of the checkout, read straight from ``.git``.

    No subprocess and no host state beyond the checkout's own files, so it
    works in a plain clone and a linked worktree alike (a worktree's
    ``.git`` is a ``gitdir:`` pointer file). Any unreadable or unsupported
    shape yields ``None`` — provenance is recorded when it can be proven,
    never guessed.
    """

    git = repo_root / ".git"
    try:
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git = Path(pointer.partition(":")[2].strip())
            if not git.is_absolute():
                git = repo_root / git
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            if not ref.startswith("refs/") or ".." in PurePosixPath(ref).parts:
                return None
            common = git
            if (git / "commondir").is_file():
                common = Path((git / "commondir").read_text(encoding="utf-8").strip())
                if not common.is_absolute():
                    common = git / common
            for directory in (git, common):
                loose = directory / ref
                if loose.is_file():
                    commit = loose.read_text(encoding="utf-8").strip()
                    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None
            packed = common / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[1] == ref:
                        return fields[0] if re.fullmatch(r"[0-9a-f]{40}", fields[0]) else None
            return None
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return head
    except OSError:
        return None
    return None


def _copy_tree(source: Path, destination: Path) -> None:
    for path in sorted(p for p in source.rglob("*") if not p.is_dir()):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def delivered_root(worktree: Path) -> Path:
    """Materialization root inside the lane worktree's scratch directory."""

    from side_lane.worktrees import SCRATCH_DIR_NAME

    return Path(worktree) / SCRATCH_DIR_NAME / DELIVERED_DIR_NAME


def deliver_skills(
    worktree: Path,
    *,
    bundle_root: Path | None = None,
    repo_root: Path | None | object = None,
    tags: Sequence[str] | frozenset[str] = DEFAULT_DELIVERY_TAGS,
    skills: Sequence[str] = (),
) -> tuple[DeliveredSkill, ...]:
    """Materialize the selected skills into the lane worktree.

    Returns one record per delivered skill (audit-shaped). The destination is
    ``<worktree>/.side-lane-scratch/skill-bundle/``, created fresh; an
    existing non-empty destination is refused because a lane worktree is
    single-use and a leftover copy could be stale relative to the pin.
    Vendor licenses travel with the delivery (MIT retention) under
    ``licenses/`` next to the skill directories.

    ``skills`` names explicit additions to the tag selection (``--skill``):
    additive to the discipline defaults, never a replacement, and closed
    under references — a selected skill that references a manifest sibling
    (qa-on-demand layers on site-uat-sweep) pulls that sibling in, because
    silently delivering a reference that dangles in the copy is the failure
    this module exists to prevent. References to anything outside the
    manifest stay hard failures.

    ``repo_root``: ``None`` (default) auto-resolves the dev-tools checkout;
    pass a ``Path`` to pin it (tests), or the string ``"forbid"`` to simulate
    a published install where no checkout exists — any selected repo-kind
    entry then fails with the same message auto-resolution produces.
    """

    root = BUNDLE_ROOT if bundle_root is None else Path(bundle_root)
    manifest = load_manifest(root)
    selected: list[BundleEntry] = []
    for entry in (*select_entries(manifest, tags), *select_named_entries(manifest, skills)):
        if entry not in selected:
            selected.append(entry)
    if not selected:
        raise _fail(
            f"skill bundle selected no entries for tags {sorted(set(tags))}"
        )
    # Whole-vendor check before anything is written: the provenance record is
    # the reviewed statement of the vendored content, license included.
    if (root / "manifest.json").is_file():
        verify_vendor_provenance(root)
    if repo_root is None:
        resolved_repo: Path | None = resolve_repo_root(root)
    elif isinstance(repo_root, str):
        if repo_root != "forbid":
            raise _fail(f"invalid repo_root selector: {repo_root!r}")
        resolved_repo = None
    else:
        resolved_repo = Path(repo_root)

    # Resolve every source, then walk the dependency closure to a fixpoint:
    # each resolved tree's markdown may reference manifest siblings, and
    # each addition needs its own source resolved (under "forbid" that is
    # exactly where a private skill requested by name fails, before
    # anything is written).
    by_name = {entry.name: entry for entry in manifest}
    sources: dict[str, Path] = {}
    chosen_names = {entry.name for entry in selected}
    pending = list(selected)
    while pending:
        entry = pending.pop(0)
        # Dependencies must obey the same selection restrictions as direct requests.
        select_named_entries(manifest, [entry.name])
        source = _source_root(entry, root, resolved_repo)
        sources[entry.name] = source
        for referenced in _referenced_skill_names(
            source, entry.name, set(by_name)
        ):
            if referenced not in chosen_names:
                chosen_names.add(referenced)
                pending.append(by_name[referenced])
    # Manifest order, so the delivered set is stable regardless of which
    # entry the closure reached first.
    entries = [entry for entry in manifest if entry.name in chosen_names]

    # Validate every source before writing anything: reference checking
    # needs the whole delivery set (skills reference selected siblings),
    # and a mid-delivery failure should leave no partial copy behind.
    descriptions = {
        entry.name: validate_skill(entry, sources[entry.name], sources)
        for entry in entries
    }
    repo_commit = (
        _checkout_commit(resolved_repo) if resolved_repo is not None else None
    )

    destination = delivered_root(worktree)
    if destination.exists() and any(destination.iterdir()):
        raise _fail(f"skill delivery destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    records: list[DeliveredSkill] = []
    licenses_dir = destination / "licenses"
    seen_licenses: set[str] = set()
    try:
        for entry in entries:
            source = sources[entry.name]
            description = descriptions[entry.name]
            skill_dir = destination / entry.name
            _copy_tree(source, skill_dir)
            # Retain the vendor license next to the delivered content. The
            # license file lives at the vendored tree root (the parent above
            # ``skills/<name>``); entries from other sources carry their
            # license label in the record only.
            if entry.source_kind == "package":
                vendor_root = (root / entry.source_path).parents[1]
                license_file = vendor_root / "LICENSE"
                if license_file.is_file() and vendor_root.name not in seen_licenses:
                    seen_licenses.add(vendor_root.name)
                    licenses_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(
                        license_file, licenses_dir / f"{vendor_root.name}-LICENSE"
                    )
            records.append(
                DeliveredSkill(
                    name=entry.name,
                    description=description,
                    # Repo-sourced trees have no per-entry pin; the checkout
                    # commit IS the pin, so it is what the audit records.
                    version=(
                        repo_commit
                        if entry.source_kind == "repo" and repo_commit is not None
                        else entry.version
                    ),
                    sha256=tree_sha256(skill_dir),
                    license=entry.license,
                    origin=entry.origin,
                    directory=skill_dir,
                    skill_md=skill_dir / "SKILL.md",
                )
            )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return tuple(records)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _compact_description(description: str) -> str:
    cleaned = " ".join(description.split())
    if len(cleaned) <= MAX_DESCRIPTION_CHARS:
        return cleaned
    cut = cleaned[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
    return cut + " …"


def catalog_note(records: Sequence[DeliveredSkill]) -> str:
    """Worker-instruction catalog: descriptions and paths, never bodies.

    States the evidence boundary explicitly — these are readable files, not a
    host-native skill registration — and stays under ``MAX_CATALOG_CHARS`` so
    a manifest cannot quietly bloat every task context.
    """

    if not records:
        return ""
    lines = [
        "## Delivered worker skills (read on demand)",
        "",
        "The coordinator delivered pinned skill instructions into this lane",
        "worktree. Each entry below names its absolute path; open that",
        "`SKILL.md` with your file tools when — and only when — its",
        "description applies to your task, and follow its relative references",
        "from the same directory. These are delivered reference files, not a",
        "claim of host-native skill discovery, and they grant no tools or",
        "account authority beyond your lane's existing grants.",
        "",
    ]
    lines.extend(
        f"- **{record.name}** — {_compact_description(record.description)}"
        f" (`{record.skill_md}`)"
        for record in sorted(records, key=lambda record: record.name)
    )
    note = "\n".join(lines)
    if len(note) > MAX_CATALOG_CHARS:
        raise _fail(
            f"skill catalog exceeds the {MAX_CATALOG_CHARS}-character budget "
            f"({len(note)}); curate the selected entries or shorten descriptions"
        )
    return note
