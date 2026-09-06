---
name: prompt-it-side-lane-routing
description: Optionally qualify Side Lane routes for a Prompt it task graph and route explicitly authorized, bounded external research reviews without changing the originating coordinator.
---

# Prompt it side-lane routing

Use this companion while researching and staffing a Prompt it task graph. It is
not a replacement for Prompt it or a general provider picker. The normal Prompt
it workflow remains fully usable when `side-lane` is not installed or has no
qualifying route.

This is an optional integration for users who already have a compatible
Prompt-it workflow. It is not required to use the core side-lane skill. Never
require a private checkout or organization-specific global configuration.

## Resolve the installed core and runner

Inspect the current host's supplied skill inventory for the installed core Side
Lane skill; its displayed name may be namespaced. Distinguish the core routing
skill from this Prompt it companion, read the core skill's own instructions,
resolve any symlink in its `SKILL.md` path, and resolve its documented runner
link relative to that resolved file. Use that runner so host-specific overlays,
route configuration, and credential-service selection remain authoritative.

The companion's presence or an executable named `side-lane` on `PATH` does not
prove that the core skill is installed or configured. If no installed core is
present, use the [companion-bundled runner](../../bin/side-lane) only when it is
part of the same compatible package as this skill, resolving this skill's
symlink before the relative link. Label that result as runner-only; do not infer
a full installation, route readiness, or host-specific overlay.

## Keep the coordinator fixed; qualify each worker host

Determine the originating coordinator host before considering a side lane:

- A Codex-origin task keeps its Codex coordinator. A proposed Claude worker
  uses Claude Code's OAuth and Claude connector identity.
- A Claude Code-origin task keeps its Claude coordinator. A proposed Codex
  worker uses Codex's OAuth and Codex connector identity.

The runner may launch a qualified worker, but it never moves the coordinator or
borrows connector identity. From a Codex-origin task, consider configured native
Claude routes and, only after explicit GLM enablement, fixed `glm-5.3` on the
Claude worker host. From a Claude-origin task, consider configured native Codex
routes and the same explicitly enabled fixed GLM route on the Claude worker
host. Probe each candidate worker host independently and exclude it when that
host lacks a required connector, tool, repository capability, or authority.
GLM has no independent connector session; it uses its selected Claude worker
host. Never infer a provider choice from coordinator origin or marketed host
support.

## Optional, presence-only discovery

Inspect the resolved runner's help before using its discovery commands. `list`
returns configured route inventory; it does not prove current authentication,
tools, task fit, or readiness. Run `check-capabilities` for the exact
`host + mode + provider + model` and repository under consideration. It may
report executable, tool, connector, and OAuth or credential presence, but it
must not retrieve a credential value. Then use the documented `recommend`
command separately for task eligibility. Supply the exact coordinator host,
requested mode, repository, required operational and behavioral capabilities,
task-fit band, quality floor, declared token budget, per-host cost state, and
any user-declared preference. Ask each command for its installed help; do not
invent runner flags or pass credentials.

For each considered route, record one state: `absent`, `configured`,
`auth-or-tool-missing`, `task-unqualified`, or `eligible`. Include the evidence
source, observation time, and reason. A configured GLM route may appear in
inventory before the user enables it; exclude it from staffing until explicit
GLM permission exists. Native OAuth defaults to included subscription usage.
When the user says a host is on extra usage, record only that statement (for
example Claude `extra-usage`, Codex `included-oauth`).

This discovery is deterministic and presence-only. It must never read or infer
provider quotas, consumer-product usage, billing, API keys, or credential
values. It must not install or configure the runner, enable a connector, make a
paid request, log in a host, or dispatch a lane. Generic runner presence, a
recommendation, or the user's generic Prompt it invocation never authorizes
dispatch.

If the executable, host adapter, native OAuth status, route, catalog evidence,
or an optional capability is unavailable, continue with ordinary in-host
Prompt it staffing. Do not block the brief, substitute a model/provider, or
claim an equivalent route. When the user requires a particular lane and it is
not ready, represent qualification or repair as a scoped dependency with an
owner and acceptance evidence; do not silently drop the requirement.

