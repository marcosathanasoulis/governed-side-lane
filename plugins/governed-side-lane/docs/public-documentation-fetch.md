# Scoped public documentation fetching

For an authorized execute task requiring public vendor or library documentation,
assess the needed hosts before dispatch and add repeatable `--web-domain HOST`
grants. Code-only tasks need no web grant.

Example: `--web-domain cloud.google.com --web-domain docs.python.org`.
At most ten entries are accepted. Each is an exact lowercase ASCII hostname,
with no scheme, port, path, wildcard, IP literal, or reserved private suffix.
Duplicates collapse; granted hosts are sorted in the `web_domains` audit field.

Devin receives `Fetch(https://HOST/*)`; Claude Code receives
`WebFetch(domain:HOST)`. These are native permission grants, independent of
provider/model and shell capability. Review mode rejects the flag. The Codex
host currently rejects it because this adapter lacks scoped native fetch rules;
a provider named OpenAI using the Claude host is not the Codex host.

Use the grant only for public documentation over HTTPS. Do not send credentials,
private request data or authenticated cookies. Missing host authority must be
reported, never silently broadened. Inherited native deny/ask rules remain.

These rules are approval controls, not a network sandbox. DNS resolution and
redirect handling are not controlled here; a public-looking name can resolve
to a private address. Claude's domain rule does not itself enforce HTTPS; that
is a task instruction. Native runtime behavior requires live qualification.

Callers must persist and propagate the exact assessed hosts into runner flags.
Do not add blanket hosts to every task or infer a host list from shell access.
