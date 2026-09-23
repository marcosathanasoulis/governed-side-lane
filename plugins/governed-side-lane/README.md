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

Native Devin 3000.10.31 was observed to prompt for a relative `../../..`
output redirect with a nested `exec.workdir`, while accepting an equivalent
absolute in-lane target. The native matcher's internal behavior is unverified.
The adapter therefore adds shared guidance for every native Devin shell lane:
use an absolute path under the lane's `.side-lane-scratch/` for temporary output
and logs. Paths containing spaces are quoted in the generated example. This
changes prompt guidance only; file containment and permission rules stay intact.

## Report-deliverable execute lanes (`--report-deliverable`, `--report-only`)

A worker can end its turn with exit 0, having navigated and saved its
screenshots, and describe in prose a findings report it never wrote. Prose in
a transcript is not an artifact, so `--report-deliverable` makes
`SIDE_LANE_REPORT.md` at the lane worktree root a checked precondition and
judges the lane on that report instead of on implementation delivery:

```bash
side-lane run --host claude --mode execute --provider <p> --model <m> \
  --lane-name <lane> --prompt-file <task> --report-deliverable \
  --capability shell
```

It is execute mode only and otherwise host-neutral: the verdict is a statement
about the lane tree, so it is available on every host and every route,
including the provider-key routes that carry no `max_budget_usd`.
`--report-only` implies `--report-deliverable` and adds the Claude-only
in-loop repair to the same verdict; it is deliberately narrow — Claude host,
execute mode, and a finite positive `max_budget_usd` on the route (the USD cap
and the repair must travel in the same command; no catalog or global budget
change is involved). Only `--report-only` carries a spend gate: it is what the
Stop hook buys, and `--report-deliverable` alone neither requires a cap nor
arms a hook. The `max_budget_usd`
value is a client-side estimate guard, not proof that the upstream server or
account enforces the same cap; verify provider-side and account limits separately.
Add `shell` or `workspace-write` capabilities when the report generation/read
tool needs to write artifacts. Ordinary execute and review lanes are untouched,
and either flag is rejected before a worktree, credential, or host executable is
touched anywhere else. A report run refuses both `--capability git-push` and
`--capability workflow-write` at the CLI and direct adapter boundaries: it
neither publishes a branch nor makes workflow or messaging updates. Report
artifact writes still use `workspace-write`, and authorized read capabilities
remain available.

The override lives in the canonical governance document rather than in a
host-specific patch, so every host is handed it: a report lane renders the
`Report deliverable` section alongside its mode rules, with the ordinary
execute commit/push and workflow/messaging-write grants removed. That section
instructs the worker to change
no source and make no git change — governing the lane's own commit and push
decisions where a conflicting repository commit convention would otherwise
require them, and claiming no precedence over a higher-priority security or
system instruction or over the instruction ordering a host decides for itself —
to write `SIDE_LANE_REPORT.md` early and revise that same file
incrementally instead of leaving a second draft at the lane root, and to keep
throwaway output in the git-excluded, untracked `.side-lane-scratch/` tree.
That section also carries the canonical declaration of the two capabilities a
report run refuses (`Report forbidden write capabilities:`), which the runner
reads from the document instead of from a code-side copy.
Enforcement differs by host and is documented as such rather than assumed:
Claude and Devin also receive the canonical `report-deliverable (denied)`
git-write rules, which are an approval-boundary seam over command strings — a
`git -C <path>` invocation, reordered or bundled options, a compound command,
and an allowed interpreter are not fully covered by them — while the Codex host
carries the instruction with no deny seam at all, so its contract is instruction
plus after-the-fact verification and never prevention. On every host the runner
refuses, after the fact, a report lane that committed or left unexpected files,
and that check is the enforcement.

**A report-only lane is judged on its report, not on implementation delivery.**
Its worker is told by that contract to change no source and make no git change,
so the execute
rule — a commit plus a clean tree — would reject the very outcome this flag
exists to accept, and `--allow-no-commit` could not repair it: the report file
is itself one of the uncommitted paths that flag's condition excludes. The
acceptance is therefore the report artifact plus the absence of source work:

