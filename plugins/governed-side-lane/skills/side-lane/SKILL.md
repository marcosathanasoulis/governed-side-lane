---
name: side-lane
description: Use at the start of an implementation task to discover and qualify optional model lanes, then route approved tasks through exact configured workers, including Prompt it staffing and explicit spend routing.
---

# Side lane

Use at the start of an implementation task to discover and qualify optional
model lanes, then route approved tasks through exact configured workers,
including Prompt it staffing and explicit spend routing. Installation is
optional: when the skill is not installed or no route qualifies, continue on
the originating host and record the exception; when it is installed, assess it at
the appropriate implementation task start, not only when the user explicitly
asks for delegation.

On an eligible execution request, run the documented host-specific
`check-capabilities` and `recommend` assessment before deciding staffing,
whether or not the user invoked Prompt it. The Side Lane assessment also runs
outside the Prompt it workflow; follow the applicable proportional-planning
rules. The assessment is not automatic dispatch and not automatic billable
approval: a recommendation never dispatches a lane, and every existing gate
still applies — host readiness, task eligibility and evidence, the user's
existing task/delegation authorization, explicit execution approval,
`--approve-billable-route`, capability grants, worktree isolation, and the
preapproved-backup rules. Existing authorization covers the approved
delegation; this assessment does not add a per-node permission prompt. If no
route is eligible or an eligible route is unavailable, use the qualified
preapproved backup under the existing availability policy when its conditions
are met; otherwise continue on the originating host and record the exception.
Never silently substitute a route.

Use the [bundled runner](../../bin/side-lane), resolving that link relative to
this `SKILL.md` and invoking the resulting absolute path.
Resolve any symlink in this `SKILL.md` path before resolving that relative link
so an installed host-specific overlay, route configuration, and credential
service remain authoritative. A companion skill or executable named
`side-lane` on `PATH` does not prove that this core skill is installed or
configured. On Unix-like hosts the runner is executable directly; on Windows
invoke it with the configured Python 3 launcher. Do not install software,
modify global instructions, or look for a private source checkout when the
bundled runner is present.

This skill is distributed to both Codex and Claude Code. Each product loads its
own skill wrapper, CLI, OAuth session, and connectors; neither product borrows
the other's identity or configuration.

Optional account/model candidates and task-specific connector setup are in the
public [model guide](../../docs/model-guide.md) and
[connector guide](../../docs/connector-guide.md).
They do not make a route executable: keep candidate, configured, available, and
authorized states separate, and continue normally with one native OpenAI or
Claude host when no optional route qualifies. For optional pooled routing, read
the [OmniRoute add-on guide](../../docs/omniroute-guide.md); it is not required
and Side Lane owns task fit and acceptance even when a gateway is present.

For new provider setup or a first trial, read
[provider qualification](references/provider-qualification.md). It covers
DeepSeek, Kimi, MiniMax, xAI/Grok, and Cognition/Devin without treating a saved
credential, subscription, or catalog entry as an executable adapter. Apply the
same route-specific process to future providers; keep exact IDs in configuration
and sourced model guidance rather than hardcoding a second allowlist here.

Inspect the resolved runner's help. `list` shows configured route inventory, not
current readiness. For the exact host, mode, provider, model, and repository,
run `check-capabilities`; treat its OAuth/credential result as presence-only and
never retrieve a secret value. Use `recommend` separately for task eligibility.
Record `absent`, `configured`, `auth-or-tool-missing`, `task-unqualified`, or
`eligible` with evidence source, observation time, and reason. Native
Codex/OpenAI and Claude routes use the selected host's existing signed-in OAuth
subscription. If OAuth is absent or expired, stop and offer that host's
sign-in/refresh command; never log in automatically. Never silently substitute
a model, provider, gateway, or paid key route.

When available, `candidates` reads the research catalog only. Its
`execution_location`, `qualification_state`, and false executable/allowlist/
credential/authorization fields identify an evaluation subject, not a configured
or usable route. It performs no account, quota, connector, credential, or
authorization check. Candidates remain optional; ordinary native work proceeds
without one.

For Prompt it staffing, inspect the current runtime and documented lane
inventory for exact available model names and supplied capability descriptions.
Compare qualified Anthropic/Claude and Codex models with the same task-relative
rubric and record the exact assignment reason; provider brand alone is not
capability or reviewer-independence evidence.
Classify each task before ranking routes: `light` for bounded mechanical or
fixture work, `deep` for ambiguous debugging, architecture, or large-repository
reasoning, `design` for visual hierarchy and design judgment, and `browser` for
connector-backed navigation and interaction. These bands are task requirements,
not model tiers. Require supplied evidence for the requested band, quality
floor, context/output estimate, tools, host, and authority. Coding or vision
evidence does not satisfy design or browser requirements by implication;
unknown or stale band evidence excludes the route until refreshed.
For ordinary work, choose the least-cost or most-efficient eligible model that
meets the task's quality, reasoning, context, tools, host, and authority needs.
Honor explicit developer/user preferences and stated usage or surplus
constraints, record material tradeoffs, and never probe quotas or invent cost
when reviewed evidence is missing. Provider diversity alone does not justify an
independent second opinion; require a distinct question and decision value based
on uncertainty, non-determinism, impact, irreversibility, material disagreement
risk, or an explicit acceptance gate.
Compare the complete session: task tokens, known tool charges, dispatch/setup,
retries and coordinator correction, and any independent review. Keep prepaid
subscription usage separate from marginal provider-key usage. An exact route
verified as covered by a subscription the user already pays has a known `$0`
additional/marginal usage cost — a different statement from an unknown price
and from a zero-cost provider key, and not a claim that every OAuth or hosted
route is free. Unknown prices or overhead remain unknown and are never treated
as zero. A frontier route needs
task-specific evidence that an eligible economical route cannot meet the need.

