# Governed Side Lane plugin

This self-contained plugin provides the `side-lane` and optional
`prompt-it-side-lane-routing` skills plus their standard-library Python runner
and canonical governance/configuration.

The core skill works without Prompt it or organization-specific configuration.
It requires Git, Python 3.10+, and at least one signed-in native host CLI.

Optional model/account and connector setup guidance lives in the public
[model guide](docs/model-guide.md),
[connector guide](docs/connector-guide.md), and
[OmniRoute add-on guide](docs/omniroute-guide.md). None of these is required to
use a single native OpenAI or Claude host, and OmniRoute is optional even for
pooled routing.

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
  --lane-name <lane> --prompt-file <task> --report-only \
  --allow-no-commit --no-publish \
  --capability shell
```

It is deliberately narrow: Claude host, execute mode, and a finite positive
`max_budget_usd` on the route (the USD cap and the repair must travel in the
same command; no catalog or global budget change is involved). The `max_budget_usd`
value is a client-side estimate guard, not proof that the upstream server or
account enforces the same cap; verify provider-side and account limits separately.
Add `shell` or `workspace-write` capabilities when the report generation/read
tool needs to write artifacts, and use `--allow-no-commit` and `--no-publish` so
a report-only outcome is not treated as a source commit or push. Ordinary execute
and review lanes are untouched, and the flag is rejected before a worktree,
credential, or host executable is touched anywhere else.

Two checks enforce one run-bound freshness rule on one path. `SIDE_LANE_REPORT.md`
counts only when it differs from what the runner recorded at that path before
the worker started. A lane worktree is added from HEAD, so a repository that
tracks `SIDE_LANE_REPORT.md` hands every new lane a complete-looking report no
worker wrote; path, non-emptiness, and a recent mtime cannot tell that inherited
file from a real delivery, and an unrelated commit or an empty session would
otherwise be enough to accept the lane. So, before the worker starts, the runner
records the SHA-256 of a bounded prefix plus the full size of whatever is at the
fixed path (`None` when nothing is there), copies a preexisting report into the
lane's git-excluded `.side-lane-scratch/report-inherited/` so replacing it
erases no history, and refuses to launch at all when the path is a symlink,
FIFO, socket, device, or directory — nothing is ever followed, opened, moved, or
deleted to make a lane start. One recorded object drives both checks: it travels
with the path in the run's own `--settings` payload, which is process argv fixed
by the parent rather than a file inside the worker's writable tree, so the hook
and the acceptance gate cannot drift onto different rules.

Inside the same invocation, a Claude Code `Stop` command hook — this package's
own stdlib helper, invoked through that `--settings` payload with a shell-quoted
absolute path — blocks one stop when the report is missing, empty,
whitespace-only, a symlink, not a regular file, or unchanged from what the lane
started with. It takes the path and the run baseline from its own argument,
never from hook stdin, and reads the event JSON (`hook_event_name`, `cwd`,
`stop_hook_active` alongside fields such as `session_id`, `transcript_path`,
`permission_mode`, and `last_assistant_message`, the last of which can be large)
under a bounded read, ignoring unknown fields. The block hands the same model
loop a reason to write the real report; `stop_hook_active` then lets the next
stop through, bounding the feedback to one round. An event it cannot trust
(malformed settings, no run baseline, oversized or unparsable stdin) is allowed
with a diagnostic — a hook must not wedge a worker — because the runner's
post-run acceptance is authoritative and fails closed without a baseline. No
saved settings file, user hook, permission, or inherited hook is written,
replaced, or disabled. After the worker exits, the runner applies the same rule
and exits `3` (`LANE_NOT_DELIVERED`) if the report is not this run's own, and the
summary names the reason (`report_state`: `current`, `stale`, `unusable`, or
`unverified`) alongside `report_preexisting` and any `report_preserved` copy, so
completion prose can never be recorded as an accepted delivery. A run that
rewrites the inherited file, or writes any report when the lane started without
one, is unaffected.

Scope of the claim: the hook is a quality gate on the lane's own artifact, not
a sandbox — the same-user harness is not an OS boundary, and the run's existing
single subprocess timeout and USD cap are unchanged. The outer GCF consumer's
own report collection remains authoritative for source changes, containment,
sizes, and scrubbing; until that consumer is wired to this flag, a report-only
lane's report must also satisfy it.

