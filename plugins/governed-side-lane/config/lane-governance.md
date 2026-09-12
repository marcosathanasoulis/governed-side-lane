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
- Personal host memory, user-global instruction files, and hooks are not shared
  lane memory and must not be assumed to exist.

## Review mode

- Perform read-only review, investigation, or design critique using only the
  explicitly enabled read tools. Do not edit, patch, commit, push, deploy,
  mutate an external system, use MCP/connectors, or access secrets.
- Return findings to the coordinator; do not make final product decisions.

## Execute mode

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
- Code-graph connectors are read-only. With the `gitnexus` capability, call
  `list_repos` first and report the indexed path, branch, and commit against
  the lane worktree HEAD; treat a mismatch as stale or partial coverage. Never
  run `analyze`, `clean`, `group_sync`, or a non-dry-run `rename`, and never
  register a lane worktree as an index. With the `codegraph` capability, the
  checkout-local graph may rebuild its own ignored database and nothing else.
- Stop and report when an action exceeds these boundaries or its authority is
  uncertain.

## Execute tool allowlist

The Claude host adapter renders this section, and only this section, into
`--allowedTools` / `--disallowedTools` for execute lanes. Review lanes never
receive an allowlist. Each subsection names the capabilities that unlock its
rules; `always` applies to every execute lane. Rules are ordinary developer
commands; nothing here may match deploy, IAM, credential, cloud, merge, or
release tooling, which stay forbidden by the Common rules above.

This allowlist is an approval boundary for a headless session, not a security
boundary. Execute lanes run with the same-user authority the Common rules
describe and without an operating-system sandbox; an allowed interpreter or
package runner (`python3`, `node`, `npx`, `uv`) can in principle run anything
the user can, including a push that the `git-push` capability did not grant, and the
lane process necessarily holds its own provider credential in its environment.
The controls are the injected rules above, the audited lane branch, and the
coordinator's review of the resulting diff before anything is merged or
pushed onward. Do not describe the allowlist as preventing those actions.

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
- `Bash(python3 *)`
- `Bash(python3.11 *)`
- `Bash(python3.12 *)`
- `Bash(pytest *)`
- `Bash(bash -n *)`
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
- `Bash(find *)`
- `Bash(sed *)`
- `Bash(wc *)`

- `Bash(echo *)`
- `Bash(pwd)`
- `Bash(which *)`
- `Bash(env)`
### playwright

- `WaitForMcpServers`
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

### codegraph

- `mcp__codegraph__find_symbol`
- `mcp__codegraph__find_callers`
- `mcp__codegraph__find_callees`
- `mcp__codegraph__find_importers`
- `mcp__codegraph__neighbors`
- `mcp__codegraph__impact_of`
- `mcp__codegraph__path_between`
