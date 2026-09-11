---
name: side-lane
description: Route an approved review or implementation task to an exact native Codex, native Claude, or explicitly configured GLM lane, including Prompt it staffing and explicit spend routing.
---

# Side lane

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

For Prompt it staffing, inspect the current runtime and documented lane
inventory for exact available model names and supplied capability descriptions.
Compare qualified Anthropic/Claude and Codex models with the same task-relative
rubric and record the exact assignment reason; provider brand alone is not
capability or reviewer-independence evidence.
For ordinary work, choose the least-cost or most-efficient eligible model that
meets the task's quality, reasoning, context, tools, host, and authority needs.
Honor explicit developer/user preferences and stated usage or surplus
constraints, record material tradeoffs, and never probe quotas or invent cost
when reviewed evidence is missing. Provider diversity alone does not justify an
independent second opinion; require a distinct question and decision value based
on uncertainty, non-determinism, impact, irreversibility, material disagreement
risk, or an explicit acceptance gate.

GLM is optional and enters staffing only when the user explicitly enables it.
Its only selectable model is the fixed `glm-5.3`; never propose another GLM
model or fallback.
It may appear as configured inventory before enablement; exclude it from
staffing until the explicit GLM gate passes.
It is execute-only: review mode cannot use a provider key because canonical
review governance forbids secret access.
It uses an exact configured provider/gateway and the user's prepaid flat-rate
subscription, so its marginal task cost is zero while available. It can rank
cheapest only after the same worker-host capability, task-evidence, and quality
gates. Before retrieving a credential value or launching, obtain explicit approval for that
key-backed run and pass the compatibility flag `--approve-billable-route`.
If GLM reports its temporary quota pause, stop and return for a new exact route;
never retry or fall back silently.
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

Prompt it may call `side-lane recommend` when installed and configured. If it is
missing or returns no eligible route, Prompt it continues on the originating
host. The coordinator stays fixed while Codex and Claude worker hosts are
qualified independently against their own OAuth, connectors, and tools.
Recommendations are capability/evidence gated and never dispatch a lane.
Coordinator origin does not select a provider: Codex-origin staffing may
consider configured native Claude and fixed `glm-5.3` on its Claude worker;
Claude-origin staffing may consider configured native Codex and the same
explicitly enabled fixed GLM route on its Claude worker.
During Prompt it research, the runner may launch a qualified review lane only
when the user has explicitly authorized that bounded external research team;
generic Prompt it consent and recommendation output do not authorize launch.
Review governance still forbids MCP/connectors and secrets. Execute lanes,
including GLM, retain their separate approval gates.

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
A connected MCP server is callable in a headless execute lane only when a
granted capability allowlists its tools: pass `--capability gitnexus` and/or
`--capability codegraph` when the worker should query those code graphs.
Only their read-only query tools are granted; GitNexus index mutation
(`analyze`, `clean`, `group_sync`, non-dry-run `rename`) never is.

## AGENTS.md linkage

Root `AGENTS.md` must contain a line with a Markdown link to root `CLAUDE.md`
in one of two accepted forms (verbatim wording may vary as long as the
required words are present):

- "You **must** read [CLAUDE.md](./CLAUDE.md); it is the authoritative source
  of truth."
- "**[`CLAUDE.md`](./CLAUDE.md) is the source of truth for this repo's
  rules.**"

Links on such a line may only point at root `CLAUDE.md`.