## Build the task profile from evidence

Use facts discovered during Prompt it research rather than model names or
marketing tiers. Record the task's actual requirements:

- review or execute mode, and the requested write authority;
- connector/MCP and hard host capabilities that the subtask requires;
- behavioral capabilities such as ambiguous planning, architecture, scoped
  implementation, debugging, large-repository comprehension, UI judgment,
  SQL/data investigation, review precision, or long-horizon reliability;
- task-fit band, context/output estimate, and quality/eval floor;
- any applicable repository governance, data-boundary, or worktree limits;
- `best-fit` or `cost-optimized` policy, plus explicit developer/user
  preferences and stated usage or surplus constraints, such as
  `prefer=claude` or `avoid=codex`.

At planning time, inspect the current runtime and documented lane inventory for
exact available model names and their supplied capability descriptions. Include
qualified currently available Anthropic/Claude models in the same task-relative
rubric as Codex models, and record the description evidence and exact assignment
reason. A provider brand or model name alone proves neither task fit nor
independent-review suitability.

Treat a preference, usage/surplus constraint, or extra-usage statement as a
visible input only. Never inspect quota pressure or provider availability from
an account. Never state or imply that a Claude, OpenAI, or GLM model is
universally equivalent to a named tier; task-relative reviewed evidence must
meet the stated quality floor.

## Hard gates before ranking

A side-lane candidate is eligible only when all of these are true:

1. Its exact `host + mode + provider + model` route is configured and the
   transport protocol is verified.
2. Its own worker host satisfies every required connector/MCP and operational
   capability; cross-host connector parity is never assumed.
3. It has current reviewed task-fit and behavioral-capability evidence,
   authority eligibility, and—when using `cost-optimized`—an applicable
   reviewed cost basis.
4. It clears the declared quality floor and preserves the applicable
   governance/worktree/data-boundary constraints.

GLM is a stricter explicit gate: include only the fixed `glm-5.3` model when the
user enables GLM for that staffing decision; never propose another GLM model or
fallback. Its prepaid flat-rate subscription has zero marginal task cost while
available, so it may rank cheapest only after its exact route, Claude
worker-host capabilities, behavioral evidence, and quality floor pass.
Do not probe quota with a paid request. A recognized quota-pause response makes
GLM temporarily unavailable and returns to Prompt it; never use GLM or another
model as a silent fallback or third connector identity.

Unknown, stale, unverified, or missing connector/capability/task evidence
excludes a candidate; it does not invite a guess. Missing or stale cost evidence
excludes a candidate from `cost-optimized` and is reported as unknown in
`best-fit`. For `best-fit`, rank only after the hard gates using reviewed
task-fit evidence. For `cost-optimized`, choose the lowest estimated cost only
among the same eligible candidates that meet the quality floor. Included native
OAuth and available prepaid GLM have zero incremental cost. If one native host
is on extra usage and the other is included, prefer the included host only when
it passes the same capability/quality gates. For ordinary work, use a
provider-neutral economical choice: among eligible candidates that satisfy the
task's quality, reasoning, context, tools, host, and authority requirements,
choose the least-cost or most-efficient supported option. Apply explicit
developer/user preferences and stated usage or surplus constraints, and record
the material tradeoff. If reviewed cost evidence is missing, report it as
unknown rather than inventing a price or relaxing another gate. Show exclusions,
assumptions, catalog/evidence timestamps, and the exact route in the brief.

Use official specifications for hard protocol/tool facts. For behavioral
capabilities, preserve typed evidence from reproducible benchmarks, local
evaluations, and aggregated community reports. Repeated independent reports
can strengthen an evaluation hypothesis; preserve contrary reports and
host-harness confounders. Community consensus alone never activates a route or
replaces task-relative local validation.

## Distinguish research helpers and execution lanes

