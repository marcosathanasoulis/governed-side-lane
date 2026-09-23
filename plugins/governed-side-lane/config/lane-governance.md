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

- MCP capability descriptions below govern those MCP tools; they do not make
  an MCP bridge, proxy, or predefined credential mapping a prerequisite for
  separately authorized direct service execution. For an already-authorized
  task, use the execution identity's existing local credentials or ADC and
  retrieve required service credentials privately inside execution when that
  access is authorized. Keep credential values out of model context, tool
  output, Slack, and logs. Actual service IAM, requester/task scope, data
  placement, and approvals for consequential actions still apply; possession
  of a credential alone is not task authority. Do not repeat approval solely
  because this authorized execution uses direct service access.

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
- With the `omniroute-read` capability, only the per-run MCP server
  registered exactly as `omniroute` — delivered through the coordinator's
  validated `--mcp-config` run file, whose bearer credential is referenced
  by env name, never a value — may be called, and only through its
  read-only tools `omniroute_get_health`, `omniroute_list_models_catalog`,
  `omniroute_list_combos`, `omniroute_get_combo_metrics`,
  `omniroute_simulate_route`, `omniroute_check_quota`,
  `omniroute_get_session_snapshot`, `omniroute_cost_report`, and
  `omniroute_tool_search` (exact tool IDs `mcp__omniroute__<name>`). The
  account is read-only by construction: it
  carries no mutation scopes and no inference or routing authority — model
  and provider selection stay with the dispatcher — so anything mutating is
  unavailable by design. Registration is presence evidence only —
  authentication and a live read remain unproven until the worker observes
  the exact granted tool names; if the tools are absent, named differently,
  or the server reports an authentication failure, stop and report that
  exact state instead of substituting another tool or claiming a read
  happened.
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
  `gcp_run_jobs`, `gcp_run_job`, `gcp_scheduler_jobs`, `gcp_functions`,
  `gcp_billing_mtd`, `gcp_billing_daily`, and `gcp_menu`. The singular
  `gcp_run_job` reads the metadata of one Cloud Run job named by a required
  job shortname and answers with that job's name, container images,
  create/update timestamps, condition state and reason, latest execution
  name, and execution count; it never returns the full job spec, environment
  variables, or free-text condition messages, and it is not a substitute for
  the plural `gcp_run_jobs`, which remains the execution listing. With the
  `database-read` capability, it may be called only through `postgres_select`;
  this is the distinct read-only Postgres account and proxy. With the
  `algolia-read` capability, only the MCP server registered exactly as
  `cm-services` may be called, and
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
  `contentful-master` account. With the `gateway-read` capability, that same
  server may be called only through its read-only Gateway run tools
  `gateway_run_status` and `gateway_run_report` (exact tool IDs
  `mcp__cm-services__gateway_run_status` and
  `mcp__cm-services__gateway_run_report`), which answer only the GET status
  and report endpoints for the run IDs in the coordinator's explicit
  `gateway_run_ids` grant file. That grant file is the server's own
  server-side allowlist, provisioned with the account before the run; a call
  naming a run outside it fails closed, and neither tool takes a URL, token,
  or other credential argument — the deployment's endpoint, credentials, and
  private identifiers are never named in this public core or passed by the
  worker. The capability is not task authority: read only the run IDs the
  coordinator's task names, and never treat a status read as authority to
  cancel, retry, or otherwise change a run. These capabilities use the same
  fixed `cm-services` registration and are admitted by registration/readiness
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

