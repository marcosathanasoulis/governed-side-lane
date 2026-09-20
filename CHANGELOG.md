# Changelog

## 0.4.29 - 2026-09-20

- Gate strict per-run MCP integration on explicit host-native `strict_mcp_support`:
  `build_command` emits `--strict-mcp-config` only when the worker declares support,
  and unsupported strict bundles fail closed before inference,
  including providerless paths. Native execute and review behavior are unchanged.
- Thread the actual worker `cwd` and child environment from `_launch_worker` into
  `_check_strict_mcp_support`, so worker-context support detection is evaluated
  against the runtime values the host session will see, not the coordinator's
  pre-scrub environment.
- Harden the per-run MCP runtime directory: `_secure_runtime_directory` rejects
  symlinked targets and immediate parents, forces `0700` mode, and writes files
  atomically with `0600`. Successfully created per-run files are cleaned up;
  the runtime directory is removed when empty.
- Preserve canonical server mapping, per-run config conflict detection, and native
  execute/review lane semantics.
- Add regression coverage for symlinked runtime paths, preexisting 0755 modes,
  worker context probing, and strict bundle fail-closed behavior.

## 0.4.28 - 2026-09-20

- Keep already-approved delegated execution instructions last after read-root
  guidance so workers execute their assignment without restarting coordination.
- Isolate routed workers from the known coordinator Superpowers plugin in
  ephemeral settings, preserving other plugins, hooks, MCP, and authentication.
  Native execution and review behavior remain unchanged.

## 0.4.27 - 2026-09-20

- Add optional metadata-only `--measurement-file` for execute runs. The runner
  validates its scalar types and vocabulary before launch, then atomically
  preserves an immutable assignment beside the run audit before invoking the
  worker. Terminal audits link the assignment; interruptions retain it.
- Existing callers remain explicitly unmeasured. Execution success does not
  imply coordinator acceptance or change delegation scoring.

## 0.4.26 - 2026-09-20

- Add explicit `--report-only` Claude execute runs with a required finite,
  positive USD budget. A per-run Stop hook checks the actual worktree report
  and requests one correction within the same invocation when it is missing
  or invalid. The correction shares the original timeout and budget; it never
  starts another worker or resumes a separate session.
- Validate report-only artifacts before treating delivery as successful or
  publishing a branch. Unsupported hosts and review mode reject the option;
  ordinary execution retains its existing contract.

## 0.4.25 - 2026-09-20

- Harden native Devin `env` command handling: only literal task-scoped
  assignments followed by an already-granted command are admitted; nested
  launchers, shell forms, redirections, and assignment-only invocations fail
  closed. Execute prompts now document the supported `env NAME=value command`
  form.

## 0.4.24 - 2026-09-20

- Bound each native `Read` call in a routed Claude execute lane. A per-run
  `PreToolUse` hook (`side_lane.routed_read_pagination`) rewrites an
  unbounded read into an explicit `offset`/`limit` line range (200 lines by
  default, overridable per run) and names the next range as continuation
  guidance, so the remainder of the file stays reachable. The hook emits no
  permission decision — every call continues through the normal permission
  flow — and no capability, grant, or lane rule changes. Smaller explicit
  reads, non-text paths (`.pdf`, `.ipynb`, images), other tools, and
  malformed events pass through untouched; the hook config and merged
  settings are written 0600 into the disposable per-run config directory, so
  inherited `PreToolUse` entries survive and the real user home is never
  modified.

## 0.4.23 - 2026-09-19

