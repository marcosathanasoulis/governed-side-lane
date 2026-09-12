# Model and account guide

*Research reviewed September 11, 2026. Links are to provider documentation or
signup pages. A listed product is an optional candidate, not an enabled route.*

You do not need another subscription to use Governed Side Lane. Start with the
native Codex or Claude Code account already signed in on the worker host. A
single OpenAI account or a single Anthropic account is enough for normal
planning and execution. Side Lane, another provider, an API key, and a model
catalog are not prerequisites.

## Read status before choosing a model

The runner keeps these facts separate:

| Status | Meaning |
| --- | --- |
| **Candidate** | A product worth evaluating. It is not in the execute allowlist. |
| **Configured** | Its local route details have been recorded. Authentication, tools, and task fit may still be missing. |
| **Available for this task** | The exact worker host, model identity, tools, evidence, and economics pass the task's eligibility checks. It still needs approval. |
| **Executable** | The exact route is in the reviewed execute allowlist and has the required task evidence. A run still needs the task's explicit authorization. |

`config/models.json` is the execute allowlist. Candidate records are
intentionally absent from it and default disabled. Use `side-lane candidates`
or `side-lane candidates --json` to read the research catalog at
`config/routing-catalog.json`. It reports `execution_location` and
`qualification_state`, plus `executable: false`, `runtime_allowlisted: false`,
`credential_checked: false`, and `authorization_checked: false`. It does not
inspect an account, connector, quota, credential, provider, or permission. A
cloud agent, consumer subscription, downloaded CLI, or key cannot promote
itself through these states.

Route identity matters: record provider, endpoint or gateway, worker host,
harness and version, mode, requested and resolved model IDs, reasoning setting,
modalities, plan/billing basis, and evidence date. Hosted inference can run at
a provider while tools operate in the local worker workspace; that does not make
the worker a cloud workspace. A worktree only isolates Git edits. Execute
workers run locally as the signed-in user with their host's filesystem, network,
and tool access, subject to the approved task and existing execute rules.

## Accounts worth considering

Do not create all of these accounts. Pick one only when a bounded evaluation
needs a capability or measured cost benefit that the native account lacks.
Never paste a key into chat or a command line, or inspect quota during
discovery.