Per-run coordinator grants extend one lane, never the host or the capability
set. Alongside the `--read-root` directories and `--mcp-config` servers, an
execute lane may be given repeatable exact hostnames with `--web-domain HOST`
to reach public technical documentation (for example `cloud.google.com`). The
adapter renders each host as that host's own permission rule —
`Fetch(https://HOST/*)` on Devin, `WebFetch(domain:HOST)` on Claude — and names
the granted list in the worker's instructions. This grant is execute-only: it
is refused in review mode, it never adds a shell, MCP, file, or write rule, and
no capability unlocks it, so a lane without `--web-domain` receives no fetch
rule at all and may not infer a web grant from shell authority. It is a
permission-matching scope, not a network sandbox: the host, not the rule list,
decides what a redirect, an embedded origin, or a subresource actually loads,
and host runtime enforcement beyond the rule is unproven. Read it as "public
technical documentation only" — no credentials, API keys, auth cookies,
tokens, or private data — and stop and report a needed page that is not on the
list instead of fetching it. A host with no per-destination fetch control
(Codex, whose execute lane runs `danger-full-access`) refuses `--web-domain`
before launch rather than recording a grant nothing enforces. Because the
grant is assessed at dispatch, the coordinator names the required
documentation domains up front instead of letting a worker prompt for them
mid-run.

The `curl` shell grant is for an explicitly task-authorized external HTTP
debugging request, not a general network capability or a per-domain allowlist.
The task brief may name a bounded class of target, the allowed methods, and
the payloads that the debugging requires; a worker may run `curl` only inside
that named scope and must stop and report any destination, method, or body
outside what the task authorized. Authenticated API calls are not
blanket-banned, but the task must explicitly authorize the account, and any
account or secret injection must use the repository's existing mechanism. Never
expose a raw credential to model-visible command arguments, URLs, request
material, files, logs, or reports; an authenticated header may be injected by
that existing authorized mechanism outside the model-visible command text. Do
not follow redirects, do not target cloud metadata
endpoints, and do not use `curl` for unapproved mutations. Localhost and
private-network targets may be legitimate fixtures only when the task
explicitly names them as such. This grant is execute-only and is absent from
review lanes. The `Bash(curl *)` rule is a command allowlist, not a URL filter:
the worker and coordinator are responsible for honoring the task scope and
keeping the request and any reported response free of secrets.

For a task-scoped environment value, use `env NAME=value <already-granted-command>`;
direct `NAME=value command` prefixes may prompt the native Devin permission layer.
The `env` form does not grant a new launcher: the policy hook still checks the
literal value and the underlying command against the execute allowlist.

## Report deliverable

A report-deliverable lane's deliverable is its report artifact, not a commit.
The runner selects this section — and only this section — with an explicit
report option, and renders it for every host, including a host where nothing in
it can be enforced.

- Make no source change, and no change to application configuration,
  infrastructure, credentials, or data.
- Make no git write of any kind. Do not run `git add`, `git commit`,
  `git push`, `git checkout`, `git switch`, `git restore`, `git stash`,
  `git reset`, `git merge`, `git revert`, `git cherry-pick`, `git apply`,
  `git am`, `git mv`, `git rm`, `git tag`, or `git update-index`, and run no
  other command that changes the index, the branch, or any other Git state.
  Where a conflicting repository commit convention — a repository instruction
  file, skill, or convention that requires the lane's work to be committed,
  that nothing stays uncommitted, or that the runner commits the lane worktree
  — would otherwise require a commit or a push, this contract governs the
  lane's own commit and push decisions instead, and the grant bullets above
  that would have authorized them are not rendered into it. That is a narrow
  rule about conflicting repository commit conventions. It is not a claim of
  precedence over a higher-priority security or system instruction, and it is
  not a claim about instruction ordering within a host: where a host orders
  its own instruction files ahead of this task instruction, that host's
  ordering applies, and the runner's after-the-fact verification below is what
  enforces this contract.
- Your one deliverable path is `SIDE_LANE_REPORT.md` at the lane worktree
  root. Write it early and revise it incrementally as you work, so a run cut
  short before its last step still leaves its best available report; revising
  that same file is the incremental update, and it is not a second artifact.
  Do not create another report file at the lane root to hold a draft: the
  runner accepts that one path, and an extra root-level file is refused rather
  than delivered.
- `.side-lane-scratch/` stays available for throwaway notes and intermediate
  output, untracked, exactly as the Common rules above describe. A draft may
  live there when the approved task names it; it is never committed and never
  a substitute for the deliverable.