- the report at the fixed path must be this run's own (`current`, below);
- the lane must hold **no commit** — a report-only lane that committed is
  refused, and is never published automatically, with or without
  `--no-publish` (a report is not a source deliverable and its branch has
  nothing to make remote-contained);
- the only lane files it may leave changed are the report itself — untracked
  where the repository does not track `SIDE_LANE_REPORT.md`, modified where it
  does — and **untracked** files under the lane's git-excluded
  `.side-lane-scratch/` directory, the documented home for throwaway output and
  screenshots. The exemption is read off real `git status` output, per path,
  not off the path prefix: a *tracked* path under scratch that was modified,
  staged, added, deleted, or renamed is source work and is refused like any
  other. Any other path is implementation work too: the run exits `3`, names
  the paths, and points at the scratch directory instead of accepting them
  silently, because nothing uncommitted outside the report survives the
  worktree. A path whose status cannot be read fails closed rather than being
  exempted on its name;
- **only when this run also granted `--capability playwright`**, the third
  allowed home is a browser-report artifact at the lane root: an **untracked**,
  regular, non-symlink file whose name matches
  `SIDE_LANE_REPORT-<name>.<ext>` exactly — `<name>` from `[A-Za-z0-9_-]+` and
  `<ext>` one of `png jpg jpeg webp yaml yml json txt md` — bounded to 40 files,
  20 MiB each, and 100 MiB in total (files are counted in sorted name order, so
  which ones fall outside a cap is deterministic). This is the same name rule
  and the same caps the cloud worker's own artifact collector applies, pinned on
  both sides by `tests/fixtures/report_artifact_names.json`, so the two halves
  cannot disagree about what an allowed artifact is. Without that capability
  there is **no** root-level exemption at all, and a file at such a name is
  refused like any other; a *tracked* path at such a name — modified, staged,
  added, deleted, renamed, or copied — is source work whatever it is called;
- the coordinator checkout is still compared against its pre-dispatch
  baseline (exit `6`), an unreadable lane tree still fails closed (exit `4`),
  and a non-zero worker exit is still retained. `--verify` runs only when the
  run has not already failed — never after a non-zero worker exit, a
  coordinator-checkout change, or a refused report — and never implies a
  commit;