GLM is optional and enters staffing only when the user explicitly enables it.
Its only selectable model is the fixed `glm-5.3`; never propose another GLM
model or generic GLM fallback.
It may appear as configured inventory before enablement; exclude it from
staffing until the explicit GLM gate passes.
It is execute-only: review mode cannot use a provider key because canonical
review governance forbids secret access.
It uses an exact configured provider/gateway and the user's prepaid flat-rate
subscription, so its marginal task cost is zero while available. It can rank
cheapest only after the same worker-host capability, task-evidence, and quality
gates. Before retrieving a credential value or launching, confirm that user
authorization covers the run and pass `--approve-billable-route`. Existing
explicit standing authorization can cover eligible runs; do not ask again
within its scope.
If GLM reports its temporary quota pause, do not retry or fall back silently.
The coordinator may use only the one approved non-GLM backup under the
preapproved-backup reassignment policy below.
The default packaged GLM gateway is direct Z.AI; OpenRouter is neither required
nor packaged as a default developer route.

Both modes require `--lane-name <name>` and use a dedicated worktree. Lane
worktrees default to `<repo>/.side-lanes/worktrees` (excluded from `git status`
via `.git/info/exclude`); when the governed repository's rules require
worktrees in a sibling directory, pass `--worktree-root ../<dir>` or set
`SIDE_LANE_WORKTREE_ROOT`, and the runner creates them there instead. Use
`--mode review` for strict read-only/no-MCP investigation; its clean disposable
worktree is audited and removed. Use `--mode execute` for implementation and
retain its worktree for coordinator inspection. Add only capabilities the task
actually requires. The runner injects
`config/lane-governance.md`; do not duplicate or weaken those rules in a prompt.

When execute work needs shared instructions outside its worktree, pass repeatable
`--read-root /absolute/directory` grants for the required directories. The runner
validates and audits them. Devin receives additional read-only file-tool grants;
Claude and Codex receive task-scope instructions without extra workspace/write
grants. This is not an OS sandbox. Review mode rejects read roots.

`side-lane recommend` runs for an eligible execution request whether staffing
happens inside Prompt it or on an ordinary request that never invoked it; when
installed and configured, both paths call it the same way. If the runner is
missing or returns no eligible route, the coordinator follows the qualified
preapproved-backup policy when applicable, otherwise continuing on the
originating host. The coordinator stays fixed while Codex and Claude worker hosts are
qualified independently against their own OAuth, connectors, and tools.
Recommendations are capability/evidence gated and never dispatch a lane.
Coordinator origin does not select a provider: Codex-origin staffing may
consider configured native Claude and fixed `glm-5.3` on its Claude worker;
Claude-origin staffing may consider configured native Codex and the same
explicitly enabled fixed GLM route on its Claude worker.
During Prompt it research, the runner may launch a qualified review lane when
the user has explicitly authorized that bounded external research team. An
authorized bounded source-research task may also use an execute harness with an
exact route, capabilities, read roots, and a brief or report-only output scope,
when existing explicit execution, delegation, and spend authority covers it and
the task performs no implementation or external writes. Read-only scope does not
mean strict review-mode-only. Generic Prompt it consent and recommendation
output do not authorize any launch. Review governance still forbids
MCP/connectors and secrets. Execute lanes, including GLM, retain their separate
approval gates.

Claude-host and Devin-host workers default to a 30-minute (1,800-second)
process timeout. An explicit model route's positive integer `timeout_seconds`
overrides that default; inspect the installed configuration before reporting
an effective limit. Native Codex routes have no Side Lane process timeout.
Provider request limits, host-service limits, readiness checks, and explicit
task budgets are separate. A timeout change applies to subsequent launches;
it cannot extend an already-running worker. Keep partial results and report
failure under the existing retry and authorization rules.

After execution approval, dispatch the exact approved route and record route
recheck, dispatch, worktree and capability grants, handoff, validation, and
coordinator acceptance. A failed task may receive only the approved in-scope
retry on that same route, with its added session cost recorded. The runner never
selects a fallback. For a qualifying availability failure, the coordinator may
visibly reassign only to the one preapproved backup after refreshing readiness.
`config/lane-governance.md#preapproved-backup-reassignment` defines the trigger
evidence, stop/preservation/reconciliation sequence, ownership, GLM restriction,
and no-permission-pause condition.