| Need | Start here | Account and setup fact | Status |
| --- | --- | --- | --- |
| Existing coding work | Keep the existing [OpenAI](https://chatgpt.com/) or [Claude](https://claude.ai/) account | Native OAuth is per host. Compare models already available there before buying anything. | Native routes may be executable when locally allowlisted. |
| Low-cost coding or vision experiment | [DeepSeek API](https://platform.deepseek.com/) with `deepseek-flash` | It has documented OpenAI- and Anthropic-compatible endpoints; its [pricing](https://api-docs.deepseek.com/quick_start/pricing/) lists token and cache rates. Use an API key only in approved local credential storage. | Candidate; no packaged execute route yet. |
| Kimi local coding trial with metered billing | [Kimi Platform](https://platform.kimi.ai/) PAYG | Use the exact regional endpoint for the funded API account. The [China Platform Claude Code guide](https://platform.kimi.com/docs/guide/claude-code-kimi) documents `https://api.moonshot.cn/anthropic` and `kimi-k2.7-code` (thinking required). Platform keys and Kimi Code subscription keys are separate. Compare K2.7 Code against K3 on accepted tasks before buying a longer subscription. | Local qualification required; funding alone does not activate Side Lane. |
| Kimi subscription alternative | [Kimi Code](https://www.kimi.com/code/) | Verify current plan eligibility, quotas, and subscription-specific model IDs in the [Kimi Code docs](https://www.kimi.com/code/docs/kimi-code/models.html). A subscription may suit sustained usage after trial; it does not fund Platform API calls. | Separate product and credential contract. |
| Coding plus multimodal comparison | [MiniMax platform](https://platform.minimax.io/subscribe) | Choose standard PAYG or prepaid Credits deliberately: they use different keys and balances. Credits use an `sk-cp` Subscription Key even without a Token Plan subscription; 1,000 credits = $1 and usage follows the PAYG list price. For sustained measured use, current [token plans](https://platform.minimax.io/docs/guides/pricing-token-plan) list Plus at $22/month and Max at $55/month; shared quota and overflow credits apply. Compare M3 with M2.7 rather than assuming the newer name wins. | Candidate; protocol and tool support need local proof. |
| Browser execution | Qualify a native-host browser connector first; use [Google AI Studio](https://aistudio.google.com/) only for a dedicated Gemini comparison | A supported local browser connector is the baseline. Gemini computer use is an optional model/action-loop evaluation; its [setup guide](https://ai.google.dev/gemini-api/docs/computer-use?authuser=0) still requires an executor, screenshots, and verified final state. | Any qualified vision/tool model may drive the selected host's browser. An API key alone does not operate it. |
| SWE-2 / local Devin CLI evaluation | [Devin](https://devin.ai/pricing) | The Free plan is enough to inspect fit. The provider advertises free SWE-2 in Desktop and CLI through October 10, 2026; verify current access before a paid pilot. | Local Devin CLI and [Local Fusion](https://cognition.com/blog/local-fusion) are optional, unqualified local routes. Devin Cloud is a separate cloud workspace; do not treat it as a local-worker equivalent. |
| Grok comparison | [xAI Console](https://console.x.ai/) | Use a metered API experiment only after a direct supported harness is qualified. See the [Grok 4.6 model page](https://docs.x.ai/developers/models/grok-4.6). A consumer Grok plan does not establish API credit or local-tool access. | Candidate; Grok Bot is cloud-hosted and excluded from local-workspace selection. |
| Wider research set | [Alibaba Model Studio](https://www.alibabacloud.com/help/en/model-studio/) or [MiMo](https://mimo.mi.com/) | Qwen Flash/dated Max and MiMo variants remain research choices. Region, access, price, endpoint, and task fit need confirmation before any evaluation. | Candidate only. |

## Match the task before the model

Use this as an evaluation shortlist, never as an automatic route assignment.

| Task | Candidate comparison | Required proof before it can run |
| --- | --- | --- |
| Bounded code, tests, or mechanical migration | Compare all configured economical workers, including DeepSeek Flash, Kimi `kimi-k2.7-code`, MiniMax M2.7/M3, and native local SWE models | Reproducible patch, tests, scope discipline, and full-session cost evidence. |
| Large context or reference-driven UI implementation | Kimi Platform `kimi-k3` versus `kimi-k2.7-code`; MiniMax M3 as a comparison | Exact model/effort, image or reference support, visual/accessibility result, and responsive behavior. |
| Browser operation | Native-host browser connector first; optionally compare Gemini computer use with the same executor | A browser connector/action loop, session handling, and verified final state. Vision is not proof. |
| SWE agent workflow | Local Devin CLI or Local Fusion separately | A local-workspace harness, exact model/composite identity, and ordinary task evidence. Devin Cloud stays out of local-worker selection. |
| High-uncertainty or consequential work | A proven native model remains the default comparison | Task-specific reasoning evidence and an independent review plan where it has decision value. |

## Economics that support a decision

Compare three different things:

1. **Dispatch cash cost** is the incremental charge for this task under the
   user's declared plan. Included usage in an already-paid subscription can be
   zero marginal cash cost.
2. **Acquisition cost** is a new monthly plan, likely overflow, and setup
   effort. It belongs in signup advice, not hidden inside each dispatch.
3. **Time and intervention** are reported separately unless the user supplies
   a value for time.

Estimate a full session, including uncached input, cached reads and writes when
priced, output/reasoning, tool fees, retries, handoff, and correction work.
Unknown prices, plan entitlements, or quotas stay unknown; discovery never
probes them. Do not treat a sunk subscription as free capacity or compare
credits, tokens, and agent units as interchangeable. Prefer the least-expensive
qualified route that meets the quality and latency target. When comparable costs
are unknown, describe the uncertainty and choose the best evidenced fit instead
of claiming optimization.

For a cohort, expected accepted-task cost is total session cash cost, including
failed attempts, divided by accepted completions. With no accepted completion,
there is no finite accepted-task estimate. Do not substitute a provider benchmark
or a one-off successful demo for this measure.

For illustration only, a 100K-uncached-input and 20K-output call at DeepSeek's
documented peak prices costs $0.054 on Flash ($0.030 input + $0.024 output) and
$0.2112 on Pro ($0.132 input + $0.0792 output). That is a token arithmetic
example, not a quality result: choose Flash or Pro only after the same task
floor has been demonstrated. DeepSeek's current [rate card](https://api-docs.deepseek.com/quick_start/pricing/)
is the source; caching, retries, and changing rate cards alter the result.

## What this research supports

The direct-provider comparison set includes DeepSeek `deepseek-flash` and
`deepseek-v4-pro`; Kimi China Platform `kimi-k2.6`, `kimi-k3`,
`kimi-k2.7-code`, and `kimi-k2.7-code-highspeed`; and MiniMax M3/M2.7 variants.
Kimi Code subscription aliases remain separate candidates. DeepSeek documents
V4.1 Flash behind the current Flash selector; retired `deepseek-v4-flash` requests
redirect, so an echoed selector alone does not prove immutable model weights.

Devin CLI exposes an account-specific model inventory. Discover it with
`devin models list --format json`, pin the exact model UID, and distinguish
Cognition SWE models from Grok/Gemini/other vendors served through Devin.
A Grok route through Devin uses Devin billing and authentication, not a direct
xAI API key. Availability and any promotional free label must be checked for
the actual account. Devin Cloud remains excluded from local-workspace routing. The linked
provider documentation establishes selectors, plans, endpoints, or published
pricing; it does not establish local tool behavior or task quality. Provider
benchmarks and community reports, including Reddit reports, are evaluation
priors only. Uncorroborated podcast or social claims are not route evidence and
do not create a candidate or an executable route.

Useful real-use signals, kept separate from provider claims, include competing
[DeepSeek Flash positive](https://www.reddit.com/r/DeepSeek/comments/1wcbz1z/im_in_love_with_v41_for_coding/)
and [critical](https://www.reddit.com/r/DeepSeek/comments/1wdd9hh/deepseek_v41_flash_is_cheap_per_tokenbut_is_it/)
reports, plus the K3 frontend hypothesis in [Moonshots episode 272](https://podbay.fm/p/moonshots-with-peter-diamandis/e/1784469600).
They motivate bounded tests; they do not establish a ranking, price, or route
qualification.

## A safe first evaluation

Choose one bounded task with an acceptance test, a small approved data packet,
and a spending ceiling. Keep the model, reasoning setting, and harness stable
for the task so caches and evidence remain meaningful. Record the exact route
identity, elapsed time, interventions, outcome, and total session cost. A
candidate only becomes executable after the reviewed local qualification and
allowlist process; a successful vendor demo is not that proof.

Start with catalog inspection, then run a task estimate only against configured
routes:

```bash
side-lane candidates --json
side-lane recommend --repo "$PWD" \
  --profile config/examples/session-profile.json
```

The supplied [session profile](../config/examples/session-profile.json) is a
hypothetical workload with a primary attempt, correction attempt, and declared
spend state. It uses each route's own qualified acceptance evidence rather than
claiming the same observed outcome for every model. An observed cohort must use
`session_cost_basis: "route-specific-cohort"`, its exact `cohort_route_id`, and
`accepted_completions`; it cannot price another route. Copy and adapt the
example to the actual task and declared plan state; it contains no credential
or quota fields. Token buckets must not overlap: when reasoning is supplied
separately, `output_tokens` excludes those reasoning tokens. Each attempt is one
priced request when context bands apply. The
[disabled provider profile](../config/examples/provider-profiles.disabled.json)
shows the exact identity contract a direct provider would need. It is not read
by Side Lane, does not enable a provider or credential lookup, and is not a
configuration shortcut. DeepSeek, Kimi, and MiniMax launches currently stop
with an adapter-unqualified error even if a caller supplies configuration.
Enabling them requires a reviewed adapter qualification change, an exact
allowlist entry, local task evaluation, and explicit task authorization.

## Qualification entrypoint

The operational `side_lane.qualification.qualify_claude` helper runs explicitly
authorized, bounded local trials for direct providers after a matching transport
probe. It reuses the adapter command/environment contracts, preserves local
workspace execution, rejects conflicting saved authentication, and returns
qualification evidence without activating a route. Automated tests mock it;
ordinary `side-lane run` still rejects these candidates until full qualification
and reviewed activation. Usage reported with `costBasis: unknown` is not a
verified vendor bill.
