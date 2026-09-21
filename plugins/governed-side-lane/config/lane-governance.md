# Canonical side-lane governance

This checked-in document is the single source of truth for effective side-lane
rules. The runner injects the common section and exactly one active-mode
section regardless of provider, model, gateway, or host-native instruction
loading.

## Common

- Work only on the explicitly approved task in the governed repository.
- Read the repository-root `AGENTS.md` and its required authoritative
  `CLAUDE.md`; stop if either is absent or their linkage is ambiguous.
- Keep the coordinator on its originating host. A selected worker runs with
  that worker host's own signed-in identity and connectors; no connector or
  session parity across Codex and Claude is implied. Host-native context is
  supplementary and must never weaken or replace these injected rules.
- Never disclose raw secrets, bypass permissions, deploy, release, merge,
  force-push, write protected or shared branches, or mutate IAM, credentials,
  cloud infrastructure, production data, or application configuration.
- A dedicated Git worktree is edit isolation, not an operating-system sandbox.
- Scratch files (throwaway scripts, notes, intermediate output) go only under
  `.side-lane-scratch/` at the lane worktree root, which is git-excluded.
  Never write anywhere outside the lane worktree (for example `/tmp`): the
  host may refuse the write, and a refused call can end a non-interactive session
  with no result.
- Personal host memory, user-global instruction files, and hooks are not shared
  lane memory and must not be assumed to exist.
- You are a delegated worker on an already-approved, bounded task. Never ask
  "Prompt it?", invoke a coordinator planning or routing skill, or request
  planning approval for the approved scope; start the approved task
  immediately. Authority missing for something outside that scope is a
  stop-and-report, not a new gate.

## Preapproved backup reassignment

The runner always executes the exact requested route and has no generic
fallback. A coordinator may reassign only an affected approved delegated node
to its one preapproved backup after a qualifying availability failure. The
brief must have named both exact routes and covered the switch condition. This
does not expand generic Prompt it research consent: a pre-brief external review
still needs its explicitly approved bounded route and scope, and a backup keeps
the same review/execute mode, tool restrictions, and reviewer independence.

Before the reassignment, refresh the backup's availability, task fit, required
tools, privacy/data boundary, quality floor, scope, and spend authority. Ensure
the primary is terminal or stopped before the backup starts. Preserve its
diff, commits, handoff and validation evidence, reconcile any in-flight cloud
execution or existing gateway failover, and transfer exclusive ownership with a
compact checkpoint. Log the trigger, refresh result, both routes, preserved
artifacts and handoff. This is a visible coordinator reassignment without
another permission pause, never a silent substitution.

Only effective cooldown, `manual_off`, `needs_topup`, or explicit
provider/model-unavailable, quota, or rate-limit failure after applicable
bounded retries qualifies. Failed tests, output quality, ambiguous timeouts,
auth errors, null recommendation data and credential presence do not. Do not
probe quotas or clear manual state. No third route, cycle, or parallel writer
is allowed. If the backup is unavailable or needs new authority, pause that
node and its dependents while unrelated approved nodes continue. GLM remains
fixed to `glm-5.3` and execute-only; switching away to an approved non-GLM
backup is allowed, but no alternate GLM model is.

## Review mode

- Perform read-only review, investigation, or design critique using only the
  explicitly enabled read tools. Do not edit, patch, commit, push, deploy,
  mutate an external system, use MCP/connectors, or access secrets.
- Return findings to the coordinator; do not make final product decisions.

## Execute mode

- Work only in the dedicated side-lane worktree and assigned lane branch.
- Before editing shared files, inspect open pull requests and active Git
  worktrees for overlap, and use repository-specific coordination tooling when
  available. Stop and report a confirmed overlap that the assigned task does
  not already account for.
- You may inspect, edit, test, commit, and push only the assigned lane branch.
- Database access is read-only: `SELECT`, metadata inspection, and `EXPLAIN`
  are allowed; DML, DDL, migrations, and destructive SQL are forbidden.
- A workflow or messaging write is allowed only when the approved task names
  that exact update and recipient or object. Make only that update through the
  selected worker host's connector and report exactly what changed.
- With the `slack-read` capability, only the Slack MCP server registered
  exactly as `slack` may be called, and only through its read-only
  `slack_read_thread` and `slack_read_channel` tools. Never send, edit,
  search, upload, or change membership through Slack. The capability is not
  task authority: act only on the channel or thread the coordinator names in
  the task. Registration is presence evidence only — Slack authentication
  and a live read remain unproven until the worker observes the exact granted
  tool names; if the server exposes different tool names or needs
  authentication, stop and report that state instead of widening the grant.
  This same-user harness is not an argument-level sandbox.