- Add the `asana-read` and `drive-read` execute capabilities for the fixed
  local `cm-services` MCP server the coordinator provisions into the worker
  host's user-global config (controlled HOME) under the worker's own account:
  - Canonical governance grants exact per-tool IDs only —
    `mcp__cm-services__asana_get_task`, `asana_get_project`,
    `asana_list_project_tasks` under `asana-read` and
    `mcp__cm-services__drive_file_info`, `drive_sheet_tabs`,
    `drive_sheet_get`, `drive_doc_get` under `drive-read` — plus the shared
    `WaitForMcpServers`. The two capabilities share one server but grant
    disjoint tool sets: one grant never unlocks the other, and there is no
    server-wide wildcard.
  - The Claude adapter maps both capabilities to the server name
    `cm-services`, treats it as a user-scope registration (no
    `enabledMcpjsonServers` approval — that setting approves project
    `.mcp.json` entries only), runs the pre-launch `mcp get` readiness probe
    once per run even when both capabilities are granted, and emits a single
    `cm-services` startup instruction telling the worker to wait for the
    server and treat registration as presence evidence only.
  - The Devin adapter admits both capabilities and translates their
    canonical rules into exact `mcp__cm-services__<tool>` permission
    entries; `check-capabilities` reports `cm-services` registration as
    presence evidence only (exact name required, near-miss is
    `name-mismatch`), with same-account provisioning, service
    authentication, and a live read explicitly untested.
  - Per-run `--mcp-config` delivery stays remote streamable-HTTP only:
    `cm-services` is a user-global registration, so no per-run config may
    declare it, and the `aws-read` narrowing is unchanged.

## 0.4.22 - 2026-09-19

- Review repairs to the per-run MCP delivery and lane governance:
  - The Claude adapter now validates per-run credential env references against
    the scrubbed child environment it actually builds (inside
    `_launch_worker`), matching the Codex and Devin adapters. Previously it
    checked the pre-scrub coordinator environment, so a referenced name the
    scrub list removes would have passed validation and failed only at the
    bridge after the model started.
  - The Devin adapter rejects a tracked `.devin/mcp_config.local.json` before
    merging, then excludes the untracked generated file through the
    repository's worktree-safe `.git/info/exclude` mechanism BEFORE the file is
    written. Thus a worker's mid-run `git add -A` cannot stage the generated
    file (URL plus `${ENV}` reference, never a value); untracked preexisting
    config still follows the additive merge/restore path.
  - Lane governance Common rules now state explicitly that a worker is a
    delegated worker on an already-approved, bounded task and must never ask
    "Prompt it?", invoke a coordinator planning or routing skill, or request
    planning approval for the approved scope — it starts the approved task
    immediately and reports missing authority instead of adding a new gate.
  - New regression coverage in `tests/test_mcp_run_config.py`,
    `tests/test_devin_adapter.py`, and `tests/test_worktrees.py`.

## 0.4.21 - 2026-09-19

- Review repairs to the per-run `--mcp-config` delivery (0.4.20):
  - Loopback for plaintext-HTTP run configs is now EXACT literal matching
    (`127.0.0.1`, `localhost`, `::1`); a `localhost.<anything>` DNS name is a
    public host, not loopback, and is rejected.
  - The Codex delivery preserves the auth scheme exactly: only
    `Authorization: Bearer ${ENV}` maps to `bearer_token_env_var`. A `Basic`
    or scheme-less Authorization reference fails closed instead of being
    silently re-labelled Bearer; `McpRunServer.bearer_env()` reports a Bearer
    reference only.
  - The Devin local-scope merge no longer crashes on a valid `{}` file and
    preserves top-level metadata and existing servers while merging.
  - Name-conflict fail-closed before launch on every host: a declared server
    name that any host scope already registers (Claude user/project,
    including a per-project entry for the lane worktree; Codex
    `config.toml`; Devin user/project/local) aborts the run before any model
    starts. Same-name merge precedence is not established for Claude's
    `--mcp-config` or Codex's `-c mcp_servers.<name>.*` overrides, so an
    existing registration and its auth are never silently overwritten or
    shadowed; an unreadable registration file also fails closed. Only
    server NAMES are read from those files, never values.
  - Regression coverage added to the public package tests
    (`tests/test_mcp_run_config.py`), which previously had no MCP run-config
    tests.

## 0.4.20 - 2026-09-19

- Add execute-only `--mcp-config <path>`: per-run MCP server registration
  delivered into the host session from a coordinator-supplied, fully
  validated file (`side_lane.mcp_run_config`). Registration is remote
  streamable-HTTP only, credentials are referenced by env name (never a
  value — literal header credentials, stdio entries, and dirty URLs are
  rejected), and every declared server must map from a granted capability
  (`aws-read` registers the server named `aws`); no server-wide wildcard is
  ever produced. The new `aws-read` capability grants exactly the seven
  read-only tools of the AWS remote bridge allowlist.
