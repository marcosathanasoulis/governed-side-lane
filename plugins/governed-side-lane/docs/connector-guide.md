# Connector guide

*Setup guidance reviewed September 11, 2026. Connectors are optional and
host-specific. Do not install, sign in, enable, or purchase one merely to use
Governed Side Lane.*

Use a connector when the task needs its information or action surface. A model's
vision, an API key, or a connector name in configuration is not proof that the
worker can complete the task. The selected worker host owns its own connector
configuration and sessions; Codex and Claude do not share them.

## Before an execution task

For the exact host, repository, mode, and route:

1. Confirm the connector is configured on that worker host.
2. Confirm the connector is visible to the lane worktree and the required tools
   are granted in execute mode.
3. Confirm its task capability with a bounded authorized exercise and verify
   the resulting state.
4. Keep separate the ability to access a system from authorization to change
   it. Existing task permissions still control every external write.

`side-lane check-capabilities` is presence-only: it does not launch a browser,
read a secret, inspect an account, or prove write permission. Review mode hides
all MCP/connectors, so a review route cannot qualify browser operation or a
connector-backed write.

## Choose the connector by task

| Task | Suggested connector | Setup source | What still needs proof |
| --- | --- | --- | --- |
| Browser tests or DOM/accessibility navigation | [Playwright MCP](https://github.com/microsoft/playwright-mcp) | The official project documents host-client setup and browser options. | A configured server is only presence evidence. Run a bounded navigation/action/final-state check on the selected execute host. Browser work needs a browser connector; vision alone is insufficient. The current Playwright capability work is not a claim that browser execution is ready. |
| Native GUI or screenshot-driven task | The worker host's native computer-use surface | [OpenAI computer-use guidance](https://developers.openai.com/api/docs/guides/tools-computer-use) describes an API tool loop, not a Side Lane connector. | Screenshot ingestion, action executor, scrolling/modals, recovery, and final-state verification on the selected host. Do not transfer a cloud API claim into local host readiness. |
| Repository issues, pull requests, or GitHub context | [GitHub MCP server](https://github.com/github/github-mcp-server) | GitHub maintains the server and its setup material. | Exact repository access and each proposed write. A signed-in GitHub connector does not authorize messaging, merging, or changing project settings. |
| Current library/API documentation | [Context7](https://context7.com/docs/resources/developer) | The official developer guide documents the MCP service and its API-key setup. | That the selected host has it configured and that the retrieved source covers the required version. Prefer primary vendor documentation for decisions with high consequence. |
| Deep codebase exploration or impact analysis | [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | Its published CLI/MCP package builds a local repository index. | Index freshness and that it covers the lane worktree commit. Side Lane permits only its read-only query tools on Claude-host execute routes. |
| Implementing from a design file | [Figma MCP](https://developers.figma.com/docs/figma-mcp-server/) | Figma documents remote and desktop server setup and design context. | Access to the intended file/node and the required direction of access. Read design context does not authorize canvas changes; confirm supported client and task permission before a write. |
| Database investigation | The database's official driver/connector for the target system | Use the database vendor's setup and least-privilege guidance. | The exact database, identity, and read scope. Side Lane execution is limited to read-only queries and metadata inspection. |
| A task-specific SaaS workflow | That service's official connector or API | Follow the service's own authentication and scope documentation. | Named object, recipient, and write authority. Do not add a general workflow connector preemptively. |

## Per-run MCP registration (`--mcp-config`)

A coordinator may deliver one remote MCP registration into an execute lane
per run instead of pre-configuring a host:

```bash
side-lane run --host <claude|codex|devin> --mode execute \
  --capability aws-read \
  --mcp-config /path/to/run-mcp.json \
  --lane-name <lane> --prompt-file <task>
```

The run file is a JSON object with exactly one key `mcpServers`, each entry
`{"type": "http", "url": "https://…", "headers": {"Authorization":
"Bearer ${ENV_NAME}"}}` — the credential is referenced by env NAME only.
Validation (the single source is `side_lane/mcp_run_config.py`) rejects
literal credentials, stdio entries, dirty URLs, and plaintext HTTP outside
the exact loopback literals (`127.0.0.1`, `localhost`, `::1`). Every
declared server name must map from a capability also passed with
`--capability` (today `aws-read` → the server named `aws`); there is no
server-wide wildcard and no blanket `mcp__*` grant. Each referenced env name
must already be present and non-empty in the environment the worker child
will run with, or the run fails closed before launch. `--read-root` is
independent: it widens file reads only and interacts with MCP delivery not
at all. Review mode rejects the flag outright.

A declared server name that the chosen host already registers in any scope
aborts the run before any model starts — same-name merge precedence is not
established, so an existing registration and its auth are never silently
overwritten. Only server names are read from host registration files, never
values.

Proof boundaries, established per host (not assumed from one host's JSON
conventions): Claude Code's `${ENV}` header expansion is live-verified
against a local mock MCP server; the Codex override shape
(`mcp_servers.<name>.url` + `bearer_token_env_var`) is CLI-accepted, and the
Codex host accepts only an exact Bearer scheme — any other Authorization
scheme fails closed rather than being converted; Devin accepts the file
shape, but whether it expands `${ENV}` inside header values is UNVERIFIED,
so a Devin delivery can fail visibly at the bridge (401) and must then be
reported as that exact state — never labeled success and never generalized
into "all hosts work" from config acceptance alone. In every case a
registration is presence evidence only: authentication and a live permitted
read stay unproven until the worker observes the exact granted tool names.

## Host and authority rules

Execute workers run locally in their dedicated Git worktree under the user
identity. The worktree separates edits; it does not sandbox the operating
system, filesystem, network, or local tools. That local authority does not
expand the assigned task or make external changes pre-approved. Keep the
existing explicit task permission and review-mode boundaries in force.

For a connector-backed request, record the worker host, connector/server name,
configuration state, evidence date, task capability, and the specific approved
action. Treat `configured`, `present`, `available`, and `authorized` as
different facts. If any one is missing, keep the route a candidate or conduct a
separately authorized bounded qualification; never silently substitute a
different connector, host, or cloud agent.
