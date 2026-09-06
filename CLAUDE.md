# Repository guidance

Preserve the public package and its documented authorization boundaries. Keep user-specific paths, private source material, and credentials out of this repository.

Before changing shared files, inspect recent commits, open pull requests and active worktrees for overlap. Follow explicit user instructions about the working checkout.

Validate changes with `python3 scripts/validate_public_package.py` and relevant package tests. Model execution and real credential retrieval must not be part of automated validation. Keep changes on a reviewed branch; do not merge your own pull request.