Prompt it may use its own native read-only helpers under its research contract;
those helpers are not Side Lane routes. Keep the coordinator responsible for
scope-shaping research, architecture, evidence evaluation, and synthesis.

An external Side Lane research helper uses `review` mode only. It may gather a
bounded source set, investigate a bounded subsystem, or answer a targeted
second-opinion question. Review mode has no MCP/connectors, secret access, or
write authority, so connector-backed research stays with an authorized
coordinator or qualified native helper. Never change a research task to
`execute` merely to obtain a connector.

An external `execute` lane is an implementation worker proposed for after the
execution brief is approved. GLM remains execute-only and keeps its separate
explicit enablement and one-run approval gates; research-team authorization
does not enable GLM, provider-key use, new spend, or execute mode.

## Build the complete task graph

Map each requested outcome and success criterion to at least one task before
selecting workers. Split shared contract decisions from the independent work
they enable; do not use shared architecture as blanket evidence against
parallel or delegated downstream tasks. Keep tightly coupled edits with one
owner, and do not manufacture tasks or agents for small work.

Give every task a stable ID and record all of these fields in the graph row or
linked node details:

- objective and covered outcome;
- prerequisite task IDs;
- relevant inputs and source provenance;
- concrete output and handoff location;
- owner/role, exact executor, and task-fit selection evidence;
- needed tools, mode, capabilities, and authority;
- affected files or exclusive worktree ownership when applicable;
- completion and acceptance evidence.

Use the same IDs in the dependency graph, node contracts, and staffing rows.
Expose fan-out, integration and independent review gates, reject cycles and
conflicting ownership, and state who accepts each prerequisite before a
dependent task becomes ready. Missing readiness for a user-required lane is a
scoped dependency; an unavailable optional lane does not block a usable
in-host graph.

## Staffing, research authorization, and approval

Treat a recommendation as input to one or more proposed task rows, never as
authorization. Each affected row must identify:

- coordinator host and exact worker host/provider/gateway/model;
- review or execute mode, dedicated worktree intent for execute, and required
  capabilities/connectors;
- policy, per-host included/extra state, quality floor, operational and
  behavioral evidence, cost assumptions, and reasons for excluded candidates;
- the coordinator's review/acceptance responsibility.

Before execution-brief approval, an external lane may be dispatched only when
the user has explicitly authorized a bounded external research team and the
assigned task is a qualified `review` route for data gathering, source
investigation, or an independent second opinion. Recheck the exact route's
current worker-host capabilities and task-fit evidence before dispatch; a
recommendation snapshot is not lasting readiness. Existing session
authorization counts: record its scope, exact route, question, inputs, output,
and stopping condition, then reuse it instead of asking again. If that authority
is absent, leave the external research task proposed in the brief and continue
with coordinator or native research.

That bounded authorization also permits the runner's required disposable
review worktree and captured result artifacts for the approved research task.
It does not permit implementation edits, an execute worktree, or unrelated
files before execution-brief approval. Preserve the result artifacts as source
provenance for the brief and let the runner audit and remove the disposable
review worktree under normal review governance.

Use a second opinion only when it has a distinct question and decision value,
justified by uncertainty or non-determinism, impact or irreversibility, material
disagreement risk, or an explicit acceptance gate. Provider diversity alone is
not a reason. Supply primary sources and assumptions and, when practical, do not
lead with the coordinator's preferred answer. Preserve corroboration,
disagreement, missing evidence, and the coordinator's resolution. An original
author's explanation is useful rationale but does not count as independent
review.

All implementation dispatch waits for execution-brief approval. External
research beyond the recorded scope requires additional specific authorization,
which may be obtained during research; reuse any existing authorization that
already covers the exact run. Use the shared `side-lane` skill from the
originating host and dispatch only ready tasks whose prerequisites the
coordinator has accepted. A failed task blocks its dependents while unrelated
approved tasks may continue. A later missing route or failed lane returns for a
staffing decision; it never silently reroutes, falls back, or changes the
primary coordinator.
