---
name: side-lane
description: Route an approved review or implementation task to an exact native Codex, native Claude, or explicitly configured GLM lane, including Prompt it staffing and explicit spend routing.
---

# Side lane

Use the [bundled runner](../../bin/side-lane), resolving that link relative to
this `SKILL.md` and invoking the resulting absolute path. On Unix-like hosts it
is executable directly; on Windows invoke it with the configured Python 3
launcher. Do not install software, modify global instructions, or look for a
private source checkout when the bundled runner is present.

This skill is distributed to both Codex and Claude Code. Each product loads its
own skill wrapper, CLI, OAuth session, and connectors; neither product borrows
the other's identity or configuration.

Before routing, run `side-lane list` and select an exact host, provider, gateway,
and model. Native Codex/OpenAI and Claude routes use the selected host's existing
signed-in OAuth subscription. If OAuth is absent or expired, stop and offer that
host's sign-in/refresh command. Never silently substitute a model, provider,
gateway, or paid key route.

GLM is optional and enters staffing only when the user explicitly enables it.
It is execute-only: review mode cannot use a provider key because canonical
review governance forbids secret access.
It uses an exact configured provider/gateway and the user's prepaid flat-rate
subscription, so its marginal task cost is zero while available. It can rank
cheapest only after the same worker-host capability, task-evidence, and quality
gates. Before credential lookup or launch, obtain explicit approval for that
key-backed run and pass the compatibility flag `--approve-billable-route`.
If GLM reports its temporary quota pause, stop and return for a new exact route;
never retry or fall back silently.
The default packaged GLM gateway is direct Z.AI; OpenRouter is neither required
nor packaged as a default developer route.

Both modes require `--lane-name <name>` and use a dedicated worktree. Use
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

For durable context that must survive movement between direct Codex, direct
Claude Code, and side lanes, follow the installed pointer to
`config/agent-context.md`. Host-private memory and connector sessions are hints,
not synchronized authoritative project memory.