- Existing browser-report artifacts are unchanged. A report lane granted the
  `playwright` capability may still leave its `SIDE_LANE_REPORT-<name>.<ext>`
  evidence at the lane root under the same namespace, untracked-status
  requirement, and caps as before; those files are capability-scoped evidence,
  not an incremental home for the report.

A report lane is also never granted a capability whose only effect is explicit
write authority. Those capability names are declared once, here, on the one
machine-readable line below, and the runner derives its refusals by reading
that line rather than from a list kept beside this document:

Report forbidden write capabilities: `git-push`, `workflow-write`

The line must appear exactly once in this section, name at least one capability,
and hold only backticked capability identifiers separated by commas; a missing,
malformed, reworded, or duplicated declaration stops the run instead of
narrowing or inventing a refusal. Nothing else is refused: `workspace-write` is
the report artifact's own write and every read capability stays available.

Enforcement differs by host, and this section claims no more than each host
delivers. Claude and Devin carry the `report-deliverable (denied)` rules of the
Execute tool allowlist below, alongside the Common rules. Those are
command-string rules: `git -C <path> ...`, reordered or bundled options, a
compound invocation, and an allowed interpreter are not fully covered by them.
They are an approval-boundary seam, not a sandbox — the worktree is edit
isolation, never an operating-system boundary, and no same-user control here
contains a deliberately adversarial process. Codex carries the instruction
only: that host has no deny seam, so its report contract is instruction plus
after-the-fact verification and is never prevention. On the installed Codex
CLI 0.155.0-alpha.2.6, this contract and the repository's own `AGENTS.md` both arrive as user-role
input, with the task text — this contract included — arriving after the
repository file, so a repository instruction file is not inherently higher
priority there; that ordering is an observation of the installed version, not a
claim established for every version or for the cloud service. On every host the
runner verifies the result after the run and refuses a report lane that
committed or left unexpected files; that gate, not this instruction, is what is
enforced.

## Publication refusal

An execute lane may be selected with an explicit task no-external-publication
guard: the approved task's authority forbids this lane from publishing anything
outside the launching machine, whatever the runner or the host could otherwise
do. The runner selects this section — and only this section — with that option,
and renders it for every host, including a host where nothing in it can be
enforced.

- Make no external publication. Do not run `git push` in any spelling or form,
  do not create or update a remote branch or remote tag for the lane branch,
  and do not have another command, tool, or connected system do it on your
  behalf. The lane's work stays on the lane branch in the lane worktree, where
  the coordinator decides what happens to it next.
- This is the task's authority, and it is not the runner's `--no-publish`. That
  option is a separate, runner-side decision to skip the runner's own automatic
  push of a delivered branch; a lane carrying it alone is not forbidden to push
  and may still publish its own branch. A lane selected with this guard also has
  the runner's own automatic push suppressed, so the guard alone is enough and
  the caller does not have to remember `--no-publish` as well. Read this
  section, not that option, as the rule that governs your own publication
  decisions.
- Do not read a missing denial as permission. The rule above governs this lane
  whether or not this host can enforce it, and a host that cannot deny the
  command still expects the lane not to run it.
- A deliverable that would otherwise be published is handed back as the lane
  branch the coordinator reviews. Losing a remote copy is the intended outcome
  here, not a failure to work around.
- Enforcement differs by host, and this section claims no more than each host
  delivers. Claude and Devin carry the `no-external-publication (denied)` rules
  of the Execute tool allowlist below, alongside the Common rules. Those are
  command-string rules: `git -C <path> push`, a reordered or bundled option, a
  compound invocation, and an allowed interpreter are not fully covered by
  them. They are an approval-boundary seam, not a sandbox — the worktree is
  edit isolation, never an operating-system boundary, and no same-user control
  here contains a deliberately adversarial process. Codex carries the
  instruction only: that host has no deny seam, so its refusal is never
  prevention. On every host the runner records in its audit what it asked for
  and which of these the host could enforce, and records that the worker's own
  publication is otherwise unverified. Neither record is evidence that no
  publication happened.

