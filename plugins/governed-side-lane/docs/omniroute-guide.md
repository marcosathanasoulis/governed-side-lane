# Optional OmniRoute add-on

*OmniRoute is an optional router/gateway add-on for organizations that want to
centralize model selection, token pricing, and quota selection behind a single
routed pool. Governed Side Lane works with native and direct-provider routes
without it; do not install a gateway merely to use the core skill.*

## What the gateway may and may not own

Side Lane keeps ownership of task fit, capability, authority, and acceptance for
every dispatch. An optional OmniRoute gateway may own only model/token pricing
and quota selection, and only inside an exact qualified approved pool that has:

- a current, operator-supplied server policy snapshot or receipt;
- a reviewed runtime allowlist and routing-catalog `routed_pool` declaration;
- explicit caps, identity binding, and fail-closed context guarantees;
- the current policy revision, member set, fingerprint, and nonsecret caps.

Unknown prices, entitlements, or quota remain unknown. Do not turn a
documented rate card, subscription, or catalog estimate into a verified zero-cost
claim, and do not claim unproven live automatic gateway behavior. A hashed
snapshot is a consistency/freshness check, not a signature: it does not prove the
live server is currently enforcing the policy.

## When to consider it

Consider OmniRoute only when an approved pool already exists and the economics of
a bounded, qualified source-research or implementation task are meaningfully
better through the gateway than through a direct or native route with current
evidence. Keep ordinary native/direct routes as the default; the gateway is not a
prerequisite for any standard Side Lane or Prompt it workflow.

## Setup shape

A routed provider needs a reviewed `routing_policy_contract` in the runtime
allowlist (`config/models.json`) and, for recommendations, a `routed_pool`
declaration in the research catalog (`config/routing-catalog.json`). The runtime
`routing_policy_contract` is a complete, pinned contract defined in the runtime
config; the expected shape is tested in `tests/test_omniroute_routed_route.py`.
The `config/routing-catalog.schema.json` schema describes the research catalog,
not the runtime provider envelope.

Example allowlist fragment under `providers.omniroute` for automatic
selector-policy collection (the `base_url`, `credential_service`,
`policy_revision`, `policy_fingerprint`, and upstream models are private
configuration; the public package uses `.invalid` placeholders):

```json
{
  "gateway": "omniroute-router",
  "auth_method": "provider-key",
  "credential_service": "side-lane-omniroute-example",
  "base_url": "https://omniroute.example.invalid",
  "billable": true,
  "explicit_only": true,
  "automatic_selector_policy": true,
  "routes": {
    "execute": {
      "claude": {
        "protocol": "anthropic-compatible",
        "models": ["example-pool-selector"],
        "model_configs": {
          "example-pool-selector": {
            "runtime_model": "example-pool-selector",
            "protocol": "anthropic-compatible",
            "routing_policy_contract": {
              "requested_selector": "example-pool-selector",
              "allowed_upstream_models": [
                "upstream-a/model-alpha",
                "upstream-b/model-beta"
              ],
              "policy_revision": "<operator-supplied-revision>",
              "policy_fingerprint": "<operator-supplied-fingerprint>",
              "settings_precedence": "verified"
            },
            "timeout_seconds": 2700,
            "max_budget_usd": 1.0
          }
        }
      }
    }
  }
}
```

The `max_budget_usd` value must be finite and positive for `--report-only`, and
it is a client-side estimate guard. It is not proof that the upstream server or
account enforces the same cap: verify any provider-side or account-level limit
separately.

The route is only eligible when the exact gateway, host, mode, and model are
configured and the per-run or automatic policy snapshot binds the current policy
and member set.

## Validation workflow

`list` and `candidates` are presence-only checks: they do not prove live policy
or call the router.

```bash
# Inspect configured routes. Listed is not ready.
side-lane list

# Inspect the research catalog. Candidates are not configured routes.
side-lane candidates --json

# Eligibility for the exact repo, task, and mode.
# With automatic_selector_policy: true, this performs an authenticated,
# read-only HTTPS GET to the gateway's /v1/selector-policy endpoint,
# validates the returned receipt, and includes it in the recommendation.
# The collector does not call inference; it reads only public receipt data
# and fails closed, so an unresponsive or untrusted gateway leaves the route
# ineligible rather than falling back.
side-lane recommend --repo "$REPO" \
  --profile /path/to/task-profile.json

# Manual alternative: the operator supplies a pre-collected JSON snapshot.
side-lane recommend --repo "$REPO" \
  --profile /path/to/task-profile.json \
  --routed-policy-snapshot /path/to/policy-snapshot.json

# Presence of credential and required capabilities for the exact route.
side-lane check-capabilities --host claude --mode execute \
  --provider omniroute --model example-pool-selector --repo "$REPO"
```

A routed-policy snapshot is a receipt Side Lane validates against the pinned
runtime contract: `match_snapshot` checks the selector, policy revision and
fingerprint, the attested upstream set, canonical member identities, nonsecret
caps, the fail-closed `context_fit_fail_closed` marker, observation freshness,
and the evidence provenance/receipt reference. It does not prove live server
enforcement or quota. The authenticated control-plane collector is responsible
for producing a fresh receipt; the operator file is an equivalent manual
alternative.

## Dispatch examples

For an implementation task with the route already approved and the spend
covered:

```bash
side-lane run --host claude --mode execute --provider omniroute \
  --model example-pool-selector --repo "$REPO" \
  --lane-name omni-implementation-001 \
  --approve-billable-route \
  --prompt-file task.md
```

For an authorized bounded source-research task that produces only a report and
uses read roots, add the capabilities the worker needs for report
generation/reading (for example `shell` or `workspace-write`), and pass
`--allow-no-commit --no-publish` so a report-only outcome is not treated as a
source commit or push. The report file is an output exception to the ordinary
execute rules; the lane still runs in execute mode and is not a sandbox.

```bash
side-lane run --host claude --mode execute --provider omniroute \
  --model example-pool-selector --repo "$REPO" \
  --lane-name omni-research-001 \
  --report-only \
  --allow-no-commit \
  --no-publish \
  --approve-billable-route \
  --capability shell \
  --read-root /path/to/shared/sources \
  --prompt-file research-prompt.md
```

`--report-only` requires the route to have a finite positive `max_budget_usd`.
That value is a client-side estimate guard, not a hard proof that the upstream
server or account enforces the same cap; verify provider-side and account limits
separately.

For a comparable bounded research task without a gateway, use an exact native or
direct route and return the findings in the lane's final response. This is a
result-only research response, not a report file; do not use `--report-only`,
which requires a `max_budget_usd` that the native default config does not carry.

```bash
# Use an approved model from the configured native claude execute allowlist.
# This example selects claude-sonnet-5; claude-opus-5, claude-fable-5, or
# claude-haiku-4-5-20251001 are also valid when approved for the task.
side-lane run --host claude --mode execute --provider claude \
  --model claude-sonnet-5 --repo "$REPO" \
  --lane-name native-research-001 \
  --allow-no-commit \
  --no-publish \
  --capability shell \
  --read-root /path/to/shared/sources \
  --prompt-file research-prompt.md
```

A native route does not imply zero cost and does not take priority over a qualified
economical route; it is simply the example for a no-gateway research response.

## Fallback and defaults

If the gateway, policy snapshot, or route is unqualified, Side Lane continues on
the originating host using native or direct routes that pass their own
eligibility assessment. A missing optional gateway is not a blocking
prerequisite. Eligibility is assessed independently of the optional integration;
switching to any other lane requires the preapproved-backup and explicit
no-silent-substitution rules. Absence of an optional gateway or route does not
activate an unapproved lane.