- a verification may not change what the run judged. The runner re-reads the
  lane, the report identity, the accepted browser artifacts (re-decided from
  the fresh tree, and compared by full-content identity as well as by path —
  the report's own identity stays the sampled one it has always been), and the
  coordinator checkout after `--verify`
  runs; a verification command that commits on the lane branch, leaves or
  removes a lane file, rewrites the report or an accepted browser artifact,
  or writes into the
  coordinator checkout refuses the lane (exit `3`, with the checkout case also
  recorded as a source mutation). An accepted artifact whose content cannot be
  read as a plain file — a link, a FIFO, or anything else the safe open
  refuses — is refused the same way and never treated as unchanged. Nothing is
  reverted or deleted to hide it — the change is reported and the lane is
  refused.

The verdict above is the whole run's, not the report artifact's alone: for a
report lane, `summary["delivered"]` is true only when the report verdict
holds **and** the worker exited 0 **and** the source check found nothing **and**
the checkout is unchanged **and** any `--verify` both passed and changed
nothing. Exit codes alone do not carry that — a consuming model qualification
step reads `summary["delivered"]` — so the flag is false on every one of those
failures.

`--allow-no-commit` and `--no-publish` are accepted and harmless here, but a
report-only lane no longer needs either to be accepted or to avoid a push.

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
one, is unaffected. `delivered` folds that report verdict together with the rest
of the run for a report-only lane — with `report_only_committed`,
`report_only_unexpected_paths`, and `report_only_verification_changes` recording
the ways a lane can still be refused — rather than following the execute lane's
`committed`/`uncommitted` pair, which continues to describe the lane tree
exactly as it did.

Scope of the claim: the hook is a quality gate on the lane's own artifact, not
a sandbox — the same-user harness is not an OS boundary, and the run's existing
single subprocess timeout and USD cap are unchanged. The outer GCF consumer's
own report collection remains authoritative for source changes, containment,
sizes, and scrubbing; until that consumer is wired to this flag, a report-only
lane's report must also satisfy it.

## Task no-external-publication lanes (`--no-external-publication`)

`--no-publish` is a decision about the **runner's** own push: it skips the
automatic push of a delivered branch and records `published: null`. It never
forbade the **worker** to push the branch itself, and a run that carried it was
recorded in a way a reader could take as "nothing was published".
`--no-external-publication` is the task-level authority, and it also suppresses
the runner's own push of a delivered branch:

```bash
side-lane run --host claude --mode execute --provider <p> --model <m> \
  --lane-name <lane> --prompt-file <task> --no-external-publication
```

It is execute mode only, rejected before a worktree, credential, or host
executable is touched. A lane carrying it refuses `--capability git-push` at the
CLI and at every direct adapter boundary — the guard denies the `git push`
command family, so that grant could only render an allow rule the same run
denies. Nothing else is refused: `workflow-write` stays a separate authority an
approved task may name, and every read capability and `workspace-write` are
untouched. The two options are not interchangeable: `--no-publish` alone is a
runner-only decision and says nothing about the worker, while
`--no-external-publication` also suppresses the runner's own push of a delivered
branch — so a caller does not have to remember `--no-publish` as well, and a
lane selected with the guard alone never has its branch pushed by the runner.
Ordinary execute lanes are unchanged.

`--verify` is refused in a lane carrying this guard, and refused before the
worktree, the credential, the host process, or the command exists. The caller's
command is arbitrary shell that runs in the lane before the runner reaches its
own publication decision, so `--verify "git push origin HEAD"` would publish
first and be *recorded* as a skipped push afterwards — the run's own record
claiming the opposite of what happened. No command-string filter makes an
arbitrary command safe to run under this authority, so the combination is
refused rather than pretended to be contained; this is an argument check, not
general arbitrary-process containment, which the flag never promised. Ordinary
verification is unaffected: `--verify` keeps its meaning on every other execute
lane, including one carrying `--no-publish`, which is the runner's own push
decision and never forbade the worker to publish.

The refusal is a section of the canonical governance document, rendered for
every host, including a host where nothing in it can be enforced. Enforcement
differs by host and is documented as such rather than assumed: Claude receives
the canonical `no-external-publication (denied)` rules as `--disallowedTools`
denials and Devin receives them in its native permission deny list plus its
`PreToolUse` command policy (which normalises `git -C <path>`), while the Codex
host carries the instruction only — a `danger-full-access` execute lane has no
deny seam, so its refusal is never prevention. Those rules are an
approval-boundary seam over command strings, not a sandbox: a `git -C <path>`
invocation, reordered or bundled options, a compound command, and an allowed
interpreter are not fully covered by them. The refused capability names come
from the document's own machine-readable declaration
(`Publication refusal never grants these capabilities:`), which the runner
reads instead of a code-side list; a missing, malformed, reworded, or duplicated
declaration stops the run.

The run's summary carries a `publication` record beside the existing `published`
outcome — `task_authority` (the flag, or `null`), `host_enforcement` (what that
host's own controls could do with it, or `null`), `runner` and
`runner_skip_reason` (what this runner itself did: `published`, `failed`, or
`skipped` with the reason for the skip, and `not-delivered` when no branch was
delivered to push), and `worker_publication_verified`, which is always
`not-checked`. An execute lane's audit file carries the same record without the
two runner fields. It is written after the worker returns and before the runner
decides its own push, so it carries nothing about that push, and an adapter that
fails leaves no audit at all. The audit states the authority the run was given,
what the host could enforce, and that the worker's own publication is
unverified. Neither artifact observes the worker — no code here checks whether
the worker published — so no field is evidence that no publication happened:
`not-checked` is the honest answer for the worker, and the runner's own
`skipped` describes this runner's push alone.