Publication-refusing lanes never hold a capability whose whole grant is the
publication this guard refuses. Those capability names are declared once, here,
on the one machine-readable line below, and the runner derives its refusals by
reading that line rather than from a list kept beside this document:

Publication refusal never grants these capabilities: `git-push`

The line must appear exactly once in this section, name at least one capability,
and hold only backticked capability identifiers separated by commas; a missing,
malformed, reworded, or duplicated declaration stops the run instead of
narrowing or inventing a refusal. Nothing else is refused: `workflow-write`
stays a separate grant a task may authorize, and every read capability and
`workspace-write` are untouched.

## Execute tool allowlist

The Claude host adapter renders this section, and only this section, into
`--allowedTools` / `--disallowedTools` for execute lanes; the one addition
outside it is the per-run `--web-domain` grant described in Execute mode
above, which appends one `WebFetch(domain:HOST)` rule per explicitly granted
host and nothing else. Review lanes never receive an allowlist. Each
subsection names the capabilities that unlock its rules; `always` applies to
every execute lane. Rules are ordinary developer
commands; no capability here may grant deploy, IAM, credential, cloud, merge,
or release tooling, which stay forbidden by the Common rules above. Three
reserved subsections are not capabilities. `report-deliverable (denied)`
lists git-write commands that a report-deliverable lane must not run, so those
rules exist there only to be denied and no capability ever grants them.
`no-external-publication (denied)` lists the publication commands a lane
carrying the task no-external-publication guard must not run, on the same
terms: rules there exist only to be denied and no capability ever grants them.
`existing-workspace (denied)` lists the direct git-write commands an
existing-owner-workspace lane must not run, on those same terms: those rules
exist only to be denied, no capability ever grants them, and the verbs they
name are the verbs the `## Existing owner workspace` section forbids, so the
command a lane is told not to run and the command the host is given to deny
are one list rather than two that can drift apart. `local-developer (granted)`
names the tool surface of the local developer
execute profile described below: no capability unlocks it and no lane is
granted it — a profile selection, made once per run, is what selects it, and
every capability's own rules are rendered alongside it exactly as before.

**The local developer execute profile.** An execute lane is either `standard`
or `local-developer`. `standard` is the public default: the literal
per-command rules of the capability subsections below, and no rule outside
them. `local-developer` selects the `local-developer (granted)` surface instead
— the host's own native shell class in place of an enumeration of literals,
because a closed enumeration of command prefixes denies every family nobody
thought to list (`gh api`, `git grep`, `cd`, `gh auth status`, a compound
`ls … | head …; cat …`, `git -C <path> …`, and the next one), and each unlisted
family is a separate denial with the same cause.

The profile also stops suppressing the worker host's own MCP registrations, and
makes the ones it already holds usable by emitting one server-wide
`mcp__<server>` rule per inherited server. The inventory is read from the
registration files the worker's own environment resolves — a controlled `HOME`,
and on a routed lane the disposable `CLAUDE_CONFIG_DIR` that lane runs under —
so the rules describe the registry the process loads,
never a registry it does not. That is what lets a local developer's own
signed-in tooling keep working, and the host's registrations of the graph,
browser, messaging and remote names count here too: a name a capability also
maps to is still the developer's own registration, so it is usable by
registration and the capability's exact per-tool rules render beside it.

The one exception is the `cm-services` proxy, which is a capability-gated
bridge rather than a host-native tool. It is deliberately excluded from the
inventory: granting a `cm-services` capability renders that capability's exact
per-tool IDs, an ungranted one has its tool IDs denied, and a registration of
the name alone reaches none of them. Registration is presence evidence only: it
authenticates nothing, grants no scope, and a listed server's granted scope
remains unproven until a permitted tool call succeeds.