- With the `aws-read` capability, only the per-run MCP server registered
  exactly as `aws` — delivered through the coordinator's validated
  `--mcp-config` run file, whose bearer credential is referenced by env name,
  never a value — may be called, and only through the exact tool allowlist
  below. The server is a read-only remote bridge by design: anything mutating
  is unavailable on it. Registration is presence evidence only — bridge
  authentication and a live read remain unproven until the worker observes
  the exact granted tool names; if the tools are absent, named differently,
  or the server reports an authentication failure, stop and report that exact
  state instead of substituting another tool or claiming a read happened.
- With the `asana-read` capability, only the MCP server registered exactly
  as `cm-services` may be called, and only through its read-only Asana
  tools `asana_get_task`, `asana_get_project`, and
  `asana_list_project_tasks` (exact tool IDs `mcp__cm-services__<name>`).
  With the `drive-read` capability, that same server may be called only
  through its read-only Drive tools `drive_file_info`, `drive_sheet_tabs`,
  `drive_sheet_get`, and `drive_doc_get`. `cm-services` is a fixed local
  stdio server the coordinator provisions into the worker host's
  user-global MCP config under the worker's own account before the run;
  the account contract is that the coordinator provisions the same account
  (Asana or Drive respectively) and its internal immutable grants BEFORE
  the server lists tools — credentials alone never authorize a call, and
  the capability is not task authority: act only on the objects the
  coordinator's task names. The two capabilities share one server but
  grant disjoint tool sets: granting one never grants the other, no
  server-wide wildcard exists, and existing API writes are unaffected —
  these capabilities are read-only and any future write capability is a
  separate grant. Registration is presence evidence only — service
  authentication and a live read remain unproven until the worker observes
  the exact granted tool names; if the tools are absent, named
  differently, or the server reports an authentication failure, stop and
  report that exact state instead of substituting another tool or widening
  the grant.
  With the `gcloud-read` capability, that same server may be called only
  through its read-only GCP operations `gcp_logs`, `gcp_run_services`,
  `gcp_run_jobs`, `gcp_scheduler_jobs`, `gcp_functions`, `gcp_billing_mtd`,
  `gcp_billing_daily`, and `gcp_menu`. With the `database-read` capability,
  it may be called only through `postgres_select`; this is the distinct
  read-only Postgres account and proxy. With the `algolia-read` capability,
  only the MCP server registered exactly as `cm-services` may be called, and
  only through its read-only Algolia tool `algolia_get_settings` (exact tool
  ID `mcp__cm-services__algolia_get_settings`). With the `contentful-read`
  capability, that same server may be called only through its read-only
  Contentful tools `contentful_get_entry` and `contentful_search_entries`
  (exact tool IDs `mcp__cm-services__contentful_get_entry` and
  `mcp__cm-services__contentful_search_entries`), mapped to the
  `contentful-new-app` account. With the `contentful-master-read` capability,
  that same server may be called only through its read-only Contentful master
  tools `contentful_master_get_entry` and `contentful_master_search_entries`
  (exact tool IDs `mcp__cm-services__contentful_master_get_entry` and
  `mcp__cm-services__contentful_master_search_entries`), mapped to the
  `contentful-master` account. These capabilities use the same fixed
  `cm-services` registration and are admitted by registration/readiness
  evidence, not by requiring local `gcloud` or `psql` executables.
- Code-graph connectors are read-only. With the `gitnexus` capability, call
  `list_repos` first and report the indexed path, branch, and commit against
  the lane worktree HEAD; treat a mismatch as stale or partial coverage. Never
  run `analyze`, `clean`, `group_sync`, or a non-dry-run `rename`, and never
  register a lane worktree as an index. With the `codegraph` capability, the
  checkout-local graph may rebuild its own ignored database and nothing else.
- Stop and report when an action exceeds these boundaries or its authority is
  uncertain.