An execute lane runs locally as the selected signed-in user. Its worktree is
Git-edit isolation, not an OS/container/cloud sandbox. This does not broaden
the approved task or authorize an external write: model inference may be hosted
while the worker's tools remain local, and each connector-backed action keeps
its existing explicit permission gate.

For durable context that must survive movement between direct Codex, direct
Claude Code, and side lanes, follow the installed pointer to
`config/agent-context.md`. Host-private memory and connector sessions are hints,
not synchronized authoritative project memory.

## Execute-lane permissions

Execute-mode Claude-host lanes (native Claude and GLM) receive an explicit
`--allowedTools` list derived from the `--capability` flags passed to the
runner. Review mode never receives an allowlist — its argv stays the strict
read-only form regardless of any capability. An unknown capability name fails
closed rather than being ignored.

The effective rules per capability are the "Execute tool allowlist" section of
`config/lane-governance.md`; the adapter renders that section and nothing else,
so do not restate or extend the list in a prompt or skill.

Execute lanes also inherit the host's configured MCP servers (for example,
Playwright, when the `playwright` capability reports as present via
`check-capabilities`). Review mode hides every MCP server with
`--strict-mcp-config`, so no capability makes a connector available there.
On a Claude-host execute lane a connected MCP server is callable only when a
granted capability allowlists its tools: pass `--capability gitnexus` and/or
`--capability codegraph` when the worker should query those code graphs.
Only their read-only query tools are granted there; GitNexus index mutation
(`analyze`, `clean`, `group_sync`, non-dry-run `rename`) never is. Codex
execute lanes do not render this allowlist. The grants name the server, so on
the Claude host the MCP server must be registered as exactly `gitnexus` /
`codegraph`; `check-capabilities` reports `name-mismatch` for a near-miss and
the launch gate refuses it. Codex lanes keep connector-name presence.

`--capability slack-read` grants the execute-only Slack read tools on the MCP
server registered exactly as `slack`: `slack_read_thread` and
`slack_read_channel`, and nothing else — no sending, editing, search, files,
membership changes, or wildcard tools. Claude-host and Devin execute lanes
receive those exact per-tool grants; Codex lanes keep their existing
capability behavior. The capability is not task authority: the coordinator's
task must name the authorized channel or thread, and the same-user harness is
not an argument-level sandbox. Registration is presence evidence only —
`check-capabilities` reports it separately from Slack authentication and a
live read, which stay unproven until the worker observes the exact granted
tool names; a server exposing different tool names fails qualification with a
reported mismatch rather than a wildcard fallback.

`--capability asana-read`, `--capability drive-read`, and `--capability
algolia-read` grant disjoint exact read-only tool sets on the fixed local MCP
server registered exactly as `cm-services` (`mcp__cm-services__asana_*` /
`mcp__cm-services__drive_*` / `mcp__cm-services__algolia_get_settings`),
which the coordinator provisions into the worker host's user-global config
under the same account before the run. Granting one capability never grants
the other's tools, there is no server-wide wildcard, and every capability is
read-only — any future write capability is a separate grant. Claude-host
execute lanes also health-probe `cm-services` before launch and instruct the
worker to `WaitForMcpServers` for it. Registration is presence evidence
only — same-account provisioning, service authentication, and a live read
stay unproven until the worker observes the exact granted tool names.

## AGENTS.md linkage

Root `AGENTS.md` must contain a line with a Markdown link to root `CLAUDE.md`
in one of two accepted forms (verbatim wording may vary as long as the
required words are present):

- "You **must** read [CLAUDE.md](./CLAUDE.md); it is the authoritative source
  of truth."
- "**[`CLAUDE.md`](./CLAUDE.md) is the source of truth for this repo's
  rules.**"

Links on such a line may only point at root `CLAUDE.md`.

## Metered models and standing authorization

Authentication and billing are separate. Native Devin OAuth can include
metered Gemini/Grok routes; use the exact model's effective `billable` metadata,
not the login method. Pass `--approve-billable-route` for every authorized
billable dispatch, including OAuth routes. Keep included SWE routes marked
non-billable only while the configured entitlement applies.

If the user explicitly authorizes cost-effective metered routing, record that
policy and apply it to eligible task-specific routes without repeated permission
questions. Compare expected total accepted-task cost, including retries and
review, rather than token price alone. Record the selected route, cost evidence,
and authorization basis. Standing cost authorization does not enable an
unconfigured provider or authorize unrelated tasks. Unknown empirical history
or a missing median is measurement absence, not a hard availability, capability,
context, security, or known-failed-quality gate; a verified included
subscription or current tariff with bounded expected usage and existing explicit
authority can support dispatch while reporting the history as unavailable.
Verified included coverage without overage has a known `$0` additional usage
cost; a paid tariff retains its actual bounded estimate, not zero. A missing median is not an unknown
current price and is not by itself a reason to alert, notify, or demand new
approval on an otherwise authorized task. When coverage is verified and the
median is not, the plain disclosure is "$0 additional usage cost — covered by
subscription; historical median unavailable". Unknown costs do not prove a route
is cheapest and are never reported as zero; missing history is never rewritten
to zero.
