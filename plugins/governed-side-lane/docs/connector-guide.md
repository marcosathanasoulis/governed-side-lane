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
