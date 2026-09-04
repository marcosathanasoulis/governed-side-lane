# Changelog

## 0.2.4 - 2026-09-03

- The sign-in hint (`refresh_command` in `auth-status`, and the OAuth preflight
  error) is now built from the resolved host executable. When the CLI was found
  only through an override or a desktop-app bundle, a bare `codex login` could
  not run in the very environment that needed the resolution; the hint now
  quotes the resolved path instead. Bare `codex login` / `claude auth login`
  are kept whenever the resolved path is the one `PATH` yields.

## 0.2.3 - 2026-09-03

- Resolve the native host executable explicitly instead of assuming a bare
  `codex` / `claude` on `PATH`. Lookup order is an explicit
  `SIDE_LANE_CODEX_EXECUTABLE` / `SIDE_LANE_CLAUDE_EXECUTABLE` override (which
  fails closed if set but unusable), then `PATH`, then — for Codex only — the
  CLI shipped inside the Codex / ChatGPT desktop app bundle on macOS. The
  resolved path is reported as `runtime` in `check-capabilities`, used for
  `auth-status` and the OAuth preflight, and appears as `argv[0]` in the run
  summary. A missing executable now fails before a lane worktree is created,
  with an actionable message. Fixes "codex not found" from non-interactive
  agent shells on machines where only the desktop app is installed.

## 0.2.2 - 2026-09-01

- Persist review findings durably: the per-run audit record
  (`<git-dir>/side-lane-runs/<branch>.json`, now schema_version 2) captures
  the lane's stdout and stderr before the disposable review worktree is
  removed, and the run summary returns it as `result_artifact`. A failed
  artifact write aborts the run without disposing the worktree.

## 0.2.1 - 2026-09-01

- Make the AGENTS.md linkage governance error actionable and stop treating
  repeated authoritative links to root `CLAUDE.md` as ambiguity.
- Ship `config/allowed_signers` and verify release tags against it, so
  SSH-signed release tags actually verify on installed hosts (`update.py`
  previously reported "no signed release tags available" for every release,
  including the unsigned v0.2.0).

## 0.2.0 - 2026-08-30

- Establish the public package as the canonical core consumed by downstream
  organization overlays.
- Allow an explicit `SIDE_LANE_MODELS_PATH` configuration overlay without
  modifying the public runtime or default allowlist.

## 0.1.0 - 2026-08-30

- Initial public-package staging for native Codex and Claude review/execute
  lanes.
- Dedicated worktrees for every lane.
- Canonical cross-host governance injection.
- Optional explicit direct-Z.AI GLM execute route.
- Claude and Codex plugin/marketplace manifests.
