# Governed Side Lane

Governed Side Lane routes an approved review or implementation task to an exact
native Codex, native Claude, or explicitly configured GLM worker. Every run
uses a dedicated Git worktree and a checked-in canonical safety contract.

This project is maintained independently by
[Marcos Athanasoulis](https://github.com/marcosathanasoulis). It requires no
employer-specific account, repository, configuration, credential, or
convention.

> Public preview: the repository and direct-install marketplace are available.
> Signed beta releases and curated marketplace submissions are still pending.

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

Codex marketplace installation will be documented after validation against the
public Codex marketplace ingestion path. The standalone skill/source install
remains supported independently.

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