- Per-host delivery is additive and ephemeral: Claude loads an ephemeral
  `--mcp-config` file written outside the lane worktree (no
  `--strict-mcp-config`, so existing registrations survive); Codex receives
  additive `-c mcp_servers.<name>.*` overrides (bearer by
  `bearer_token_env_var`); Devin's git-ignored local-scope
  `.devin/mcp_config.local.json` is merge-written into the lane worktree and
  restored afterwards. Referenced env names must resolve in the worker child
  environment or the run fails closed before launch. Review mode remains
  strict no-MCP and rejects the flag. Host support was established per CLI:
  Claude's `${ENV}` header expansion is live-verified against a local mock
  server; Codex's override shape is CLI-accepted; Devin accepts the file
  shape but its `${ENV}` header expansion is unverified — a Devin delivery
  can fail visibly at the bridge and must be reported, never labeled
  success. The audit records server names and the config path only.

## 0.4.19 - 2026-09-19

- Add an explicit routed-provider contract for router-selected upstream pools
  (OmniRoute-style), distinct from the exact-model `identity_contract`.
  Routed providers (`omniroute`, gateway `omniroute-router`) declare a
  `routing_policy_contract` — requested selector, an explicit non-empty
  `allowed_upstream_models` set, verified settings precedence. Exact routes
  reject a routing policy contract and routed routes reject an
  `identity_contract`; existing exact-route validation is unchanged. A routed
  run's streamed attestation must be a non-empty set of actual upstream
  models inside the declared pool (a selector-alias echo is ignored as
  non-evidence); multiple in-pool models are legitimate worker-conversation
  fallback, while missing, unknown, or out-of-pool attestation fails the run
  closed as exit 65. Receipts record the attested upstream model set
  (`attested_models`) and never promote a bare model id — which can collide
  across upstream providers — to an upstream provider-identity claim. The
  public package ships only a disabled illustrative example profile;
  deployment-specific hostnames, credential services, and exact upstream ids
  are private configuration. The qualification harness accepts routed
  trials and reports attestation against the declared pool. Mocked
  translation-boundary stream fixtures cover tool-call round trips; no
  upstream runtime qualification is claimed.
- Execute lanes on every host now receive a pinned worker skill bundle. A
  stdlib-only `skill_bundle` module validates a hash-pinned manifest
  (`skill-bundle/`, vendored Superpowers 6.4.1 under MIT with per-vendor
  provenance) and materializes the five discipline skills into the lane
  worktree's git-ignored scratch directory, appending a compact catalog with
  absolute runtime paths to the worker's task context — at the shared launch
  layer, so codex, claude, and devin lanes behave identically without touching
  user homes, CODEX_HOME, or MCP configuration. Review lanes are unchanged.
  Delivery is fail-closed (manifest, hash, reference, and symlink problems
  abort the run before a worker starts) and is recorded as an additive
  `skill_catalog` audit field. Catalog delivery is not a claim of host-native
  skill discovery or live tool connectivity.

## 0.4.18 - 2026-09-19

- Add execute-only `--read-root` grants for external read-only directories.
  Devin's file-tool default now reads only the lane worktree rather than `**`;
  assignments reading other checkouts need explicit roots. Claude and Codex
  retain their native host filesystem authority.
- Add `slack-read` qualification and scoped Slack read-tool grants. Connector
  registration is presence evidence, not proof of authentication or live access.
- Enable granted project MCP servers for Claude workers and wait for their
  startup before calling tools; review-mode MCP isolation stays unchanged.
- Parse Devin compound shell commands against the configured permission rules
  so permitted command sequences work while denied operations remain denied.

- Update the Side Lane skill discovery trigger to assess optional model lanes at
  the start of an implementation task, not only after an explicit delegation
  request. Installation and route qualification remain optional; a missing
  eligible route is a recorded exception, never silent coordinator execution.

## 0.4.17 - 2026-09-18