- An authorized bounded source-research task may use this execute harness with
  an exact route, capabilities, read roots, and a brief or report-only output
  scope, provided existing explicit execution, delegation, and spend authority
  covers it and the task performs no implementation or external writes.
  Read-only scope does not mean strict review-mode-only. Preserve the strict
  review no-secret/no-MCP contract where the task explicitly requires it, and
  never relabel an execute lane as a sandbox. Generic planning consent alone
  grants no new external dispatch, costs, execute authority, or arbitrary
  worktree writes. The coordinator owns the research question and final
  synthesis, but may delegate evidence gathering or analysis to an economical
  qualified route under existing authority; this does not introduce per-node
  approval.

For a task-scoped environment value, use `env NAME=value <already-granted-command>`;
direct `NAME=value command` prefixes may prompt the native Devin permission layer.
The `env` form does not grant a new launcher: the policy hook still checks the
literal value and the underlying command against the execute allowlist.

## Execute tool allowlist

The Claude host adapter renders this section, and only this section, into
`--allowedTools` / `--disallowedTools` for execute lanes. Review lanes never
receive an allowlist. Each subsection names the capabilities that unlock its
rules; `always` applies to every execute lane. Rules are ordinary developer
commands; nothing here may match deploy, IAM, credential, cloud, merge, or
release tooling, which stay forbidden by the Common rules above.

This allowlist is an approval boundary for a headless session, not a security
boundary. Execute lanes run with the same-user authority the Common rules
describe and without an operating-system sandbox; an allowed interpreter or
package runner (`python3`, `node`, `npx`, `uv`) can in principle run anything
the user can, including a push that the `git-push` capability did not grant, and the
lane process necessarily holds its own provider credential in its environment.
The controls are the injected rules above, the audited lane branch, and the
coordinator's review of the resulting diff before anything is merged or
pushed onward. Do not describe the allowlist as preventing those actions.

MCP capability grants are exact per-tool IDs, never a server-wide wildcard.
A capability whose server arrives per run (`aws-read`) names its exact server
registration in the Execute-mode rules above, delivered through the
coordinator's validated `--mcp-config` run file; a host-registered capability
(`slack-read`, the graph and browser connectors) names its registration in the
host's user or project MCP config instead, and the `asana-read`/`drive-read`
capabilities name the fixed user-global `cm-services` registration the
coordinator provisions into the worker host's controlled home.

Per-run `--mcp-config` delivery rules live in
`side_lane/mcp_run_config.py` (the single source): remote streamable-HTTP
only, credentials referenced by env name only, plaintext HTTP only for the
exact loopback literals, and the Codex host accepts only an exact
`Authorization: Bearer ${ENV}` reference (any other scheme fails closed
rather than being re-labelled). A declared server name that any host scope
already registers aborts the run before any model starts — same-name merge
precedence is not established, so an existing registration and its auth are
never silently overwritten or shadowed — and only server NAMES are read
from host registration files, never values.

### always

- `Read`
- `Edit`
- `Write`
- `Glob`
- `Grep`

### shell, workspace-write, git-push

- `Bash(pnpm *)`
- `Bash(npx *)`
- `Bash(npm *)`
- `Bash(node *)`
- `Bash(./node_modules/.bin/*)`
- `Bash(yarn *)`
- `Bash(uv *)`
- `Bash(uvx *)`
- `Bash(python *)`
- `Bash(python3 *)`
- `Bash(python3.11 *)`
- `Bash(python3.12 *)`
- `Bash(pytest *)`
- `Bash(bash -n *)`
- `Bash(git status *)`
- `Bash(git diff *)`
- `Bash(git log *)`
- `Bash(git show *)`
- `Bash(git add *)`
- `Bash(git commit *)`
- `Bash(git checkout *)`
- `Bash(git switch *)`
- `Bash(git restore *)`
- `Bash(git stash *)`
- `Bash(git worktree list *)`
- `Bash(git rev-parse HEAD)`
- `Bash(git branch --show-current)`
- `Bash(git branch -a)`
- `Bash(git branch -r)`
- `Bash(git merge-base *)`
- `Bash(git fetch origin *)`
- `Bash(gh pr view *)`
- `Bash(gh pr list *)`
- `Bash(gh pr diff *)`
- `Bash(gh pr checks *)`
- `Bash(gh run view *)`
- `Bash(gh run list *)`
- `Bash(ln *)`
- `Bash(cp *)`
- `Bash(mv *)`
- `Bash(mkdir *)`
- `Bash(rm *)`
- `Bash(ls *)`
- `Bash(cat *)`
- `Bash(head *)`
- `Bash(tail *)`
- `Bash(grep *)`
- `Bash(sort *)`
- `Bash(find *)`
- `Bash(sed *)`
- `Bash(wc *)`

