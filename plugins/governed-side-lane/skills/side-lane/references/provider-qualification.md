# Optional provider setup and first trials

Use this reference when the user supplies an account/key, asks whether new lanes
are ready, or authorizes a provider trial. Ordinary Prompt it work with one
OpenAI or Anthropic host requires none of these accounts.

## Identify the product before configuring a route

Record the provider, product, account region, documented endpoint, exact API
model ID, local worker harness, credential reference, and billing basis. Store
references to secrets, never their values in skills, prompts, artifacts, or argv.

- DeepSeek: distinguish economical/Flash and frontier variants using current
  account-compatible IDs and pricing; do not default to the frontier variant.
- Kimi: distinguish Platform PAYG from Kimi Code subscription credentials and
  endpoints. Match the account's regional endpoint and currency. A funded
  Platform account does not establish a Code subscription entitlement.
- MiniMax: distinguish the standard PAYG inference API key from a `sk-cp`
  Token Plan key backed by prepaid credits. Token Plan credits can qualify the
  route without a separate subscription; neither credential is an account-
  management credential. Confirm the product, region, endpoint, and applicable
  credit or subscription terms before comparing marginal cost. Use the
  [Token Plan pricing documentation](https://platform.minimax.io/docs/guides/pricing-token-plan)
  as the current source for those terms.
- Cognition/Devin: distinguish service credentials, personal access tokens, and
  API credentials using current documentation. For the Devin local worker,
  qualify the separate CLI login and its local workspace harness; inherited
  worker MCP tools remain host-local. A Devin Cloud workspace is a separate
  route and is never a fallback for local execution.
- xAI/Grok: distinguish consumer subscription access from API credentials and
  establish the actual tool harness rather than inferring browser support.

The [model guide](../../../docs/model-guide.md) owns signup links, plan advice,
and sourced candidate details. Verify current vendor documentation before
changing endpoints, IDs, plan recommendations, or prices. Account acquisition,
adapter implementation, route configuration, live qualification, and activation
are separate milestones. Do not suggest another purchase to fix a missing
adapter. Preserve the user's already-authorized scope across setup turns.

## Qualify a local worker

Presence-only discovery may inspect installed versions, route configuration,
and credential references. Live calls require the applicable run authority.
If the runner reports an unqualified adapter, finish its implementation and
validation; do not remove the gate or invoke an ad hoc bypass to claim readiness.

For an authorized trial, record the exact route, task boundary, spend limit,
required capabilities, success criteria, and stopping condition. Confirm:

1. Authentication and model identity match the intended account and endpoint.
2. Tool execution takes place as the user on the intended workspace host.
   A dedicated Git worktree isolates edits, not OS access. A cloud model can
   power local tools; a hosted cloud-agent workspace is a different route.
3. The worker can read the intended project, make the authorized fixture edit,
   and run its check. Preserve host settings and existing task permissions.
4. Required connectors work on that worker host. For browser tasks, test an
   authorized navigation, page read, and screenshot with the actual browser
   connector/session. MCP presence alone does not establish working access.
5. Timeout, cancellation, errors, artifacts, and usage reporting work without
   leaking credentials. Report unknown usage/cost rather than zero.

Keep coding, browser-navigation, and visual-design acceptance evidence separate.
Use representative tasks and coordinator inspection, not a model's self-rating.
Connector recommendations should identify the needed operation and host: for
example browser interaction, design-file inspection, or repository access.
Enable connectors within the user's authorization; do not copy another host's
sessions or expand external-write permissions. Review mode retains its existing
no-secret/no-MCP contract; an API-backed execute trial does not become review
mode because the requested output is a review.

## Compare and activate

Compare the cheaper capable variant and the stronger variant on comparable
accepted outcomes when useful. Include failed attempts, retries, cache treatment,
tool charges, and coordinator repair in total cost per accepted task. Preserve
the currency and applicable pricing tier. A prepaid API balance is consumed by
usage; it is not zero marginal cost. Subscription spend displaced is a measured
saving only if it actually reduces paid usage or a later subscription purchase.
Keep hypothetical estimates separate from observed, route-specific cohorts.

Record observed model/harness versions, task type, result, limitations, date,
and qualification evidence. Enable only routes whose adapter and task evidence
pass the existing gates. Report separately whether changes are tested in a
worktree, committed, merged, installed, and confirmed by installed discovery.
A successful API call or a passing unit test alone is not an end-to-end lane.

For measured cross-route cost comparisons, set `session_cost_basis` to
`route-specific-cohorts` and provide `route_session_cohorts`, keyed by exact route
ID, with each route's complete `session_attempts` and `accepted_completions`.
Include failed attempts. Missing or zero-success cohorts are ineligible; usage
is never borrowed from another model. This is distinct from a hypothetical
workload estimate. Keep unmeasured coordinator overhead explicitly unknown.