It changes nothing else: the capability grants and their exact per-tool MCP
IDs, the `report-deliverable (denied)` rules, and every Common and Execute-mode
rule are unaffected. The profile reuses each registration's own native auth
reference — an env reference the registration states, or a session the server
manages — exactly as written; it copies, exports, and logs no credential. A
credential the host holds outside the registration itself, notably a native MCP
OAuth token cache, is not carried into the lane's config directory, so such a
server may load and still fail to authenticate: that is reported as unproven,
never as a grant. In particular the profile does not move a cloud read onto
a proxy or off one. A local developer reading cloud state with the `gcloud` CLI
or ADC through the host's own shell is doing ordinary local work under this
task's authority — the same-user authority described below — while the
capability-gated `cm-services` path stays a separate, narrower, explicitly
granted route that is never required for it.

The profile is selected by private configuration, and never by a rule in this
public document: this package ships the mechanism and the conservative default,
not the policy about which route is local. Two independent statements must both
hold — the private route table explicitly opts its local routes in, and the
route itself declares `execution_location: local-user-workspace`. Which host
executes the lane is **not** a third premise: the same same-user local
workspace is the same ordinary developer surface whichever qualified host runs
there, so the profile resolves from the route alone and each adapter renders it
on its own seam — the Claude allowlist, the Devin pre-tool command policy *and*
the Devin native permission layer that decides before that policy hook runs, the
Codex native surface. Gating the selection on a host name would let a route's
own authorized statement be true while the read is still denied, which is a
routing fault and not a boundary. The public
route table declares local locations but no policy, so every lane it describes
is `standard`; a cloud-generated table, a `cloud-only` route, and a route that
declares nothing are likewise `standard`. A lane for which nothing selects the
profile is `standard`. The selection is execute mode only: a review lane's argv
is the strict read-only form and refuses it. A lane whose deliverable is the
report (`--report-only` or `--report-deliverable`) always resolves `standard`,
whatever the private table and the route declare: the report contract is the
narrowed form of an execute lane, and the widened surface — the bare shell
class and the server-wide rules for inherited registrations — is exactly what
that contract excludes. An explicit `local-developer` selection beside a report
flag is refused rather than silently reconciled, because dropping either
selection would bypass the other.

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
A capability whose server arrives per run (`aws-read`, `omniroute-read`) names its exact server
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
- `Bash(curl *)`
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

### playwright, gitnexus, codegraph, slack-read, aws-read, omniroute-read, asana-read, drive-read

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

### local-developer (granted)

- `Bash`

### report-deliverable (denied)

- `Bash(git fetch)`
- `Bash(git fetch *)`
- `Bash(git add *)`
- `Bash(git commit)`
- `Bash(git commit *)`
- `Bash(git checkout *)`
- `Bash(git switch *)`
- `Bash(git restore *)`
- `Bash(git stash)`
- `Bash(git stash *)`
- `Bash(git merge)`
- `Bash(git merge *)`
- `Bash(git reset)`
- `Bash(git reset *)`
- `Bash(git revert *)`
- `Bash(git cherry-pick *)`
- `Bash(git apply *)`
- `Bash(git am *)`
- `Bash(git mv *)`
- `Bash(git rm *)`
- `Bash(git tag *)`
- `Bash(git update-index *)`
- `Bash(git push)`
- `Bash(git push *)`

### no-external-publication (denied)

- `Bash(git push)`
- `Bash(git push *)`

### existing-workspace (denied)

