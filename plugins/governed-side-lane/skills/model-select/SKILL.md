---
name: model-select
description: Use when picking the cheapest qualified model for a task across whichever hosts a user actually has (Claude only, Codex only, or both), with OpenRouter optional.
---

# Model select

Pick the cheapest qualified route for a task, limited to what the current
user actually has: native Claude, native Codex, or both, plus OpenRouter if
the user has configured a key. It never detects account usage; the user
declares it. It is a frozen, versioned snapshot of derived model-selection
knowledge, not a live learning system: it never writes back or improves
itself between published versions. A new snapshot ships only with a new
published package version.

## When to use

- The user wants the cheapest model that can still do a given task well
  enough, and is not asking for a specific named model.
- Before dispatching a task through a metered route (OpenRouter, or a
  native host the user has declared to be in extra usage), to decide which
  model to use and confirm the user is fine with the expected cost.

## 0. Start from the Auto Router decision

`side-lane auto-route --task "<one line>"` applies the shared policy first:
pin, then included OAuth usage on a host the user is signed in to, then the
OpenRouter Auto Router (metered, only when nothing included can run the task),
else `blocked` with setup steps. A `native` decision includes a `staffing` menu
for the coordinator to choose from when there is no OpenRouter. Use this skill's ranking when the user wants to
compare routes in detail or the decision is `auto` and they want a cost view.
OpenRouter is optional: without a key everything works on the user's own hosts,
and you may mention once that a free OpenRouter account (key stored through the
setup wizard, never pasted into chat) lets the Auto Router pick cheaper models
when included usage is out.

## 1. Gather inputs without guessing

- Detect which native hosts are present: call
  `side_lane.model_select.detect_available_hosts()` (presence/executable
  lookup only; this never checks login state or quota).
- Detect OpenRouter presence: call `side_lane.model_select.openrouter_present()`,
  which uses this package's `side_lane.credentials.credential_present`
  against the service name `governed-side-lane-openrouter`. This reports
  presence only; it never reads or displays the key value.
- Ask the user (do not infer) the declared usage state for each native host
  they have: `included-oauth`, `extra-usage`, or `unknown` (default). This
  package never detects usage automatically.
- Build a task profile: a `task_family` (a short label describing the kind
  of work -- see the snapshot's `task_families` for the known ones, e.g.
  `general_coding_execution`), any `required_capabilities` (e.g. `tools`),
  a `quality_floor` (`low`/`medium`/`high`), and a rough `input_tokens`/
  `output_tokens` forecast. When the task doesn't match a known family,
  use `general_coding_execution` and say so.

## 2. Run the CLI

```
python3 -m side_lane.model_select --profile task.json [--prices prices.json] [--jev-consent]
```

`task.json` holds the task profile fields above. `prices.json` is optional:
a map of model id to `{"input_usd_per_million", "output_usd_per_million"}`,
normally the user's own fetch of OpenRouter's public
`GET /api/v1/models` response reshaped to that map -- this skill never
fetches it automatically. Without a price row for a given model, its cost
stays `"unknown"` in the output; it is never shown as free or zero.

The output is JSON: a `ranked` list (route id, model id, host, expected
cost per success, its `basis` -- `live_price`, `snapshot_prior+opportunity_cost`,
or `unknown` -- and quality tier), an `excluded` list with reasons, and the
`snapshot_id` the ranking came from.

## 3. Present the choice

- Show the top-ranked route and its basis plainly, including when the cost
  is `unknown` (never imply it's free).
- State the snapshot's `snapshot_id` and that it is frozen, dated knowledge
  that does not learn from this run.
- If the top candidates are a metered OpenRouter route or an extra-usage
  native host, confirm with the user before dispatching -- this skill never
  dispatches on its own.

## 4. Optional: ask Jev (only with OpenRouter and explicit consent)

When OpenRouter is configured, you may ask Jev -- an OpenRouter judge model,
paid by the user -- for a second opinion on the ranked candidates:

1. Build the de-identified payload with `side_lane.model_select.jev_request(...)`.
   It carries only candidate model ids, the task family, and capability
   needs -- never the user's task text, unless the user explicitly adds it
   themselves.
2. Ask the user for explicit consent before this call; it is a paid,
   external request. Never call it implicitly.
3. Call `side_lane.model_select.call_jev(payload, transport=..., consent=True)`
   through the user's own OpenRouter setup.
4. Always run `side_lane.model_select.recheck_jev_choice(choice, ranking)`
   on the result. It accepts Jev's pick only if it is one of the eligible
   ranked candidates and within a small cost tolerance of the scorer's own
   best pick; otherwise it keeps the scorer's pick and says why. Report
   whichever pick was kept, and the reason, to the user.

Without OpenRouter configured, skip this step entirely and present the
scorer's own ranking over the user's native hosts only.
