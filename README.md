# Governed Side Lane

Governed Side Lane allows you to work within one coding agent app—Codex or
Claude Code—and have that agent coordinate a specific task on another agent. You
stay in the conversation and workflow you already have open instead of
switching apps and rebuilding the context midway through the work.

This is useful when you want an easy second opinion, a review from a different
model, or help implementing part of a larger task. It can also help avoid extra
usage costs: if you know one product is in extra usage while another still has
included capacity, you can tell your current agent to use the other one. The
choice is always made by you; Governed Side Lane does not inspect your account,
check how much usage you have left, or switch providers automatically.

The skill works on its own, and it also works with
[Prompt it](https://github.com/marcosathanasoulis/prompt-it). Prompt it helps
turn a substantial request into a researched, approved plan and recommends
which model should do each part. Governed Side Lane can then route an approved
review or implementation task to the agent you selected.

You choose the exact host, model, and mode. The runner gives the worker the same
checked-in repository rules and durable context, then isolates its work in a
dedicated Git worktree.

This project is maintained independently by
[Marcos Athanasoulis](https://github.com/marcosathanasoulis). It requires no
employer-specific account, repository, configuration, credential, or
convention.

> Public preview: the repository and direct-install marketplace are available.
> Signed beta releases and curated marketplace submissions are still pending.

## Why it is useful

- **Stay in one coordinator app.** Keep the main conversation in Codex or
  Claude Code and ask it to route a clearly scoped task to the other agent.
  The result comes back to the coordinator instead of requiring you to recreate
  the task and context by hand in another app.
- **Get genuine second opinions.** Ask Codex to review Claude's approach, ask
  Claude to review Codex's change, or compare agents on the same question under
  the same repository rules.
- **Use included subscription capacity deliberately.** Native Codex and Claude
  lanes use the OAuth session for the corresponding signed-in product. If you,
  the human, know one product has entered extra usage or you want to preserve
  its remaining allowance, explicitly choose the other native lane. The tool
  does not inspect quotas, billing, or usage and never switches agents
  automatically.
- **Delegate without agents colliding.** Every review or implementation gets a
  uniquely named branch and worktree rather than editing the coordinator's
  checkout.
- **Keep routing auditable.** Host, provider, model, mode, lane name, and
  capabilities are explicit. Missing authentication and unavailable routes
  fail closed rather than falling back to a different or potentially billable
  provider.

For example, while working in Claude Code you can request a read-only Codex
review of a proposed change. While working in Codex you can ask Claude for an
alternative design or delegate an implementation lane. When you know Codex is
in extra usage, you can instruct the coordinator to use your included Claude
subscription capacity instead—or vice versa. Optional GLM routing is separate,
explicitly configured, key-backed, and potentially billable.

## How a side lane works

1. You give your current Codex or Claude coordinator a task and explicitly
   choose a review or execute lane and its target agent.
2. The runner validates the target repository's `AGENTS.md` and authoritative
   `CLAUDE.md`, creates a dedicated branch/worktree, and builds the worker prompt
   from the approved task plus the canonical lane contract.
3. The selected native agent runs through its own installed CLI and OAuth
   session. Durable project facts come from checked-in repository context;
   private product memory and connectors are not treated as shared truth.
4. A review lane returns structured findings and removes its clean temporary
   worktree. An execute lane preserves its branch and worktree so the
   coordinator can inspect, test, and decide whether to integrate the result.

## What it does

- Read-only review lanes disable MCP/connectors, secrets, and writes.
- Execute lanes retain normal host tools but can edit, test, commit, and push
  only their dedicated lane branch.
- Native Codex and Claude routes use each host's own OAuth session.
- Optional GLM is execute-only, explicit, key-backed, and never a fallback.
- Host-private memory and connectors are never presented as synchronized.

A worktree isolates Git edits; it is not an operating-system sandbox. Execute
lanes run with the same local-user authority as their selected host.

## Prerequisites

- Git
- Python 3.10 or newer
- Codex CLI and/or Claude Code, already signed in for native routes
- A target Git repository with root `AGENTS.md` and `CLAUDE.md`

The target `AGENTS.md` must require and authoritatively link the root
`CLAUDE.md`, for example:

```markdown
# Agent entry point

You **must** read [CLAUDE.md](./CLAUDE.md); it is the authoritative source of
truth for repository rules.
```

The target owns the contents of `CLAUDE.md`. No organization-specific template
or global configuration is downloaded by this package.

## Claude marketplace installation

From Claude Code:

```text
/plugin marketplace add marcosathanasoulis/governed-side-lane
/plugin install governed-side-lane@marcos-side-lane
```

Run `/reload-plugins` when Claude requests it.

## Codex marketplace installation

From a terminal with a current Codex CLI:

```bash
codex plugin marketplace add marcosathanasoulis/governed-side-lane --ref main
codex plugin add governed-side-lane@marcos-side-lane
```

Start a new Codex task after installation if plugin discovery is cached.

## Source installation

```bash
git clone https://github.com/marcosathanasoulis/governed-side-lane.git
cd governed-side-lane/plugins/governed-side-lane
./scripts/install.sh install codex   # or claude, or both
./scripts/install.sh check codex
```

Windows PowerShell:

```powershell
git clone https://github.com/marcosathanasoulis/governed-side-lane.git
Set-Location governed-side-lane\plugins\governed-side-lane
.\scripts\install.ps1 install codex # or claude, or both
.\scripts\install.ps1 check codex
```

The standalone skill/source install remains supported independently of either
marketplace.

## Usage

List exact routes before choosing one:

```bash
side-lane list
side-lane auth-status --host codex --json
side-lane auth-status --host claude --json
```

Native Claude review:

```bash
side-lane run --host claude --mode review \
  --provider claude --model claude-sonnet-5 \
  --lane-name pr-review --repo "$PWD" \
  --prompt "Review this change and report material risks."
```

Native Codex implementation:

```bash
side-lane run --host codex --mode execute \
  --provider openai --model gpt-5.6-terra \
  --lane-name parser-worker --repo "$PWD" \
  --capability workspace-write \
  --prompt-file /path/to/approved-task.md
```

GLM is optional. Configure its documented credential locally and explicitly
approve each key-backed run; never put the key on a command line.

## Development

From `plugins/governed-side-lane`:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q side_lane scripts tests
bash -n scripts/install.sh
```

From the repository root:

```bash
python3 scripts/validate_public_package.py
```

See [SECURITY.md](SECURITY.md) before reporting a vulnerability and
[CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change.

## License

Apache License 2.0. See [LICENSE](LICENSE).