- `Bash(git add)`
- `Bash(git add *)`
- `Bash(git commit)`
- `Bash(git commit *)`
- `Bash(git push)`
- `Bash(git push *)`
- `Bash(git checkout)`
- `Bash(git checkout *)`
- `Bash(git switch)`
- `Bash(git switch *)`
- `Bash(git restore)`
- `Bash(git restore *)`
- `Bash(git reset)`
- `Bash(git reset *)`
- `Bash(git stash)`
- `Bash(git stash *)`
- `Bash(git clean)`
- `Bash(git clean *)`
- `Bash(git rm)`
- `Bash(git rm *)`
- `Bash(git mv)`
- `Bash(git mv *)`
- `Bash(git apply)`
- `Bash(git apply *)`
- `Bash(git am)`
- `Bash(git am *)`
- `Bash(git rebase)`
- `Bash(git rebase *)`
- `Bash(git merge)`
- `Bash(git merge *)`
- `Bash(git cherry-pick)`
- `Bash(git cherry-pick *)`
- `Bash(git revert)`
- `Bash(git revert *)`
- `Bash(git update-ref)`
- `Bash(git update-ref *)`
- `Bash(git symbolic-ref)`
- `Bash(git symbolic-ref *)`
- `Bash(git update-index)`
- `Bash(git update-index *)`
- `Bash(git read-tree)`
- `Bash(git read-tree *)`
- `Bash(git write-tree)`
- `Bash(git write-tree *)`
- `Bash(git commit-tree)`
- `Bash(git commit-tree *)`
- `Bash(git pack-refs)`
- `Bash(git pack-refs *)`
- `Bash(git replace)`
- `Bash(git replace *)`
- `Bash(git filter-branch)`
- `Bash(git filter-branch *)`
- `Bash(git config)`
- `Bash(git config *)`

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

### omniroute-read

- `mcp__omniroute__omniroute_get_health`
- `mcp__omniroute__omniroute_list_models_catalog`
- `mcp__omniroute__omniroute_list_combos`
- `mcp__omniroute__omniroute_get_combo_metrics`
- `mcp__omniroute__omniroute_simulate_route`
- `mcp__omniroute__omniroute_check_quota`
- `mcp__omniroute__omniroute_get_session_snapshot`
- `mcp__omniroute__omniroute_cost_report`
- `mcp__omniroute__omniroute_tool_search`

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
- `mcp__cm-services__gcp_run_job`
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

### gateway-read

- `WaitForMcpServers`
- `mcp__cm-services__gateway_run_status`
- `mcp__cm-services__gateway_run_report`

### codegraph

- `mcp__codegraph__find_symbol`
- `mcp__codegraph__find_callers`
- `mcp__codegraph__find_callees`
- `mcp__codegraph__find_importers`
- `mcp__codegraph__neighbors`
- `mcp__codegraph__impact_of`
- `mcp__codegraph__path_between`

## Existing owner workspace

An execute lane normally runs in a dedicated worktree the runner created from
HEAD. An existing owner workspace is the other case: the operator names a
checkout they own — through the CLI's explicit workspace selection — and the
worker's working directory is that checkout, exactly as it stood. The runner
selects this section, and only this section, for such a lane, in addition to
the active-mode rules; the commit/push bullet of Execute mode is dropped,
because there is no assigned lane branch here for it to be about.

- The workspace is not a dedicated lane and isolation is not claimed for it.
  It is a checkout that predates this run, on a branch this run did not
  create, and it may hold staged, unstaged, and untracked work belonging to
  someone else. Nothing contains your writes to it beyond the task you were
  given and the rules below.
- Work only in that workspace. Do not create a worktree, switch branches, or
  check out any other ref.
- Make no git write of any kind. Do not run `git add`, `git commit`,
  `git push`, `git checkout`, `git switch`, `git restore`, `git reset`,
  `git stash`, `git clean`, `git rm`, `git mv`, `git apply`, `git am`,
  `git rebase`, `git merge`, `git cherry-pick`, `git revert`,
  `git update-ref`, `git symbolic-ref`, `git update-index`, `git read-tree`,
  `git write-tree`, `git commit-tree`, `git pack-refs`, `git replace`,
  `git filter-branch`, or `git config`. A commit
  here would take the owner's staged work with it, and a push would send the
  owner's commits to a remote as a side effect of your run. Leave the work
  uncommitted; deciding what to do with it is the owner's, not yours. These
  same verbs are declared once as deny rules, in the
  `existing-workspace (denied)` subsection of the Execute tool allowlist, so
  the command you are told not to run and the command each host is given to
  deny are one list; the `Everything else is unchanged` bullet below says what
  that denial does and does not add. The list names only commands whose every
  spelling is a write: update-index and symbolic-ref write the index and HEAD
  respectively even when the file they name is untouched, which is why they
  are denied here and why the same commands appear in the
  `report-deliverable (denied)` bucket above. Commands with a read-only
  spelling this mode must keep — branch and tag list, remote and worktree have
  inspection forms, reflog is a log — are not denied, and a write through one
  of their other spellings is caught by the after-the-fact comparison below
  instead.