- `Bash(echo *)`
- `Bash(pwd)`
- `Bash(which *)`
- `Bash(env)`
- `Bash(env *)`
- `Bash(terraform fmt *)`
- `Bash(terraform validate *)`
- `Bash(terraform version)`

### playwright, gitnexus, codegraph, slack-read, aws-read, asana-read, drive-read

- `WaitForMcpServers`

### playwright

- `mcp__playwright__browser_navigate`
- `mcp__playwright__browser_snapshot`
- `mcp__playwright__browser_find`
- `mcp__playwright__browser_take_screenshot`
- `mcp__playwright__browser_resize`
- `mcp__playwright__browser_click`
- `mcp__playwright__browser_press_key`
- `mcp__playwright__browser_evaluate`
- `mcp__playwright__browser_tabs`
- `mcp__playwright__browser_close`
- `mcp__playwright__browser_wait_for`
- `mcp__playwright__browser_console_messages`
- `mcp__playwright__browser_network_requests`
- `mcp__playwright__browser_fill_form`
- `mcp__playwright__browser_type`
- `mcp__playwright__browser_select_option`

### git-push

- `Bash(git push *)`

### git-push (denied)

- `Bash(git push --force*)`
- `Bash(git push -f*)`
- `Bash(git push * --force*)`
- `Bash(git push * -f*)`
- `Bash(git push --force-with-lease*)`
- `Bash(git push * --force-with-lease*)`
- `Bash(git push --mirror*)`
- `Bash(git push * --mirror*)`
- `Bash(git push +*)`
- `Bash(git push * +*)`

### gitnexus

- `mcp__gitnexus__api_impact`
- `mcp__gitnexus__check`
- `mcp__gitnexus__context`
- `mcp__gitnexus__cypher`
- `mcp__gitnexus__detect_changes`
- `mcp__gitnexus__explain`
- `mcp__gitnexus__group_list`
- `mcp__gitnexus__impact`
- `mcp__gitnexus__list_repos`
- `mcp__gitnexus__pdg_query`
- `mcp__gitnexus__query`
- `mcp__gitnexus__route_map`
- `mcp__gitnexus__shape_check`
- `mcp__gitnexus__tool_map`
- `mcp__gitnexus__trace`

### slack-read

- `mcp__slack__slack_read_thread`
- `mcp__slack__slack_read_channel`

### aws-read

- `mcp__aws__aws___run_script`
- `mcp__aws__aws___get_tasks`
- `mcp__aws__aws___search_documentation`
- `mcp__aws__aws___read_documentation`
- `mcp__aws__aws___retrieve_skill`
- `mcp__aws__aws___list_regions`
- `mcp__aws__aws___get_regional_availability`

### asana-read

- `mcp__cm-services__asana_get_task`
- `mcp__cm-services__asana_get_project`
- `mcp__cm-services__asana_list_project_tasks`

### drive-read

- `mcp__cm-services__drive_file_info`
- `mcp__cm-services__drive_sheet_tabs`
- `mcp__cm-services__drive_sheet_get`
- `mcp__cm-services__drive_doc_get`

### gcloud-read

- `WaitForMcpServers`
- `mcp__cm-services__gcp_logs`
- `mcp__cm-services__gcp_run_services`
- `mcp__cm-services__gcp_run_jobs`
- `mcp__cm-services__gcp_scheduler_jobs`
- `mcp__cm-services__gcp_functions`
- `mcp__cm-services__gcp_billing_mtd`
- `mcp__cm-services__gcp_billing_daily`
- `mcp__cm-services__gcp_menu`

### database-read

- `WaitForMcpServers`
- `mcp__cm-services__postgres_select`

### algolia-read

- `WaitForMcpServers`
- `mcp__cm-services__algolia_get_settings`

### contentful-read

- `WaitForMcpServers`
- `mcp__cm-services__contentful_get_entry`
- `mcp__cm-services__contentful_search_entries`

### contentful-master-read

- `WaitForMcpServers`
- `mcp__cm-services__contentful_master_get_entry`
- `mcp__cm-services__contentful_master_search_entries`

### codegraph

- `mcp__codegraph__find_symbol`
- `mcp__codegraph__find_callers`
- `mcp__codegraph__find_callees`
- `mcp__codegraph__find_importers`
- `mcp__codegraph__neighbors`
- `mcp__codegraph__impact_of`
- `mcp__codegraph__path_between`
