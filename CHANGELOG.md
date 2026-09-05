# Changelog

## 0.3.0 - 2026-09-04

- Execute-mode Claude-host lanes (native Claude and GLM) now receive explicit
  `--allowedTools` rules derived from `--capability` flags: always
  Read/Edit/Write/Glob/Grep; with `shell` or `workspace-write` (or
  `git-push`) the ordinary dev-command set `SHELL_TOOLS`; `git-push`
  additionally allows `Bash(git push *)` and passes `--disallowedTools`
  denying force pushes. Review mode argv is unchanged (no allowlist ever).
  Unknown capability names fail closed. The run summary JSON now includes
  `capabilities`, `allowed_tools`, `disallowed_tools`.
- `.side-lanes/` is added to the coordinator repo's `.git/info/exclude`
  before the dirty-checkout check, so earlier lane worktrees no longer block
  the next launch.
- Codex host: `hosts.host_support_dir` resolves the real directory of the
  `codex` executable; if `codex-code-mode-host` sits beside it (codex-cli
  0.152+ from the desktop app bundle), that directory is prepended to the
  lane child's PATH. `check-capabilities` reports it as `host_support_dir`.
- AGENTS.md linkage now also accepts a "source of truth" line with a
  Markdown link to root CLAUDE.md, in addition to the existing "You **must**
  read [CLAUDE.md](./CLAUDE.md)" form. Links on such lines may only point at
  root CLAUDE.md.
- New capability `playwright` in config/models.json; `check-capabilities`
  reports it present when an MCP server name containing "playwright" is
  configured for the host. Review mode still hides all MCP servers via
  `--strict-mcp-config`; execute mode inherits the user's/project's MCP
  servers, so Playwright is usable in execute lanes.

## 0.2.5 - 2026-09-03

- Sign-in hints for an off-`PATH` executable are quoted for the platform's
  default shell: PowerShell gets the call-operator form (`& 'C:\\...\\codex.exe'
  login`), POSIX shells keep `shlex` quoting. `refresh_command` looks up
  `which`/platform at call time so they can be substituted in tests.

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