- Permit a coordinator-visible reassignment to the one exact, preapproved
  backup after a qualifying availability failure. The runner still executes
  only its requested route; refresh readiness and authority, preserve partial
  work, prevent parallel writers and third routes, and retain fixed
  execute-only `glm-5.3` governance.

## 0.4.16 - 2026-09-18

- `claude-fable-5-1` on the `anthropic` direct route is now `qualification.verified: true`:
  the 2026-09-18 `qualify_claude` trial passed once the trial host ran Claude Code
  2.1.276 (the earlier 2.1.247 attempt returned `400 ... 2.1.251 or newer is required`).
  Exact model attested, X-Api-Key export confirmed. All four Anthropic API-key models
  are now launchable.

## 0.4.15 - 2026-09-18

- The `direct-anthropic` execute route now uses haiku's exact dated id
  `claude-haiku-4-5-20251001`; the API resolves the `claude-haiku-4-5`
  alias to the dated id, which breaks the requested/resolved identity
  contract, so only the dated id is allowlisted (price unchanged at
  $1/$5 per million tokens).
- `claude-opus-5`, `claude-sonnet-5`, and `claude-haiku-4-5-20251001` are
  qualified as of 2026-09-18 via a developer-authorized
  `side_lane.qualification.qualify_claude` paid trial against
  `https://api.anthropic.com` — the `X-Api-Key` path is confirmed end to
  end for transport and local adapter.
- `claude-fable-5-1` remains unverified: Claude Code 2.1.247 returned
  "400 ... does not support this model; version 2.1.251 or newer is
  required" on the 2026-09-18 trial; retry pending a newer CLI.

## 0.4.14 - 2026-09-18

- New `anthropic` provider-key execute route (gateway `direct-anthropic`,
  base URL `https://api.anthropic.com`) for `claude-opus-5`,
  `claude-fable-5-1`, `claude-sonnet-5`, and `claude-haiku-4-5`. Claude Code
  sends `ANTHROPIC_API_KEY` as `X-Api-Key` (first-party key auth) and
  `ANTHROPIC_AUTH_TOKEN` as `Authorization: Bearer` (proxy/OAuth-style), so
  this gateway exports the secret under `ANTHROPIC_API_KEY` only; every other
  billable direct gateway keeps `ANTHROPIC_AUTH_TOKEN` unchanged (source:
  code.claude.com/docs/en/env-vars). The route is execute-only, explicit
  `--approve-billable-route`, requires an identity contract, and stays
  unverified until the first governed run on the cloud worker.
- The `openai-api-key` execute route additionally allowlists `gpt-5.5`,
  `gpt-5.5-pro`, and `gpt-5.3-codex` with the same transport contract as the
  existing ids; Codex CLI support for `-pro` ids is unconfirmed until the
  first run.
- Both routing catalogs now carry the metered-spend rate cards
  `openai-api-list-price-usd-2026-09-18` (OpenAI API list prices for the
  seven `openai-api-key` models) and
  `anthropic-api-list-price-usd-2026-09-18` (Anthropic API list prices for
  the four `direct-anthropic` models), and every `openai-api-key` and
  `anthropic` route references its card through a `external-billable`
  cost model.
- The qualification harness can now qualify the `anthropic` route:
  `claude.FIRST_WAVE_ENDPOINTS` admits `https://api.anthropic.com`, so
  `side_lane.qualification.qualify_claude` can run the developer-authorized
  paid trial (secret injected via the environment, `ANTHROPIC_API_KEY` /
  `X-Api-Key`) that a model needs before `qualification.verified` flips to
  `true`; the catalog keeps `verified: false` with the trial recorded as
  `pending` until that run happens.

## 0.4.13 - 2026-09-17

- Devin adapter: grant the `git -C <lane worktree>` and `git -C .` spellings
  of every allowed git subcommand, so a non-interactive run is not ended by
  Devin's confirmation prompt (gateway run 99e5ec8a). Deny rules unchanged;
  the PreToolUse hook still normalises and blocks.

## 0.4.12 - 2026-09-15

