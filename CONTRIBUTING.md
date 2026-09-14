# Contributing

Contributions are welcome through reviewed pull requests.

1. Open an issue for changes to trust boundaries, supported providers,
   authentication, permissions, or marketplace packaging.
2. Work on a dedicated branch and worktree.
3. Keep runtime code Python-standard-library-only unless a dependency proposal
   is approved first.
4. Add meaningful mocked tests. Never use a real credential or paid model call
   in tests.
5. Run the full development and public-package validation commands from the
   README.
6. Do not merge your own pull request when another maintainer is available.

## How changes land

Public pull requests are reviewed and, when accepted, ported by hand into the
private source of truth. The public `main` branch is a publish target and does
not accept direct merges. Changes appear in the next published release.

By contributing, you agree that your contribution is licensed under
Apache-2.0.
