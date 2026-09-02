from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SIGNERS = ROOT / "config" / "allowed_signers"
CANONICAL = "https://github.com/marcosathanasoulis/governed-side-lane"
ALLOWED_REMOTES = {
    CANONICAL,
    CANONICAL + ".git",
    "git@github.com:marcosathanasoulis/governed-side-lane.git",
}
TAG = re.compile(r"^v\d+\.\d+\.\d+$")


class UpdateError(Exception):
    pass


def git(args: Sequence[str], runner: Callable[..., object] = subprocess.run) -> str:
    result = runner(
        ["git", "-C", str(ROOT), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if result.returncode:
        raise UpdateError((result.stderr or result.stdout).strip() or "git failed")
    return result.stdout.strip()


def preflight(runner: Callable[..., object] = subprocess.run) -> str:
    origin = git(["remote", "get-url", "origin"], runner)
    if origin not in ALLOWED_REMOTES:
        raise UpdateError(f"unexpected origin; expected {CANONICAL}")
    if git(["status", "--porcelain"], runner):
        raise UpdateError("refusing to update a dirty checkout")
    branch = git(["branch", "--show-current"], runner)
    if not branch:
        raise UpdateError("refusing to update detached HEAD")
    return branch


def available(runner: Callable[..., object] = subprocess.run) -> list[str]:
    preflight(runner)
    git(["fetch", "--tags", "--prune", "origin"], runner)
    candidates = [value for value in git(["tag", "--list", "v*", "--sort=-v:refname"], runner).splitlines() if TAG.fullmatch(value)]
    verified: list[str] = []
    for tag in candidates:
        try:
            verify_tag(tag, runner)
        except UpdateError:
            continue
        verified.append(tag)
    return verified


def verify_tag(tag: str, runner: Callable[..., object] = subprocess.run) -> None:
    signer_config = (
        ["-c", f"gpg.ssh.allowedSignersFile={ALLOWED_SIGNERS}"]
        if ALLOWED_SIGNERS.is_file()
        else []
    )
    git([*signer_config, "verify-tag", tag], runner)


def apply(tag: str, runner: Callable[..., object] = subprocess.run) -> None:
    if not TAG.fullmatch(tag):
        raise UpdateError("release must be an explicit semantic version tag")
    preflight(runner)
    git(["fetch", "--tags", "--prune", "origin"], runner)
    verify_tag(tag, runner)
    git(["merge-base", "--is-ancestor", "HEAD", tag], runner)
    git(["merge", "--ff-only", tag], runner)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("tag", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.mode == "check":
            if args.tag:
                raise UpdateError("check does not accept a tag")
            tags = available()
            print(tags[0] if tags else "no signed release tags available")
        else:
            if not args.tag:
                raise UpdateError("apply requires an explicit release tag")
            apply(args.tag)
            print("updated; rerun install/check and restart the host if required")
        return 0
    except UpdateError as exc:
        print(f"update: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