- `EXECUTE_UNSAFE`'s second pattern now matches "deploy" only as a
  verb/imperative ("deploy it/this/the/to/now", "run the deploy", or a
  sentence-initial "Deploy ..."), not as a filename (`deploy.py`), a noun
  ("deployment", "the deploy script", "deploy config/workflow",
  `DEPLOYMENT_TYPE`), or the word inside a path or identifier. A coordinator
  had been refused with "prompt requests an action prohibited in execute
  mode" for merely naming the release script or describing its config; the
  force-push and "merge the PR/branch" alternatives, the other three
  patterns, and the error message are unchanged.
  The internal BE gateway repo's `functions/sideLaneGateway/prompt_governance.py`
  mirrors these regexes verbatim and must be re-synced.
- Follow-up: the sentence-initial `deploy` branch now also carries a negative
  lookahead so a filename/path/identifier immediately following the word
  (`deploy.py`, `Deploy-runbook.md`, `deploy_config`, `deploy/`) is accepted at
  the start of a sentence too, and `that` joins the `deploy it/this/the/to/now`
  alternation.

## 0.4.11 - 2026-09-15

- Every lane worktree is now created with a git-excluded
  `.side-lane-scratch/` directory for throwaway scripts, notes, and
  intermediate output. `git status` and `git add -A` never see it (the entry
  lives in the shared `.git/info/exclude`), it never fails the dirty checks
  around lane creation and disposal, and it is removed with the worktree. The
  canonical governance Common section now directs scratch files there and
  forbids writing outside the lane worktree.
- Devin's PreToolUse policy hook now covers the file-mutating `write`, `edit`,
  and `str_replace` tools in addition to `exec`. A write whose target resolves
  outside the lane worktree is blocked by the hook with a pointer to
  `.side-lane-scratch/` instead of reaching Devin's `accept-edits`
  confirmation, which printed "rejected a tool call that requires
  confirmation" and ended the non-interactive session with exit 0 (run
  aa9331fa, devin/swe-2-high, 2026-09-15, failed `no_report` after a `write`
  to `/tmp/pr1678_sim.py`). The lane worktree path reaches the hook through a
  new `worktree` key in `devin-command-policy.json`.
- An `exec` command with a leading `git -C <lane-worktree>` now matches the
  canonical grants as the bare git command, so
  `git -C <worktree> log --oneline -5` passes `Bash(git log *)` (observed
  blocked in the same run). Any other `-C` target must still match a rule
  literally, and deny rules match through the stripped prefix.

## 0.4.10 - 2026-09-14

- Devin's PreToolUse policy hook now grants an argument-free command under its
  `Bash(<command> *)` rule: `git status` and `git worktree list` matched
  neither `git status *` nor `git worktree list *` (fnmatch requires the
  literal space), so they were blocked as "outside canonical capability
  grants" while `git log --oneline -10` passed. Deny rules such as
  `Bash(git push --force*)` and unrelated commands (`git statusx`, `git push`
  without a grant) behave as before. Observed in side-lane run
  eae6a4055b154009a88ded0ea91aa2dd (devin/grok-4-6-medium, 2026-09-14).

## 0.4.9 - 2026-09-14

- Published from the maintainer's private development repository. The public
  repository is now a publish target: its `main` branch is updated by an
  export that runs this repository's own validator, tests and a leakage scan
  before every push, and release tags remain SSH-signed by the maintainer.
  External pull requests are still welcome and are ported by hand.

## 0.4.8 - 2026-09-13

- Codex lanes (both `native-codex` and `codex-api-key`) run `codex exec --json`
  and capture the final `turn.completed` usage block (`input_tokens`,
  `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`) plus the attested model into the lane result; the
  raw stream still lands in `stdout` with secrets redacted.
- The `codex-api-key` route exports the launcher-read secret as `CODEX_API_KEY`
  as well as `OPENAI_API_KEY`, because Codex CLI 0.154.0 authenticates only
  from `CODEX_API_KEY` (or `codex login --with-api-key`) and ignores
  `OPENAI_API_KEY`. Inherited `CODEX_API_KEY` / `CODEX_ACCESS_TOKEN` are
  scrubbed on every route; `CODEX_HOME` is preserved as a config path.