- Anything already in the workspace that you did not put there belongs to
  whoever left it. Do not revert, reformat, tidy, relocate, or delete it, and
  do not treat it as a change you made. If it blocks the task, stop and report
  it rather than clearing it.
- Report the paths you changed. The runner compares the workspace against a
  content-and-index baseline taken before you started, so a file that was
  already dirty and that you changed again is reported as changed, and a file
  you left alone is reported as untouched — however dirty it already was.
  That comparison covers more than file contents: it records every tracked
  path's index flags as well as its entry, and it reads the repository's refs,
  its local config and its worktree registrations, none of which appear in
  `git status`. A write that moves no file — a ref moved, HEAD repointed, a
  flag set, a worktree registered — is therefore still reported, and a run
  that made one is refused as a git write this mode forbids. Your report and
  the runner's record must agree.
- Everything else is unchanged: the Common rules above, the capability grants
  and their exact per-tool MCP IDs, the execute tool allowlist or the tool
  profile this lane selected, model and credential handling, and the read-only
  database rule. This section narrows a lane's git authority; it widens
  nothing. In particular it does not narrow the tool surface: a lane reached
  here still runs whichever execute profile it selected, the local developer
  profile included, and every read-only git command (`git status`, `git log`,
  `git diff`, `git show`, `git branch`, `git rev-parse`) keeps working exactly
  as it did. Dedicated worktrees on the same host and route are untouched:
  this section is selected by an explicit owner-workspace run, and an ordinary
  lane keeps the commit and push grant above.

Enforcement differs by host, and this section claims no more than each host
delivers. Claude and Devin carry the `existing-workspace (denied)` rules of the
Execute tool allowlist above, alongside the Common rules: on Claude they render
as `--disallowedTools` deny rules, which take precedence over the profile's
allow rule, and on Devin they render twice — in the native permission deny list
the layer consults first, and in the PreToolUse command policy, which strips a
leading `git -C <target>` before matching a *denial*, so a
`git -C <path> <verb>` spelling is denied there too, whatever checkout the
target names. Those are command-string rules, and a command-string rule only
describes the command it is matched against: on Claude that same
`git -C <path> <verb>` spelling, a reordered or bundled option, a compound
invocation, and an allowed interpreter are not fully covered by them. They are an approval-boundary seam, not a sandbox — the
workspace is edit isolation, never an operating-system boundary, and no
same-user control here contains a deliberately adversarial process. An allowed
interpreter or package runner (`python3`, `node`, `npx`, `uv`) can in principle
run `git` itself, and a scaffolded subprocess can write git state that no
command-string rule here inspects. That gap is stated rather than papered over:
this section does not promise that a shell which must stay arbitrary is also a
sandbox. Codex carries the instruction only: that host has no deny seam, its
execute lane runs `danger-full-access`, and its owner-workspace contract is
instruction plus the runner's after-the-fact comparison below, never
prevention. On every host the runner compares the workspace against the
content-and-index baseline it took before the run and reports the paths that
changed — including a file that was already dirty and changed again, and one
whose index entry carries a flag that made `git status` stop reporting it —
together with the parts of the repository's git state that are not paths at
all: HEAD and the branch, the index, the refs, the local config and the
worktree registrations. That is what makes the guard effective rather than
declarative: a deny list is a set of command spellings and is never complete
over an arbitrary shell, while this comparison is over what the workspace
actually holds before and after. It is a report about what happened, not
evidence that nothing else did.
