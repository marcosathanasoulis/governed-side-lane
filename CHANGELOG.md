# Changelog

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