- Ignore JSONL events whose `type` is not a string instead of aborting the
  lane.

## 0.4.7 - 2026-09-13

- Version-only release. The `v0.4.6` tag was created on `37b687a`, the
  `codex-api-key` merge, before the 0.4.6 version-bump commit landed on
  `main`, so its manifests still read 0.4.5. Published tags are never
  re-pointed; 0.4.7 is the same code with correct version metadata. Skip
  `v0.4.6` when pinning.

## 0.4.6 - 2026-09-13

- Codex adapter accepts a second, config-selected gateway `codex-api-key`
  (`auth_method: provider-key`, `billable: true`, execute-only) for hosts with
  no signed-in Codex session, such as a cloud worker. The launcher-read
  credential reaches the Codex CLI as `OPENAI_API_KEY` (plus `OPENAI_BASE_URL`
  when the provider config sets `base_url`); every other inherited provider
  credential is still scrubbed, the secret is redacted from captured output,
  review mode refuses the route, and the lane result reports the real gateway,
  auth method and billable flag.
- `native-codex` OAuth routes are unchanged and still refuse any secret. The
  shipped catalog does not enable the new route; a downstream overlay adds the
  provider block.

## 0.4.5 - 2026-09-13

- Redact known provider-key fragments and explicitly masked prefix/suffix
  displays in worker stdout/stderr, startup/readiness errors, and qualification
  output before parsing or excerpting. Suppress raw launch exception chaining.
- Preserve nonsecret model/usage diagnostics and existing timeout limits.
- This protects runner-returned output; it cannot retroactively clean host-owned
  transcripts or detect every encoding or arbitrarily short fragment. Never
  intentionally emit credential fragments. Tests use only synthetic keys.

## 0.4.4 - 2026-09-13

- Default all Claude-host and Devin-host worker processes to 30 minutes,
  including configured compatible provider routes; preserve explicit overrides.
- Explain timeout scope and upgrade behavior in the shared Side Lane skill.
- Bump both host plugin manifests, the Claude marketplace entry, and source
  metadata together so installed plugin caches can recognize this release.

## 0.4.3 - 2026-09-11

