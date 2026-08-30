---
name: prompt-it-side-lane-routing
description: Optionally add deterministic, capability-gated side-lane recommendations to a Prompt it staffing brief without changing the originating coordinator or dispatching work.
---

# Prompt it side-lane routing

Use this companion only while preparing a Prompt it staffing proposal. It is
not a replacement for Prompt it, a provider picker, or an execution command.
The normal Prompt it workflow remains fully usable when `side-lane` is not
installed or has no qualifying route.

This is an optional integration for users who already have a compatible
Prompt-it workflow. It is not required to use the core side-lane skill. When it
applies, use the [bundled runner](../../bin/side-lane), resolving the link
relative to this `SKILL.md`; never require a private checkout or
organization-specific global configuration.

## Keep the coordinator fixed; qualify each worker host

Determine the originating coordinator host before considering a side lane:

- A Codex-origin task keeps its Codex coordinator. A proposed Claude worker
  uses Claude Code's OAuth and Claude connector identity.
- A Claude Code-origin task keeps its Claude coordinator. A proposed Codex
  worker uses Codex's OAuth and Codex connector identity.

The runner may launch the exact other native host as a worker, but it never
moves the coordinator or borrows connector identity. Probe each candidate
worker host independently and exclude it when that host lacks a required
connector, tool, repository capability, or authority. GLM has no independent
connector session; it uses its selected Claude worker host.

## Optional, presence-only discovery

If the shared runner is already installed, it may be queried through its
documented `check-capabilities` and `recommend` commands. Supply the exact
coordinator host, requested mode, repository, required operational and
behavioral capabilities, task-fit band, quality floor, declared token budget,
per-host cost state, and any user-declared preference. Native OAuth defaults to
included subscription usage. When the user says a host is on extra usage,
record only that statement (for example Claude `extra-usage`, Codex
`included-oauth`). Ask `side-lane recommend --help` for the installed command
shape; do not invent runner flags or pass credentials.

This discovery is deterministic and presence-only. It must never read or infer
provider quotas, consumer-product usage, billing, API keys, or credential
values. It must not install or configure the runner, enable a connector, make a
paid request, or dispatch a lane.

If the executable, host adapter, native OAuth status, route, catalog
evidence, or required connector is unavailable, continue with ordinary in-host
Prompt it staffing. Do not block the brief, substitute a model/provider, or
claim an equivalent route.

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
- `best-fit` or `cost-optimized` policy, plus an explicit user-declared
  preference such as `prefer=claude` or `avoid=codex`.

Treat a preference or extra-usage statement as a visible input only. Never
inspect quota pressure or provider availability from an account. Never state or imply
that a Claude, OpenAI, or GLM model is universally equivalent to a named tier;
task-relative reviewed evidence must meet the stated quality floor.

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

GLM is a stricter explicit gate: include it only when the user enables GLM for
that staffing decision. Its prepaid flat-rate subscription has zero marginal
task cost while available, so it may rank cheapest only after its exact route,
Claude worker-host capabilities, behavioral evidence, and quality floor pass.
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
it passes the same capability/quality gates. Show exclusions, assumptions,
catalog/evidence timestamps, and the exact route in the brief.

Use official specifications for hard protocol/tool facts. For behavioral
capabilities, preserve typed evidence from reproducible benchmarks, local
evaluations, and aggregated community reports. Repeated independent reports
can strengthen an evaluation hypothesis; preserve contrary reports and
host-harness confounders. Community consensus alone never activates a route or
replaces task-relative local validation.

## Staffing and approval

Treat a recommendation as one proposed row in the Prompt it staffing table,
never as authorization. The row must identify:

- coordinator host and exact worker host/provider/gateway/model;
- review or execute mode, dedicated worktree intent for execute, and required
  capabilities/connectors;
- policy, per-host included/extra state, quality floor, operational and
  behavioral evidence, cost assumptions, and reasons for excluded candidates;
- the coordinator's review/acceptance responsibility.

Prompt it approval remains required before any external lane is dispatched.
After approval, invoke the shared `side-lane` skill from the originating host.
A later missing route or failed lane returns to the user for
a staffing decision; it never silently reroutes, falls back, or changes the
primary coordinator.
