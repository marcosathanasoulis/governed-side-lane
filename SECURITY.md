# Security policy

## Supported versions

Only the latest signed release is supported. Pre-release commits are provided
for testing and must not be treated as a stable security boundary.

## Reporting

Do not open a public issue for a vulnerability, leaked credential, unsafe
permission boundary, or route that can bypass canonical governance. Use GitHub
private vulnerability reporting on the public repository. If that feature is
unavailable, contact the maintainer privately through the contact method listed
on the GitHub profile.

Include the affected version, host, mode, exact route, reproduction steps, and
whether any secret or external system was exposed. Do not include live secrets,
customer data, or private repository content.

## Security model

- Review mode is read-only, disables MCP/connectors, and does not access
  secrets.
- Execute mode uses the selected host's same-user authority. Its worktree is
  Git edit isolation, not an OS sandbox.
- Provider/model selection is exact and fail-closed; there is no silent
  fallback.
- Tests and CI use mocks only and must never retrieve a real credential or make
  a paid model call.