- Claude-host connector discovery is scope-aware. Only root-level `mcpServers`
  entries of `~/.claude.json` / `~/.claude/settings.json` and the target
  repository's `.mcp.json` count as inventory; per-project entries
  (`projects.<path>.mcpServers`) are keyed by the directory Claude was started
  in and never apply to a lane worktree, so they no longer make a capability
  `present`. `check-capabilities` reports them as `mcp_connectors_out_of_scope`,
  and an exact `gitnexus`/`codegraph` name found only there is `unknown` with a
  basis saying so. `json_mcp_name_scopes` exposes the key path of each
  declaration; values are still never materialized (#22).

## 0.4.2 - 2026-09-10

- `check-capabilities` on the Claude host reports `gitnexus`/`codegraph` as
  `present` only when an MCP server is registered under exactly that name. A server whose name merely
  contains the word (for example `gitnexus-local`) is reported as
  `name-mismatch` with the offending name, because the execute grants target
  the fixed `mcp__gitnexus__*` / `mcp__codegraph__*` namespaces and such a
  server would connect without a single callable tool. The launch gate treats
  `name-mismatch` like any non-present state (#20). Codex lanes inherit their
  MCP servers directly and keep connector-name presence evidence.

## 0.4.1 - 2026-09-10

- The `gitnexus` and `codegraph` capabilities now grant their read-only MCP
  query tools to Claude-host execute lanes. Previously both names were
  declared in `models.json` but unlocked no `--allowedTools` rule, so a
  headless lane connected to the servers yet could not call them. GitNexus
  index mutation (`analyze`, `clean`, `group_sync`, non-dry-run `rename`)
  stays ungranted; Execute mode gains a read-only code-graph conduct rule.

## 0.4.0 - 2026-09-05

- Discover the installed core runner and configured providers in either host direction; preserve wrapper overlays and distinguish presence, task eligibility and consent.

- Select provider-neutral economical qualified routes from current supplied descriptions; GLM remains the explicitly enabled fixed `glm-5.3` route.
- Require a distinct, justified question for independent second opinions; provider diversity alone adds no review requirement.
- Connect Prompt it task graphs to exact, capability-qualified worker assignments and explicit readiness dependencies.
- Support explicitly authorized bounded review research and targeted second opinions before execution-brief approval; generic discovery remains presence-only.
- Preserve review-mode isolation, execute/key-backed approval boundaries and the originating coordinator's ownership. Runtime governance and route catalogs are unchanged.

## 0.3.5 - 2026-09-05

- `ensure_lane_exclusion` upgrades a legacy unanchored `.side-lanes/` entry
  written by 0.3.0-0.3.3 to the root-anchored `/.side-lanes/` in place (and
  drops a duplicate), so nested directories of that name become visible to the
  dirty-checkout check after upgrading.

## 0.3.4 - 2026-09-05

- The `.git/info/exclude` entry for lane worktrees is root-anchored
  (`/.side-lanes/`), so a nested directory of the same name still makes the
  coordinator checkout dirty.
- `git-push (denied)` also covers `--force-with-lease`, `--mirror`, and
  forced refspecs (`+HEAD:main`).
- `tool_policy()` fails closed on any non-blank line in an allowlist subsection
  that is not a rule bullet, instead of silently skipping it.
- Linkage negation adds "under no circumstances", "in no case/way", "by no
  means", "at no time/point"; the module documents that the check validates a
  maintainer-owned file for completeness and is not an adversarial-wording
  defence.
- The `playwright` evidence basis states that host user config may include
  connectors scoped to other projects.

## 0.3.3 - 2026-09-05

- AGENTS.md linkage negation also catches qualified forms ("is not currently
  the source of truth", "is not really authoritative").
- The `git-push` deny rules add `Bash(git push * -f*)` so a trailing short
  force flag (`git push origin main -f`) is denied like the long form.

## 0.3.2 - 2026-09-05

- Project connector discovery is host-specific: a Claude lane reads the
  repository's `.mcp.json`, a Codex lane its `.codex/config.toml`, so a
  connector configured only for the other host is not reported as evidence.
- AGENTS.md linkage also ignores modal negations ("must not treat ... as
  authoritative", "should not consider ... the source of truth").
- `config/lane-governance.md` states plainly that the execute tool allowlist is
  an approval boundary for headless sessions, not a security boundary: allowed
  interpreters can perform actions a capability did not grant, and the controls
  are the injected rules, the audited lane branch, and coordinator review.
- The adapter type-alias test no longer calls
  `typing.get_type_hints` on Python 3.9, where PEP 604 unions in the
  adapter's other annotations cannot be evaluated; it asserts the aliases are
  real `Union`/`Callable` types on every supported version instead.

## 0.3.1 - 2026-09-04

- The execute-lane tool allowlist is now a section of the canonical
  `config/lane-governance.md` ("Execute tool allowlist"); the Claude adapter
  parses and renders that section instead of carrying its own rule lists, and
  the skill points at the section rather than restating it. Malformed sections
  fail closed.
- AGENTS.md linkage ignores negated declarations ("is not the source of
  truth", "never ... authoritative"), which previously satisfied the check.
- `side-lane run --worktree-root <dir>` (or `SIDE_LANE_WORKTREE_ROOT`) places
  lane worktrees outside the governed repository, for repositories whose policy
  forbids nested checkouts (sibling-directory conventions). A relative value is
  anchored to the repository root (`../lanes`). The default stays
  `<repo>/.side-lanes/worktrees` with the `.git/info/exclude` entry; any other
  location inside the repository is refused so a typo cannot create an
  un-excluded nested checkout. The exclude entry is only written for the
  default location.

## 0.3.0 - 2026-09-04

- Execute-mode Claude-host lanes (native Claude and GLM) now receive explicit
  `--allowedTools` rules derived from `--capability` flags: always
  Read/Edit/Write/Glob/Grep; with `shell` or `workspace-write` (or
  `git-push`) the ordinary dev-command set `SHELL_TOOLS`; `git-push`
  additionally allows `Bash(git push *)` and passes `--disallowedTools`
  denying force pushes. Review mode argv is unchanged (no allowlist ever).
  Unknown capability names fail closed. The run summary JSON now includes
  `capabilities`, `allowed_tools`, `disallowed_tools`.
- `.side-lanes/` is added to the coordinator repo's `.git/info/exclude`
  before the dirty-checkout check, so earlier lane worktrees no longer block
  the next launch.
- Codex host: `hosts.host_support_dir` resolves the real directory of the
  `codex` executable; if `codex-code-mode-host` sits beside it (codex-cli
  0.152+ from the desktop app bundle), that directory is prepended to the
  lane child's PATH. `check-capabilities` reports it as `host_support_dir`.
- AGENTS.md linkage now also accepts a "source of truth" line with a
  Markdown link to root CLAUDE.md, in addition to the existing "You **must**
  read [CLAUDE.md](./CLAUDE.md)" form. Links on such lines may only point at
  root CLAUDE.md.
- New capability `playwright` in config/models.json; `check-capabilities`
  reports it present when an MCP server name containing "playwright" is
  configured for the host. Review mode still hides all MCP servers via
  `--strict-mcp-config`; execute mode inherits the user's/project's MCP
  servers, so Playwright is usable in execute lanes.

## 0.2.5 - 2026-09-03

- Sign-in hints for an off-`PATH` executable are quoted for the platform's
  default shell: PowerShell gets the call-operator form (`& 'C:\\...\\codex.exe'
  login`), POSIX shells keep `shlex` quoting. `refresh_command` looks up
  `which`/platform at call time so they can be substituted in tests.

## 0.2.4 - 2026-09-03

- The sign-in hint (`refresh_command` in `auth-status`, and the OAuth preflight
  error) is now built from the resolved host executable. When the CLI was found
  only through an override or a desktop-app bundle, a bare `codex login` could
  not run in the very environment that needed the resolution; the hint now
  quotes the resolved path instead. Bare `codex login` / `claude auth login`
  are kept whenever the resolved path is the one `PATH` yields.

## 0.2.3 - 2026-09-03

- Resolve the native host executable explicitly instead of assuming a bare
  `codex` / `claude` on `PATH`. Lookup order is an explicit
  `SIDE_LANE_CODEX_EXECUTABLE` / `SIDE_LANE_CLAUDE_EXECUTABLE` override (which
  fails closed if set but unusable), then `PATH`, then — for Codex only — the
  CLI shipped inside the Codex / ChatGPT desktop app bundle on macOS. The
  resolved path is reported as `runtime` in `check-capabilities`, used for
  `auth-status` and the OAuth preflight, and appears as `argv[0]` in the run
  summary. A missing executable now fails before a lane worktree is created,
  with an actionable message. Fixes "codex not found" from non-interactive
  agent shells on machines where only the desktop app is installed.

## 0.2.2 - 2026-09-01

- Persist review findings durably: the per-run audit record
  (`<git-dir>/side-lane-runs/<branch>.json`, now schema_version 2) captures
  the lane's stdout and stderr before the disposable review worktree is
  removed, and the run summary returns it as `result_artifact`. A failed
  artifact write aborts the run without disposing the worktree.

## 0.2.1 - 2026-09-01

- Make the AGENTS.md linkage governance error actionable and stop treating
  repeated authoritative links to root `CLAUDE.md` as ambiguity.
- Ship `config/allowed_signers` and verify release tags against it, so
  SSH-signed release tags actually verify on installed hosts (`update.py`
  previously reported "no signed release tags available" for every release,
  including the unsigned v0.2.0).

## 0.2.0 - 2026-08-30

- Establish the public package as the canonical core consumed by downstream
  organization overlays.
- Allow an explicit `SIDE_LANE_MODELS_PATH` configuration overlay without
  modifying the public runtime or default allowlist.

## 0.1.0 - 2026-08-30

- Initial public-package staging for native Codex and Claude review/execute
  lanes.
- Dedicated worktrees for every lane.
- Canonical cross-host governance injection.
- Optional explicit direct-Z.AI GLM execute route.
- Claude and Codex plugin/marketplace manifests.
