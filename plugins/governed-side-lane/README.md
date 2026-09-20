# Governed Side Lane plugin

This self-contained plugin provides the `side-lane` and optional
`prompt-it-side-lane-routing` skills plus their standard-library Python runner
and canonical governance/configuration.

The core skill works without Prompt it or organization-specific configuration.
It requires Git, Python 3.10+, and at least one signed-in native host CLI.

Optional model/account and connector setup guidance lives in the public
[model guide](docs/model-guide.md) and
[connector guide](docs/connector-guide.md). Neither is required to use a
single native OpenAI or Claude host.

## Provider credentials

macOS uses Keychain and Windows uses Credential Manager by default. Linux uses
an environment/file backend; other platforms can select it explicitly with
`SIDE_LANE_CREDENTIAL_BACKEND=env`. For a configured service name `S`, it reads
`SIDE_LANE_CREDENTIAL_<S>` first, upper-casing the name and replacing each
non-alphanumeric character with `_`. It otherwise reads the file
`$SIDE_LANE_CREDENTIALS_DIR/<S>`. Populate only the provider needed for a run
from the container's secret manager. Service names cannot contain path
separators or escape the configured directory.

`side-lane credentials` reports presence only. A provider credential is read
only after the run receives `--approve-billable-route`; credential values are
not printed or written to audit records.

Every target repository must contain a regular root `AGENTS.md` that requires
and authoritatively links one regular root `CLAUDE.md` (or states in a
Markdown-linked line that `CLAUDE.md` is the source of truth). Review and
execute runs always use dedicated worktrees; the coordinator repo's
`.side-lanes/` lane worktrees are auto-excluded from its own `git status`.
Execute-mode Claude-host lanes get a capability-derived `--allowedTools`
allowlist (file tools always; dev-shell commands with `shell`/
`workspace-write`; `git push` only with `git-push`) and can see the host's
configured MCP servers, including Playwright when present. A capability grant
also approves exactly its matching project `.mcp.json` server for that one
process — `playwright`, `gitnexus`, `codegraph`, and `slack` (for
`slack-read`) — never an unrequested server and never all project servers;
inherited user- and server-scope MCP grants are unaffected. Workers are
instructed to wait (`WaitForMcpServers`) for a granted server that is still
loading before declaring the capability missing; only Playwright additionally
requires the pre-launch readiness probe. Review mode never gets an allowlist
and always hides MCP servers. See the public repository
README for install, usage, security, and contribution details.

Licensed under Apache-2.0.

Worktree placement: lane worktrees default to `<repo>/.side-lanes/worktrees`,
kept out of `git status` by an entry in `.git/info/exclude`. Repositories that
forbid nested checkouts pass `--worktree-root ../lanes` (relative to the
repository root) or set `SIDE_LANE_WORKTREE_ROOT`; other in-repository
locations are refused.

## Scratch files and the Devin policy hook

Every lane worktree is prepared with a `.side-lane-scratch/` directory at its
root for throwaway scripts, notes, and intermediate output. The directory is
git-excluded (through the same shared `.git/info/exclude`, so `git status` and
`git add -A` never see it) and is not removed per-run: review lanes are
disposed with their whole worktree, and an execute lane's worktree — scratch
included — stays put for the coordinator's review. Lane runs never write
scratch files anywhere outside the lane worktree.

Native Devin execute lanes run a CLI PreToolUse policy hook that blocks
Devin's own `write`, `edit`, and `str_replace` file tools from writing outside
the lane worktree, and pattern-checks `exec` commands against the canonical
`Bash(...)` grants (a leading `git -C <lane-worktree> ...` is matched as the
bare git command, so `git -C <worktree> log --oneline -5` passes
`Bash(git log *)`; any other `-C` target must match a rule literally). As with
any allowlisted-command check, an interpreter the hook lets `exec` run (a
shell, an interpreter invoked directly, etc.) can still write anywhere the
user's own account can reach — the hook does not sandbox what an allowed
command does once it runs. A hook block lets the model continue; without it
the write would reach Devin's `accept-edits` confirmation, which ends a
non-interactive session outright. The lane worktree path travels to the
hook in the per-run `devin-command-policy.json` rules file.

## Report-only execute lanes (`--report-only`)

A worker can end its turn with exit 0, having navigated and saved its
screenshots, and describe in prose a findings report it never wrote. Prose in
a transcript is not an artifact, so `--report-only` makes
`SIDE_LANE_REPORT.md` at the lane worktree root a checked precondition:

```bash
side-lane run --host claude --mode execute --provider <p> --model <m> \
  --lane-name <lane> --prompt-file <task> --report-only
```

It is deliberately narrow: Claude host, execute mode, and a finite positive
`max_budget_usd` on the route (the USD cap and the repair must travel in the
same command; no catalog or global budget change is involved). Ordinary execute
and review lanes are untouched, and the flag is rejected before a worktree,
credential, or host executable is touched anywhere else.

Two checks enforce it. Inside the same invocation, a Claude Code `Stop`
command hook — this package's own stdlib helper, invoked through the run's
process-local `--settings` payload with a shell-quoted absolute path — reads
the fixed report path from its own argument, never from hook stdin, and blocks
one stop when the report is missing, empty, whitespace-only, a symlink, or not
a regular file. The hook's stdin event JSON carries `hook_event_name`, `cwd`,
and `stop_hook_active` alongside fields such as `session_id`,
`transcript_path`, `permission_mode`, and `last_assistant_message`, the last
of which can be large; the hook reads a bounded amount and ignores unknown
fields. The block hands the same model loop a reason to write the real
report; `stop_hook_active` then lets the next stop through, bounding the
feedback to one round. No saved settings file, user hook, permission, or
inherited hook is written, replaced, or disabled. After the worker exits, the
runner applies the same rule as its own acceptance gate and exits
`3` (`LANE_NOT_DELIVERED`) if the report is still unusable, so completion prose
can never be recorded as an accepted delivery.

Scope of the claim: the hook is a quality gate on the lane's own artifact, not
a sandbox — the same-user harness is not an OS boundary, and the run's existing
single subprocess timeout and USD cap are unchanged. The outer GCF consumer's
own report collection remains authoritative for source changes, containment,
sizes, and scrubbing; until that consumer is wired to this flag, a report-only
lane's report must also satisfy it.

