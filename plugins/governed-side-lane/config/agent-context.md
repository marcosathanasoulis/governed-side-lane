# Shared direct-session context

This file is the common entrypoint for developers working directly in Codex or
Claude Code. Host-global instruction files should point here; they must not copy
this content.

For every repository task:

1. Read the repository-root `AGENTS.md` first. It must identify and require one
   authoritative repository-root `CLAUDE.md`; read that file completely.
2. Before editing shared files, inspect open pull requests and active Git
   worktrees for overlap, and use repository-specific coordination tooling when
   available. Do not create or consult a hand-maintained claims ledger.
3. Treat checked-in repository documentation as the durable shared memory for
   rules, protocols, exceptions, decisions, and verified project facts. A fact
   that must survive a host switch belongs in the repository, linked from its
   authoritative governance file, rather than only in a host's private memory.
4. When dispatching a side lane, use `config/lane-governance.md` through the
   packaged launcher. Do not reproduce or weaken its mode-specific contract.

Claude Code auto-memory, Codex product memory, host hooks, OAuth sessions, and
connectors remain host-specific. They may provide hints but are not authoritative
shared project memory. Verify useful durable facts and promote them to the
repository through the normal reviewed workflow.
