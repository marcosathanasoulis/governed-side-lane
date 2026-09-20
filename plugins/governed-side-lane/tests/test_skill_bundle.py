"""Skill bundle: manifest validation, fail-closed checks, and delivery.

The property under test mirrors ``read_roots``: what is delivered must be
exactly the reviewed pin — every manifest problem, hash drift, dangling
reference, escape, or symlink is fatal with a message that names the cause —
and delivery must change nothing outside ``<worktree>/.side-lane-scratch``.

Fixture bundles are synthetic so failure modes are deterministic; one test
additionally validates the real checked-in bundle end to end.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from side_lane import skill_bundle


def write_skill(
    root: Path,
    name: str,
    description: "str | None" = None,
    body: str = "Instructions.\n",
    extra: "dict[str, str] | None" = None,
) -> Path:
    if description is None:
        description = f"{name} description"
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    parts = ["---", f"name: {name}", f"description: {description}", "---", "", body]
    (skill_dir / "SKILL.md").write_text("\n".join(parts), encoding="utf-8")
    for relative, text in (extra or {}).items():
        target = skill_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return skill_dir


def write_manifest(root: Path, entries: list[dict], schema_version: int = 1) -> Path:
    payload = {"schema_version": schema_version, "entries": entries}
    path = root / "manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def package_entry(name: str, skill_dir: Path, bundle_root: Path, **overrides: object) -> dict:
    """A manifest row for a package-kind skill (path is bundle-root relative)."""

    entry = {
        "name": name,
        "tags": ["discipline"],
        "source": {"kind": "package", "path": str(skill_dir.relative_to(bundle_root))},
        "version": "1.0.0",
        "sha256": skill_bundle.tree_sha256(skill_dir),
        "license": "MIT",
        "origin": "fixture",
    }
    entry.update(overrides)
    return entry


def build_bundle(root: Path) -> Path:
    """A minimal valid bundle: one vendor with a license and one skill."""

    vendor = root / "fixture-vendor-1.0.0"
    (vendor / "skills").mkdir(parents=True)
    (vendor / "LICENSE").write_text("MIT fixture license\n", encoding="utf-8")
    (vendor / "README.md").write_text("fixture readme\n", encoding="utf-8")
    skill = write_skill(
        vendor / "skills",
        "fixture-skill",
        extra={"references/guide.md": "Guide with [link](../SKILL.md).\n"},
    )
    write_manifest(root, [package_entry("fixture-skill", skill, root)])
    return root


class ManifestValidationTests(unittest.TestCase):
    def bundle(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        return root

    def test_valid_manifest_loads(self) -> None:
        entries = skill_bundle.load_manifest(self.bundle())
        self.assertEqual([entry.name for entry in entries], ["fixture-skill"])
        self.assertEqual(entries[0].tags, ("discipline",))

    def test_missing_manifest_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(skill_bundle.SkillBundleError) as caught:
                skill_bundle.load_manifest(Path(tmp))
            self.assertIn("unreadable", str(caught.exception))

    def test_malformed_json_fails_clearly(self) -> None:
        root = self.bundle()
        (root / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_wrong_schema_version_fails(self) -> None:
        root = self.bundle()
        write_manifest(root, [], schema_version=2)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("schema_version", str(caught.exception))

    def test_traversal_name_fails(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        entry = package_entry("fixture-skill", skill, root)
        entry["name"] = "../escape"
        write_manifest(root, [entry])
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("invalid name", str(caught.exception))

    def test_traversal_source_path_fails(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        entry = package_entry("fixture-skill", skill, root)
        entry["source"] = {"kind": "package", "path": "../../outside"}
        write_manifest(root, [entry])
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("stay inside its root", str(caught.exception))

    def test_unpinned_package_source_fails(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        entry = package_entry("fixture-skill", skill, vendor, sha256=None)
        write_manifest(root, [entry])
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("hash-pinned", str(caught.exception))

    def test_unknown_tag_fails(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        entry = package_entry("fixture-skill", skill, vendor, tags=["who-knows"])
        write_manifest(root, [entry])
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("unknown tags", str(caught.exception))

    def test_duplicate_name_fails(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        write_manifest(root, [
            package_entry("fixture-skill", skill, root),
            package_entry("fixture-skill", skill, root),
        ])
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.load_manifest(root)
        self.assertIn("duplicates name", str(caught.exception))


class SourceValidationTests(unittest.TestCase):
    def bundle_with_entry(self, mutate) -> tuple[Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        entry = package_entry("fixture-skill", skill, root)
        mutate(root, vendor, skill, entry)
        write_manifest(root, [entry])
        return root, skill

    def deliver(self, root: Path, **kwargs) -> None:
        kwargs.setdefault("repo_root", "forbid")
        with tempfile.TemporaryDirectory() as worktree:
            skill_bundle.deliver_skills(Path(worktree), bundle_root=root, **kwargs)

    def test_valid_skill_delivers(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, repo_root="forbid"
            )
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.name, "fixture-skill")
            self.assertTrue(record.skill_md.is_file())
            delivered = Path(record.directory)
            self.assertIn(".side-lane-scratch", delivered.parts)
            self.assertEqual(
                (delivered / "references" / "guide.md").read_text(encoding="utf-8"),
                "Guide with [link](../SKILL.md).\n",
            )
            licenses = delivered.parent / "licenses"
            self.assertEqual(
                sorted(path.name for path in licenses.iterdir()),
                ["fixture-vendor-1.0.0-LICENSE"],
            )

    def test_missing_skill_md_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            (skill / "SKILL.md").unlink()
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("no SKILL.md", str(caught.exception))

    def test_empty_skill_md_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            (skill / "SKILL.md").write_text("", encoding="utf-8")
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("empty SKILL.md", str(caught.exception))

    def test_frontmatter_name_mismatch_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            path = skill / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace("name: fixture-skill", "name: other"),
                encoding="utf-8",
            )
            entry["sha256"] = skill_bundle.tree_sha256(skill)
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("frontmatter name", str(caught.exception))

    def test_hash_drift_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            (skill / "extra.md").write_text("unreviewed content\n", encoding="utf-8")
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("does not match the pinned", str(caught.exception))

    def test_missing_reference_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            guide = skill / "references" / "guide.md"
            guide.write_text("See [missing](./absent.md).\n", encoding="utf-8")
            entry["sha256"] = skill_bundle.tree_sha256(skill)
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("missing file", str(caught.exception))

    def test_reference_escape_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            path = skill / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8") + "\nEscapes to [outside](../../../etc/passwd).\n",
                encoding="utf-8",
            )
            entry["sha256"] = skill_bundle.tree_sha256(skill)
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("escapes the delivery", str(caught.exception))

    def test_absolute_reference_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            path = skill / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8") + "\nAbsolute [x](/etc/passwd).\n",
                encoding="utf-8",
            )
            entry["sha256"] = skill_bundle.tree_sha256(skill)
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("absolute path", str(caught.exception))

    def test_reference_to_unselected_skill_fails(self) -> None:
        def mutate(_root, _vendor, skill, entry):
            path = skill / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nSee [sibling](../not-delivered/SKILL.md).\n",
                encoding="utf-8",
            )
            entry["sha256"] = skill_bundle.tree_sha256(skill)
        root, _ = self.bundle_with_entry(mutate)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root)
        self.assertIn("not part of this delivery", str(caught.exception))

    def test_sibling_reference_within_delivery_resolves(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        vendor = root / "fixture-vendor-1.0.0"
        (vendor / "skills").mkdir(parents=True)
        (vendor / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (vendor / "README.md").write_text("r\n", encoding="utf-8")
        first = write_skill(
            vendor / "skills",
            "first-skill",
            body="See [second](../second-skill/SKILL.md).\n",
        )
        second = write_skill(vendor / "skills", "second-skill")
        write_manifest(root, [
            package_entry("first-skill", first, root),
            package_entry("second-skill", second, root),
        ])
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, repo_root="forbid"
            )
        self.assertEqual(sorted(record.name for record in records), ["first-skill", "second-skill"])

    def test_symlink_in_source_fails(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        vendor = root / "fixture-vendor-1.0.0"
        skill = vendor / "skills" / "fixture-skill"
        outside = root / "outside-secret.md"
        outside.write_text("host content\n", encoding="utf-8")
        (skill / "references" / "escape.md").symlink_to(outside)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.deliver_skills(
                Path(tempfile.mkdtemp()), bundle_root=root, repo_root="forbid"
            )
        self.assertIn("symlink", str(caught.exception))
        outside.unlink()

    def test_quoted_and_coded_link_text_is_not_a_reference(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        vendor = root / "fixture-vendor-1.0.0"
        (vendor / "skills").mkdir(parents=True)
        (vendor / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (vendor / "README.md").write_text("r\n", encoding="utf-8")
        skill = write_skill(
            vendor / "skills",
            "fixture-skill",
            body=(
                "The exact sentence \"must read [CLAUDE.md](./CLAUDE.md) …\"\n"
                "and inline code `[x](./nope.md)` are quoted text.\n"
            ),
        )
        write_manifest(root, [package_entry("fixture-skill", skill, root)])
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, repo_root="forbid"
            )
        self.assertEqual(len(records), 1)

    def test_failure_leaves_no_partial_delivery(self) -> None:
        root, _ = self.bundle_with_entry(
            lambda _r, _v, skill, entry: (skill / "SKILL.md").unlink()
        )
        with tempfile.TemporaryDirectory() as worktree:
            with self.assertRaises(skill_bundle.SkillBundleError):
                skill_bundle.deliver_skills(Path(worktree), bundle_root=root)
            self.assertFalse(
                (Path(worktree) / ".side-lane-scratch" / "skill-bundle").exists()
            )

    def test_nonempty_destination_refused(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        with tempfile.TemporaryDirectory() as worktree:
            destination = Path(worktree) / ".side-lane-scratch" / "skill-bundle"
            destination.mkdir(parents=True)
            (destination / "stale.txt").write_text("stale\n", encoding="utf-8")
            with self.assertRaises(skill_bundle.SkillBundleError) as caught:
                skill_bundle.deliver_skills(Path(worktree), bundle_root=root)
            self.assertIn("not empty", str(caught.exception))

    def test_no_entries_selected_fails(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.deliver_skills(
                Path(tempfile.mkdtemp()), bundle_root=root, tags=["domain"]
            )
        self.assertIn("selected no entries", str(caught.exception))


def repo_entry(name: str, source_path: str, **overrides: object) -> dict:
    """A manifest row for a repo-kind skill (path is checkout-root relative)."""

    entry = {
        "name": name,
        "tags": ["domain"],
        "source": {"kind": "repo", "path": source_path},
        "version": None,
        "sha256": None,
        "license": "internal (fixture)",
        "origin": "fixture checkout",
    }
    entry.update(overrides)
    return entry


def build_checkout(root: Path, commit: str = "a" * 40) -> Path:
    """A minimal dev-tools-shaped checkout: markers, skills, and a HEAD."""

    (root / "bin").mkdir(parents=True)
    (root / "bin" / "side-lane").write_text("#!fixture\n", encoding="utf-8")
    (root / "public" / "governed-side-lane").mkdir(parents=True)
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(commit + "\n", encoding="utf-8")
    return root


class NamedSelectionTests(unittest.TestCase):
    """Explicit ``--skill`` selection: additive, closed over references, private-safe.

    Named selection is the only way a non-default entry (the private QA
    skills above all) reaches a worker: it must add to — never replace — the
    discipline defaults, pull in every referenced sibling rather than
    silently dropping the reference, and fail actionably when a private
    (repo-sourced) skill is requested where no checkout can supply it.
    """

    def selectable_bundle(self, root: Path) -> Path:
        """Discipline default + domain skill referencing a workflow sibling."""

        vendor = root / "fixture-vendor-1.0.0"
        (vendor / "skills").mkdir(parents=True)
        (vendor / "LICENSE").write_text("MIT fixture license\n", encoding="utf-8")
        (vendor / "README.md").write_text("fixture readme\n", encoding="utf-8")
        write_skill(vendor / "skills", "fixture-skill")
        write_skill(
            vendor / "skills",
            "domain-skill",
            body=(
                "Layers on [sibling](../sibling-skill/SKILL.md); the contract is "
                "[schema](./templates/schema.json).\n"
            ),
            extra={"templates/schema.json": "{}\n"},
        )
        write_skill(vendor / "skills", "sibling-skill")
        write_manifest(root, [
            package_entry("fixture-skill", vendor / "skills" / "fixture-skill", root),
            package_entry(
                "domain-skill", vendor / "skills" / "domain-skill", root,
                tags=["domain"],
            ),
            package_entry(
                "sibling-skill", vendor / "skills" / "sibling-skill", root,
                tags=["workflow"],
            ),
        ])
        return root

    def deliver(self, root: Path, **kwargs):
        kwargs.setdefault("repo_root", "forbid")
        with tempfile.TemporaryDirectory() as worktree:
            return skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, **kwargs
            )

    def bundle(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return self.selectable_bundle(Path(temporary.name))

    def test_named_selection_is_additive_and_closes_over_references(self) -> None:
        root = self.bundle()
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, repo_root="forbid",
                skills=["domain-skill"],
            )
            self.assertEqual(
                sorted(record.name for record in records),
                ["domain-skill", "fixture-skill", "sibling-skill"],
            )
            domain = next(r for r in records if r.name == "domain-skill")
            # The referenced template materializes inside the delivery, so
            # the copied reference resolves at its own relative path.
            self.assertTrue(
                (domain.directory / "templates" / "schema.json").is_file()
            )

    def test_closure_is_transitive(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        vendor = root / "fixture-vendor-1.0.0"
        (vendor / "skills").mkdir(parents=True)
        (vendor / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (vendor / "README.md").write_text("r\n", encoding="utf-8")
        top = write_skill(
            vendor / "skills", "top-skill", body="[mid](../mid-skill/SKILL.md)\n"
        )
        mid = write_skill(
            vendor / "skills", "mid-skill", body="[leaf](../leaf-skill/SKILL.md)\n"
        )
        leaf = write_skill(vendor / "skills", "leaf-skill")
        write_manifest(root, [
            package_entry("top-skill", top, root, tags=["domain"]),
            package_entry("mid-skill", mid, root, tags=["workflow"]),
            package_entry("leaf-skill", leaf, root, tags=["meta"]),
        ])
        records = self.deliver(root, skills=["top-skill"])
        self.assertEqual(
            sorted(record.name for record in records),
            ["leaf-skill", "mid-skill", "top-skill"],
        )

    def test_unknown_name_fails_listing_selectable_names(self) -> None:
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(self.bundle(), skills=["no-such-skill"])
        message = str(caught.exception)
        self.assertIn("no-such-skill", message)
        # Actionable: the error names what CAN be selected instead.
        self.assertIn("domain-skill", message)
        self.assertIn("sibling-skill", message)

    def test_delegation_skill_is_refused_by_name(self) -> None:
        root = self.bundle()
        vendor = root / "fixture-vendor-1.0.0"
        delegation = write_skill(vendor / "skills", "delegate-skill")
        entries = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        entries["entries"].append(
            package_entry("delegate-skill", delegation, root, tags=["delegation"])
        )
        (root / "manifest.json").write_text(
            json.dumps(entries), encoding="utf-8"
        )
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root, skills=["delegate-skill"])
        self.assertIn("delegation", str(caught.exception))

    def test_delegation_dependency_is_refused(self) -> None:
        root = self.bundle()
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["entries"]:
            if entry["name"] == "sibling-skill":
                entry["tags"] = ["delegation"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root, skills=["domain-skill"])
        self.assertIn("delegation", str(caught.exception))

    def test_reference_to_unlisted_skill_is_not_silently_dropped(self) -> None:
        root = self.bundle()
        domain = root / "fixture-vendor-1.0.0" / "skills" / "domain-skill"
        path = domain / "SKILL.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\nGhost: [ghost](../ghost-skill/SKILL.md).\n",
            encoding="utf-8",
        )
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            self.deliver(root, skills=["domain-skill"])
        # Closure only pulls entries that exist in the manifest; anything
        # else stays a hard failure naming the dangling reference.
        self.assertIn("not part of this delivery", str(caught.exception))

    def test_repo_skill_without_checkout_fails_actionably(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.selectable_bundle(root)
        checkout = build_checkout(root / "checkout")
        write_skill(checkout / "skills" / "claude", "repo-skill")
        entries = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        entries["entries"].append(
            repo_entry("repo-skill", "skills/claude/repo-skill")
        )
        (root / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            # "forbid" simulates a published install with no checkout next to
            # the bundle — exactly where a private QA skill is unavailable.
            self.deliver(root, repo_root="forbid", skills=["repo-skill"])
        message = str(caught.exception)
        self.assertIn("repo-skill", message)
        self.assertIn("dev-tools checkout", message)
        # The remedy names the flag that requested the private skill.
        self.assertIn("--skill repo-skill", message)

    def test_repo_skill_delivers_with_commit_provenance(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.selectable_bundle(root)
        commit = "b" * 40
        checkout = build_checkout(root / "checkout", commit=commit)
        write_skill(checkout / "skills" / "claude", "repo-skill")
        entries = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        entries["entries"].append(
            repo_entry("repo-skill", "skills/claude/repo-skill")
        )
        (root / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")
        records = self.deliver(root, repo_root=checkout, skills=["repo-skill"])
        record = next(r for r in records if r.name == "repo-skill")
        # The audit record pins the checkout commit the tree was copied from.
        self.assertEqual(record.version, commit)
        self.assertEqual(
            record.sha256,
            skill_bundle.tree_sha256(checkout / "skills" / "claude" / "repo-skill"),
        )


class CheckoutCommitTests(unittest.TestCase):
    def test_linked_worktree_loose_and_packed_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            common = root / "common"
            gitdir = common / "worktrees" / "lane"
            checkout.mkdir()
            gitdir.mkdir(parents=True)
            (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
            (gitdir / "commondir").write_text("../..\n")
            (gitdir / "HEAD").write_text("ref: refs/heads/lane\n")
            ref = common / "refs/heads/lane"
            ref.parent.mkdir(parents=True)
            commit = "c" * 40
            ref.write_text(commit + "\n")
            self.assertEqual(skill_bundle._checkout_commit(checkout), commit)
            ref.unlink()
            (common / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                + commit + " refs/heads/lane\n"
            )
            self.assertEqual(skill_bundle._checkout_commit(checkout), commit)
            (common / "packed-refs").write_text("bad refs/heads/lane\n")
            self.assertIsNone(skill_bundle._checkout_commit(checkout))


class ProvenanceTests(unittest.TestCase):
    def test_provenance_mismatch_fails(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        vendor = root / "fixture-vendor-1.0.0"
        record = {
            "schema_version": 1,
            "name": "fixture-vendor",
            "version": "1.0.0",
            "tree_sha256": "0" * 64,
        }
        (vendor / "PROVENANCE.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.deliver_skills(
                Path(tempfile.mkdtemp()), bundle_root=root, repo_root="forbid"
            )
        self.assertIn("does not match its provenance record", str(caught.exception))

    def test_license_removal_breaks_provenance(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        vendor = root / "fixture-vendor-1.0.0"
        digest = hashlib.sha256()
        for path in sorted(
            [
                vendor / "LICENSE",
                vendor / "README.md",
                *(p for p in (vendor / "skills").rglob("*") if p.is_file()),
            ]
        ):
            relative = path.relative_to(vendor).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        record = {"tree_sha256": digest.hexdigest()}
        (vendor / "PROVENANCE.json").write_text(json.dumps(record), encoding="utf-8")
        (vendor / "LICENSE").unlink()
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.deliver_skills(
                Path(tempfile.mkdtemp()), bundle_root=root, repo_root="forbid"
            )
        # The missing license fails the provenance walk before any hash compare.
        self.assertIn("LICENSE", str(caught.exception))


class CatalogTests(unittest.TestCase):
    def test_catalog_lists_names_descriptions_paths(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        build_bundle(root)
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(
                Path(worktree), bundle_root=root, repo_root="forbid"
            )
            note = skill_bundle.catalog_note(records)
        self.assertIn("## Delivered worker skills", note)
        self.assertIn("**fixture-skill**", note)
        self.assertIn("fixture-skill description", note)
        self.assertIn(str(records[0].skill_md), note)
        self.assertIn("not a", note)  # evidence-boundary sentence present
        self.assertLessEqual(len(note), skill_bundle.MAX_CATALOG_CHARS)

    def test_empty_catalog_is_empty(self) -> None:
        self.assertEqual(skill_bundle.catalog_note([]), "")

    def test_catalog_budget_is_enforced(self) -> None:
        long_description = "word " * 100
        record = skill_bundle.DeliveredSkill(
            name="x-skill",
            description=long_description,
            version="1",
            sha256="0" * 64,
            license="MIT",
            origin="fixture",
            directory=Path("/tmp/x"),
            skill_md=Path("/tmp/x/SKILL.md"),
        )
        # One record stays under budget after description compaction.
        self.assertTrue(skill_bundle.catalog_note([record]))
        many = [record] * 40
        with self.assertRaises(skill_bundle.SkillBundleError) as caught:
            skill_bundle.catalog_note(many)
        self.assertIn("budget", str(caught.exception))


class RealBundleTests(unittest.TestCase):
    """Validate the checked-in bundle: pin, license, and default delivery.

    Runs identically in the source checkout and the published component (both
    ship ``skill-bundle/``). The dev-tools checkout is deliberately NOT
    assumed: default delivery must work from a published install alone
    (``repo_root='forbid'`` simulates one).
    """

    def test_manifest_loads_and_default_set_is_discipline_only(self) -> None:
        entries = skill_bundle.load_manifest()
        names = {entry.name for entry in entries}
        self.assertIn("systematic-debugging", names)
        self.assertIn("test-driven-development", names)
        self.assertIn("verification-before-completion", names)
        # Coordinator-side and delegation skills are never default-delivered.
        default = skill_bundle.select_entries(entries, skill_bundle.DEFAULT_DELIVERY_TAGS)
        self.assertNotIn("prompt-it", names)
        self.assertNotIn("side-lane", names)
        self.assertFalse(
            {"dispatching-parallel-agents", "subagent-driven-development"}
            & {entry.name for entry in default}
        )

    def test_default_delivery_materializes_and_references_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as worktree:
            records = skill_bundle.deliver_skills(Path(worktree), repo_root="forbid")
            self.assertTrue(records)
            for record in records:
                self.assertTrue(record.skill_md.is_file())
                self.assertEqual(record.version, "6.4.1")
                self.assertEqual(record.license, "MIT")
            # A vendored skill with internal relative references delivers the
            # referenced files alongside, so the links resolve in the copy.
            debugging = next(
                record for record in records if record.name == "systematic-debugging"
            )
            delivered_targets = sorted(
                path.relative_to(debugging.directory).as_posix()
                for path in debugging.directory.rglob("*")
                if path.is_file()
            )
            self.assertIn("SKILL.md", delivered_targets)
            licenses = Path(debugging.directory).parent / "licenses"
            self.assertIn(
                "superpowers-6.4.1-LICENSE",
                [path.name for path in licenses.iterdir()],
            )
            self.assertIn("MIT", (licenses / "superpowers-6.4.1-LICENSE").read_text())

    def test_no_host_home_paths_in_authored_files(self) -> None:
        # Vendored third-party skill docs may legitimately contain example
        # home paths; the files THIS repository authors must not.
        authored = [skill_bundle.BUNDLE_ROOT / "manifest.json"]
        authored += sorted(skill_bundle.BUNDLE_ROOT.glob("*/PROVENANCE.json"))
        authored += sorted(skill_bundle.BUNDLE_ROOT.glob("*.py"))
        for path in authored:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("/Users/", text, f"host home path in {path}")
            self.assertNotIn("/home/", text, f"host home path in {path}")


class RealQaSelectionTests(unittest.TestCase):
    """The real QA selection, delivered from a fresh clone of the checkout.

    Private QA skills (``qa-on-demand``, ``site-uat-sweep``) never ship in
    the published package; delivery reuses the canonical trees from a
    dev-tools checkout. This simulates exactly that at a different absolute
    path (a fresh local or cloud clone) and proves the full applicable
    instructions plus every referenced template/schema materialize inside
    the lane worktree — the "full instructions in the cloud" requirement —
    with the audit pinning the clone's commit. Skipped where no checkout
    can supply the sources (a published install), which stays usable for
    default deliveries.
    """

    def setUp(self) -> None:
        checkout = skill_bundle.resolve_repo_root()
        if checkout is None:
            self.skipTest("no dev-tools checkout next to the bundle")

    def test_qa_on_demand_selection_closures_and_materializes(self) -> None:
        checkout = skill_bundle.resolve_repo_root()
        self.assertIsNotNone(checkout)
        with tempfile.TemporaryDirectory() as tmp:
            # A fresh clone shape: markers, the plugin package with the
            # bundle, and the canonical private skill trees. Different
            # absolute path than the original checkout, no host paths used.
            clone = Path(tmp) / "cloud-clone"
            (clone / "bin").mkdir(parents=True)
            (clone / "bin" / "side-lane").write_text("#!clone\n", encoding="utf-8")
            git = clone / ".git" / "refs" / "heads"
            git.mkdir(parents=True)
            (clone / ".git" / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8"
            )
            (git / "main").write_text("c" * 40 + "\n", encoding="utf-8")
            plugin = (
                clone / "public" / "governed-side-lane"
                / "plugins" / "governed-side-lane"
            )
            plugin.mkdir(parents=True)
            shutil.copytree(skill_bundle.BUNDLE_ROOT, plugin / "skill-bundle")
            for name in ("qa-on-demand", "site-uat-sweep"):
                shutil.copytree(
                    checkout / "skills" / "claude" / name,
                    clone / "skills" / "claude" / name,
                )
            with tempfile.TemporaryDirectory() as worktree:
                records = skill_bundle.deliver_skills(
                    Path(worktree),
                    bundle_root=plugin / "skill-bundle",
                    skills=["qa-on-demand"],
                )
                names = {record.name for record in records}
                # Additive defaults plus the closure: qa-on-demand layers on
                # site-uat-sweep, so selecting it pulls the sibling in.
                self.assertTrue(
                    {"qa-on-demand", "site-uat-sweep"} <= names,
                    f"QA closure missing skills: {sorted(names)}",
                )
                self.assertTrue(
                    {"receiving-code-review", "requesting-code-review",
                     "systematic-debugging", "test-driven-development",
                     "verification-before-completion"} <= names
                )
                by_name = {record.name: record for record in records}
                qa = by_name["qa-on-demand"]
                sweep = by_name["site-uat-sweep"]
                # Full instructions plus referenced templates/schema at
                # materialized paths inside the delivery (cloud-readable).
                for relative in (
                    "SKILL.md",
                    "templates/scenarios.schema.json",
                    "templates/application-adapters.json",
                    "templates/resolve-application-adapter.py",
                    "templates/readiness.sh",
                    "templates/estimates.md",
                    "templates/pm-setup.md",
                    "templates/browsers.md",
                    "templates/report.md",
                ):
                    self.assertTrue(
                        (qa.directory / relative).is_file(),
                        f"qa-on-demand did not materialize {relative}",
                    )
                for relative in (
                    "SKILL.md",
                    "templates/PROMPT-site-uat-sweep.md",
                    "templates/asana-card-body.md",
                    "templates/findings-schema.json",
                    "templates/status.sh",
                ):
                    self.assertTrue(
                        (sweep.directory / relative).is_file(),
                        f"site-uat-sweep did not materialize {relative}",
                    )
                # Audit provenance: the delivered trees hash-match the
                # canonical sources, and repo-sourced records pin the
                # clone's commit.
                for record in (qa, sweep):
                    source = clone / "skills" / "claude" / record.name
                    self.assertEqual(
                        record.sha256, skill_bundle.tree_sha256(source)
                    )
                    self.assertEqual(record.version, "c" * 40)
                # The catalog stays a bounded, progressive description set —
                # never a full prompt dump of the skill bodies.
                note = skill_bundle.catalog_note(records)
                self.assertIn("**qa-on-demand**", note)
                self.assertIn("**site-uat-sweep**", note)
                self.assertLessEqual(len(note), skill_bundle.MAX_CATALOG_CHARS)
                self.assertNotIn("# QA on demand", note)


if __name__ == "__main__":
    unittest.main()
