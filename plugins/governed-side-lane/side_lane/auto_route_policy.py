# SHARED CORE: the OpenRouter Auto Router selection policy, ported without
# behavior change from the hosted Side Lane implementation. The only difference
# is that ``datetime.UTC`` (Python 3.11+) is spelled ``timezone.utc`` so this
# runs on Python 3.10. A drift test in the maintainers' repository compares this
# file with the hosted original; change behavior there first, then re-port.
"""OpenRouter Auto Router selection policy (``model_selection_mode: auto``).

Owner policy (final, 2026-10-03):

1. An explicit user model pin (``model_intent``) wins -- this module is not
   consulted for pinned turns.
2. Otherwise the ``openrouter/auto`` route always chooses. The only
   plan-related rule: when a provider's included usage is OUT (declared
   ``quota_status: exhausted``/expired, or an auto-detected 429 hard stop on
   that provider's native route), that provider's model patterns go on
   Auto's ``excluded_models`` for the request. Free models are never
   excluded by default.
3. ``cost_tier`` comes from a simple complexity rule; ``data_collection:
   deny`` + ZDR whenever a granted capability touches user-data services.
4. Failover: Auto's own model fallbacks first (inside OpenRouter), then the
   remaining granted routes in their existing order.

Included-plan consumption: OpenRouter bills metered and cannot spend a
Claude/Codex/Devin/GLM/Gemini plan. So when Auto's turn-1 pick is a model
whose provider has a declared ``included``, non-exhausted plan with
``native_handoff: true`` and a granted native route, follow-up turns move to
that native route (``included_native`` in the per-turn settings). Turn 1 is
always metered; no extra probe call is made.

Declared plans live outside OpenRouter: the owner declares them in a small
JSON file the resolver config names. Nothing here reads a secret or calls a
network.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

AUTO_MODEL = 'openrouter/auto'
#: Sentinel provider tag for the auto route: the router, not one endpoint,
#: chooses the provider. Never sent as ``provider.only``.
AUTO_PROVIDER_TAG = 'auto'
CONTEXT_KEY = 'auto_router'
SETTINGS_VERSION = 1
COST_TIERS = ('low', 'medium', 'high', 'xhigh', 'max')
SELECTION_MODES = frozenset({'auto', 'legacy'})
PLAN_MODES = frozenset({'included', 'extra', 'metered'})
QUOTA_STATUSES = frozenset({'declared', 'unknown', 'exhausted'})
DEFAULT_EFFECTIVE_COST_FRACTION = Decimal('0.10')

#: Model pattern charset: OpenRouter ids plus ``*``/``?`` wildcards.
MODEL_PATTERN_RE = re.compile(r'[A-Za-z0-9._/:*?+-]{1,200}')
SERVED_MODEL_RE = re.compile(r'[A-Za-z0-9._/:+-]{1,200}')
SESSION_ID_RE = re.compile(r'sl-[0-9a-f]{32}')
_PROVIDER_RE = re.compile(r'[a-z][a-z0-9-]{0,63}')
_ROUTE_ID_RE = re.compile(r'[a-z][a-z0-9-]{1,79}')
MAX_PATTERNS = 200

#: Defaults: only non-chat / non-LLM models and other routers are excluded.
#: Free (``:free`` / zero-cost) models stay in the pool by owner decision;
#: their outcomes are recorded so any that fail in practice can be excluded
#: later (and route-health hard stops exclude them automatically per turn).
#: The owner tunes the actual lists in the resolver config.
DEFAULT_EXCLUDED_MODELS = (
    'openrouter/*',
    'typesafe/*',
    '*embed*',
    '*whisper*',
    '*tts*',
    '*transcribe*',
    '*dall-e*',
    '*image-only*',
    '*-guard*',
    '*moderation*',
)

#: Capability ids / service names whose grant means user data may reach the
#: model dialogue. Matching grants force ``data_collection: deny`` + ZDR.
DEFAULT_SENSITIVE_PATTERNS = (
    '*postgres*',
    '*firestore*',
    '*bigquery*',
    '*mixpanel*',
    '*user*',
    '*member*',
)


class AutoRouterConfigError(ValueError):
    """Typed config failure; the message is a short field code."""


def _fail(code: str) -> None:
    raise AutoRouterConfigError(code)


def _patterns(value: Any, code: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > MAX_PATTERNS
        or not all(isinstance(v, str) and MODEL_PATTERN_RE.fullmatch(v) for v in value)
    ):
        _fail(code)
    return tuple(dict.fromkeys(value))


def _price(value: Any, code: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _fail(code)
    if not parsed.is_finite() or parsed <= 0:
        _fail(code)
    return str(parsed)


@dataclass(frozen=True)
class AutoRouterSettings:
    """Resolver-config block ``auto_router`` (all fields optional)."""

    allowed_models: tuple[str, ...] = ()
    excluded_models: tuple[str, ...] = DEFAULT_EXCLUDED_MODELS
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    #: Optional per-million USD ceiling sent as ``provider.max_price``.
    max_price: Mapping[str, str] | None = None
    #: After turn 1, follow-ups reuse the model Auto served (config flag).
    pin_served_model_on_followup: bool = True
    #: Owner default 2026-10-03: every auto request uses ``high`` unless the
    #: request words override it (``use the best model`` -> max, ``use the
    #: cheapest model`` -> low) or this config changes it.
    default_cost_tier: str = 'high'
    #: Opt-in complexity mapping (write -> high, analysis -> medium, simple
    #: -> default). Off by default per owner decision.
    complexity_tiers: bool = False

    @classmethod
    def from_dict(cls, raw: Any) -> AutoRouterSettings:
        if raw is None:
            return cls()
        allowed_keys = {
            'allowed_models',
            'excluded_models',
            'sensitive_patterns',
            'max_price',
            'pin_served_model_on_followup',
            'default_cost_tier',
            'complexity_tiers',
        }
        if not isinstance(raw, dict) or set(raw) - allowed_keys:
            _fail('auto_router.shape')
        max_price = raw.get('max_price')
        if max_price is not None:
            if (
                not isinstance(max_price, dict)
                or not set(max_price)
                or (set(max_price) - {'prompt', 'completion'})
            ):
                _fail('auto_router.max_price')
            max_price = {
                key: _price(value, f'auto_router.max_price.{key}')
                for key, value in sorted(max_price.items())
            }
        pin = raw.get('pin_served_model_on_followup', True)
        if not isinstance(pin, bool):
            _fail('auto_router.pin_served_model_on_followup')
        tier = raw.get('default_cost_tier', 'high')
        if tier not in COST_TIERS:
            _fail('auto_router.default_cost_tier')
        complexity = raw.get('complexity_tiers', False)
        if not isinstance(complexity, bool):
            _fail('auto_router.complexity_tiers')
        return cls(
            allowed_models=_patterns(raw.get('allowed_models'), 'auto_router.allowed_models'),
            excluded_models=(
                _patterns(raw['excluded_models'], 'auto_router.excluded_models')
                if 'excluded_models' in raw
                else DEFAULT_EXCLUDED_MODELS
            ),
            sensitive_patterns=(
                _patterns(raw['sensitive_patterns'], 'auto_router.sensitive_patterns')
                if 'sensitive_patterns' in raw
                else DEFAULT_SENSITIVE_PATTERNS
            ),
            max_price=max_price,
            pin_served_model_on_followup=pin,
            default_cost_tier=tier,
            complexity_tiers=complexity,
        )


# ----------------------------------------------------------------------
# Declared plans
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredPlan:
    provider: str
    mode: str
    effective_cost_fraction: Decimal = DEFAULT_EFFECTIVE_COST_FRACTION
    quota_status: str = 'unknown'
    expires_at: float | None = None
    route_ids: tuple[str, ...] = ()
    notes: str = ''
    #: OpenRouter model patterns that belong to this provider's plan
    #: (e.g. ``anthropic/*`` for claude). Used for both rules below.
    model_patterns: tuple[str, ...] = ()
    #: What Auto excludes when this plan's included usage is out. ``None``
    #: means "use ``model_patterns``"; ``[]`` means exclude nothing.
    excluded_models_when_exhausted: tuple[str, ...] | None = None
    #: Move follow-ups to the native route when Auto served this plan's model.
    native_handoff: bool = True

    def exhausted(self, now: float) -> bool:
        return self.quota_status == 'exhausted' or (
            self.expires_at is not None and self.expires_at <= now
        )

    def usable_included(self, now: float) -> bool:
        return self.mode == 'included' and not self.exhausted(now)

    def exclusions_when_out(self) -> tuple[str, ...]:
        if self.excluded_models_when_exhausted is None:
            return self.model_patterns
        return self.excluded_models_when_exhausted

    def matches(self, route_id: str, provider_tag: str) -> bool:
        if route_id in self.route_ids:
            return True
        return provider_tag == f'native-{self.provider}'


_PLAN_KEYS = {
    'provider',
    'mode',
    'effective_cost_fraction',
    'quota_status',
    'expires_at',
    'route_ids',
    'notes',
    'model_patterns',
    'excluded_models_when_exhausted',
    'native_handoff',
}


def _bool(value: Any, index: int) -> bool:
    if not isinstance(value, bool):
        _fail(f'plans[{index}].native_handoff')
    return value


def _parse_expiry(value: Any, index: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        _fail(f'plans[{index}].expires_at')
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            _fail(f'plans[{index}].expires_at')
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            _fail(f'plans[{index}].expires_at')
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    _fail(f'plans[{index}].expires_at')
    return None


def parse_declared_plans(raw: Any) -> tuple[DeclaredPlan, ...]:
    """Validate the owner-declared plans document (schema_version 1)."""
    if not isinstance(raw, dict) or set(raw) - {'schema_version', 'plans', 'notes'}:
        _fail('plans.shape')
    if raw.get('schema_version') != 1:
        _fail('plans.schema_version')
    entries = raw.get('plans')
    if not isinstance(entries, list) or len(entries) > 64:
        _fail('plans.plans')
    plans: list[DeclaredPlan] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) - _PLAN_KEYS:
            _fail(f'plans[{index}].shape')
        provider = entry.get('provider')
        if not isinstance(provider, str) or not _PROVIDER_RE.fullmatch(provider):
            _fail(f'plans[{index}].provider')
        if provider in seen:
            _fail(f'plans[{index}].duplicate_provider')
        seen.add(provider)
        mode = entry.get('mode')
        if mode not in PLAN_MODES:
            _fail(f'plans[{index}].mode')
        fraction_raw = entry.get('effective_cost_fraction', '0.10')
        if isinstance(fraction_raw, bool) or not isinstance(fraction_raw, (int, float, str)):
            _fail(f'plans[{index}].effective_cost_fraction')
        try:
            fraction = Decimal(str(fraction_raw))
        except (InvalidOperation, ValueError):
            _fail(f'plans[{index}].effective_cost_fraction')
        if not fraction.is_finite() or not Decimal(0) <= fraction <= Decimal(1):
            _fail(f'plans[{index}].effective_cost_fraction')
        quota = entry.get('quota_status', 'unknown')
        if quota not in QUOTA_STATUSES:
            _fail(f'plans[{index}].quota_status')
        route_ids = entry.get('route_ids', [])
        if not isinstance(route_ids, list) or not all(
            isinstance(r, str) and _ROUTE_ID_RE.fullmatch(r) for r in route_ids
        ):
            _fail(f'plans[{index}].route_ids')
        notes = entry.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 500:
            _fail(f'plans[{index}].notes')
        plans.append(
            DeclaredPlan(
                provider=provider,
                mode=mode,
                effective_cost_fraction=fraction,
                quota_status=quota,
                expires_at=_parse_expiry(entry.get('expires_at'), index),
                route_ids=tuple(route_ids),
                notes=notes,
                model_patterns=_patterns(
                    entry.get('model_patterns'), f'plans[{index}].model_patterns'
                ),
                excluded_models_when_exhausted=(
                    None
                    if entry.get('excluded_models_when_exhausted') is None
                    else _patterns(
                        entry['excluded_models_when_exhausted'],
                        f'plans[{index}].excluded_models_when_exhausted',
                    )
                ),
                native_handoff=_bool(entry.get('native_handoff', True), index),
            )
        )
    return tuple(plans)


def load_declared_plans(path: Path | None) -> tuple[DeclaredPlan, ...]:
    """Read the plans file; a missing path means no plans are declared."""
    if path is None:
        return ()
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError, UnicodeError) as exc:
        raise AutoRouterConfigError('plans.unreadable') from exc
    return parse_declared_plans(raw)


# ----------------------------------------------------------------------
# Complexity -> cost tier, sensitivity, session id
# ----------------------------------------------------------------------

_MAX_TIER_RE = re.compile(
    r'\b(?:use|with|pick|choose)\s+(?:the\s+)?(?:best|strongest|smartest|most capable|top)\s+'
    r'(?:available\s+)?(?:model|llm|ai)\b|\bmax(?:imum)?\s+quality\b|\bcost[_ ]tier\s*[:=]?\s*max\b',
    re.IGNORECASE,
)
_CHEAP_TIER_RE = re.compile(
    r'\b(?:use|with|pick|choose)\s+(?:the\s+|a\s+)?(?:cheapest|cheap|fastest)\s+'
    r'(?:model|llm|ai)\b',
    re.IGNORECASE,
)
_WRITE_RE = re.compile(
    r'\b(?:implement|refactor|fix|patch|commit|push|open\s+(?:a\s+)?pr|pull request|'
    r'create|update|edit|write|modify|delete|deploy|migrate|add)\b',
    re.IGNORECASE,
)
_ANALYSIS_RE = re.compile(
    r'\b(?:analy[sz]e|analysis|compare|investigate|audit|diagnose|debug|why|explain|'
    r'plan|design|review|evaluate|summari[sz]e|trend|root cause|step[- ]by[- ]step|'
    r'then)\b',
    re.IGNORECASE,
)


def cost_tier_for(
    query: str,
    granted_capabilities: Sequence[str] = (),
    *,
    model_only: bool = False,
    write_capability: bool = False,
    default: str = 'high',
    complexity: bool = False,
) -> str:
    """Cost tier for one auto request.

    Request words always win: ``use the best model`` / ``max quality`` ->
    max; ``use the cheapest model`` -> low. Otherwise the configured default
    (owner default: high). With ``complexity`` enabled (opt-in), a write or
    repo change -> high, multi-step read/analysis -> medium, a service-free or
    simple read -> the default.
    """
    text = query if isinstance(query, str) else ''
    if _MAX_TIER_RE.search(text):
        return 'max'
    if _CHEAP_TIER_RE.search(text):
        return 'low'
    fallback = default if default in COST_TIERS else 'high'
    if not complexity:
        return fallback
    if write_capability or any(
        'write' in cap or cap.endswith('-rw') for cap in granted_capabilities
    ):
        return 'high'
    if _WRITE_RE.search(text) and not model_only:
        return 'high'
    if _ANALYSIS_RE.search(text) or len(granted_capabilities) > 1:
        return 'medium'
    return fallback


def touches_sensitive_data(
    names: Iterable[str], patterns: Sequence[str] = DEFAULT_SENSITIVE_PATTERNS
) -> bool:
    return any(
        fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
        for name in names
        if isinstance(name, str)
        for pattern in patterns
    )


def session_id_for(thread: Sequence[str]) -> str:
    """Stable, opaque per-thread id: channel + root ts, hashed."""
    channel, root_ts = thread[0], thread[1]
    digest = sha256(f'{channel}\0{root_ts}'.encode()).hexdigest()[:32]
    return f'sl-{digest}'


def model_allowed(model: str, allowed: Sequence[str], excluded: Sequence[str]) -> bool:
    """Served-model check: never the router itself, matches allowed (or any
    when no allow-list), and matches no exclusion."""
    if not isinstance(model, str) or not SERVED_MODEL_RE.fullmatch(model):
        return False
    if model == AUTO_MODEL:
        return False
    if allowed and not any(fnmatch.fnmatchcase(model, p) for p in allowed):
        return False
    return not any(fnmatch.fnmatchcase(model, p) for p in excluded)


# ----------------------------------------------------------------------
# Per-turn settings (grant.context['auto_router'])
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TurnSettings:
    cost_tier: str
    allowed_models: tuple[str, ...]
    excluded_models: tuple[str, ...]
    session_id: str
    require_zdr: bool
    max_price: Mapping[str, str] | None = None
    pin_served_model_on_followup: bool = True
    policy_step: str = 'auto'
    pinned_model: str | None = None
    pinned_provider: str | None = None
    #: Providers whose included usage is out for this turn (learning/notice).
    excluded_providers: tuple[str, ...] = ()
    #: native route id -> model patterns: follow-up handoff targets.
    included_native: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_context(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'version': SETTINGS_VERSION,
            'cost_tier': self.cost_tier,
            'allowed_models': list(self.allowed_models),
            'excluded_models': list(self.excluded_models),
            'session_id': self.session_id,
            'require_zdr': self.require_zdr,
            'pin_served_model_on_followup': self.pin_served_model_on_followup,
            'policy_step': self.policy_step,
        }
        if self.max_price:
            out['max_price'] = dict(self.max_price)
        if self.pinned_model:
            out['pinned_model'] = self.pinned_model
        if self.pinned_provider:
            out['pinned_provider'] = self.pinned_provider
        if self.excluded_providers:
            out['excluded_providers'] = list(self.excluded_providers)
        if self.included_native:
            out['included_native'] = {k: list(v) for k, v in self.included_native.items()}
        return out


_PINNED_PROVIDER_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}')
_POLICY_STEP_RE = re.compile(r'[a-z_]{1,40}')


def parse_turn_settings(raw: Any) -> TurnSettings:
    """Strict validation of the context block (executor side)."""
    keys = {
        'version',
        'cost_tier',
        'allowed_models',
        'excluded_models',
        'session_id',
        'require_zdr',
        'max_price',
        'pin_served_model_on_followup',
        'policy_step',
        'pinned_model',
        'pinned_provider',
        'excluded_providers',
        'included_native',
    }
    if not isinstance(raw, dict) or set(raw) - keys or raw.get('version') != SETTINGS_VERSION:
        _fail('auto_settings.shape')
    if raw.get('cost_tier') not in COST_TIERS:
        _fail('auto_settings.cost_tier')
    session_id = raw.get('session_id')
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        _fail('auto_settings.session_id')
    if not isinstance(raw.get('require_zdr'), bool):
        _fail('auto_settings.require_zdr')
    pin_flag = raw.get('pin_served_model_on_followup', True)
    if not isinstance(pin_flag, bool):
        _fail('auto_settings.pin_flag')
    max_price = raw.get('max_price')
    if max_price is not None:
        if (
            not isinstance(max_price, dict)
            or not max_price
            or (set(max_price) - {'prompt', 'completion'})
        ):
            _fail('auto_settings.max_price')
        max_price = {k: _price(v, 'auto_settings.max_price') for k, v in max_price.items()}
    step = raw.get('policy_step', 'auto')
    if not isinstance(step, str) or not _POLICY_STEP_RE.fullmatch(step):
        _fail('auto_settings.policy_step')
    pinned_model = raw.get('pinned_model')
    if pinned_model is not None and (
        not isinstance(pinned_model, str) or not SERVED_MODEL_RE.fullmatch(pinned_model)
    ):
        _fail('auto_settings.pinned_model')
    pinned_provider = raw.get('pinned_provider')
    if pinned_provider is not None and (
        not isinstance(pinned_provider, str) or not _PINNED_PROVIDER_RE.fullmatch(pinned_provider)
    ):
        _fail('auto_settings.pinned_provider')
    excluded_providers = raw.get('excluded_providers', [])
    if not isinstance(excluded_providers, list) or not all(
        isinstance(v, str) and _PROVIDER_RE.fullmatch(v) for v in excluded_providers
    ):
        _fail('auto_settings.excluded_providers')
    included_native_raw = raw.get('included_native', {})
    if not isinstance(included_native_raw, dict) or not all(
        isinstance(k, str) and _ROUTE_ID_RE.fullmatch(k) for k in included_native_raw
    ):
        _fail('auto_settings.included_native')
    included_native = {
        k: _patterns(v, 'auto_settings.included_native') for k, v in included_native_raw.items()
    }
    return TurnSettings(
        cost_tier=raw['cost_tier'],
        allowed_models=_patterns(raw.get('allowed_models'), 'auto_settings.allowed_models'),
        excluded_models=_patterns(raw.get('excluded_models'), 'auto_settings.excluded_models'),
        session_id=session_id,
        require_zdr=raw['require_zdr'],
        max_price=max_price,
        pin_served_model_on_followup=pin_flag,
        policy_step=step,
        pinned_model=pinned_model,
        pinned_provider=pinned_provider,
        excluded_providers=tuple(excluded_providers),
        included_native=included_native,
    )


def providers_out(
    plans: Sequence[DeclaredPlan],
    route_pins: Mapping[str, Any],
    hard_stopped_route_ids: Iterable[str],
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    """Providers whose included usage is out: declared exhausted/expired, or
    a hard-stopped (e.g. auto-detected 429) native route for that provider."""
    current = time.time() if now is None else now
    out = {plan.provider for plan in plans if plan.mode == 'included' and plan.exhausted(current)}
    for route_id in hard_stopped_route_ids:
        tag = getattr(route_pins.get(route_id), 'provider_tag', '') or ''
        for plan in plans:
            if plan.matches(route_id, tag):
                out.add(plan.provider)
    return tuple(sorted(out))


def included_native_routes(
    plans: Sequence[DeclaredPlan],
    route_ids: Sequence[str],
    route_pins: Mapping[str, Any],
    excluded_providers: Iterable[str],
    *,
    now: float | None = None,
) -> dict[str, tuple[str, ...]]:
    """Granted native route -> its included plan's model patterns, for plans
    that are included, usable, handoff-enabled and not currently out."""
    current = time.time() if now is None else now
    out_set = set(excluded_providers)
    result: dict[str, tuple[str, ...]] = {}
    for plan in plans:
        if (
            not plan.native_handoff
            or not plan.model_patterns
            or plan.provider in out_set
            or not plan.usable_included(current)
        ):
            continue
        for route_id in route_ids:
            pin = route_pins.get(route_id)
            if pin is not None and plan.matches(route_id, getattr(pin, 'provider_tag', '')):
                result.setdefault(route_id, plan.model_patterns)
    return result


def build_turn_settings(
    settings: AutoRouterSettings,
    *,
    thread: Sequence[str],
    query: str,
    granted_capabilities: Sequence[str],
    service_names: Iterable[str] = (),
    model_only: bool = False,
    hard_stopped_models: Iterable[str] = (),
    plans: Sequence[DeclaredPlan] = (),
    excluded_providers: Sequence[str] = (),
    included_native: Mapping[str, tuple[str, ...]] | None = None,
) -> TurnSettings:
    caps = tuple(granted_capabilities)
    out = set(excluded_providers)
    excluded = tuple(
        dict.fromkeys(
            (
                *settings.excluded_models,
                *(p for plan in plans if plan.provider in out for p in plan.exclusions_when_out()),
                *(m for m in hard_stopped_models if isinstance(m, str) and m != AUTO_MODEL),
            )
        )
    )[:MAX_PATTERNS]
    return TurnSettings(
        cost_tier=cost_tier_for(
            query,
            caps,
            model_only=model_only,
            default=settings.default_cost_tier,
            complexity=settings.complexity_tiers,
        ),
        allowed_models=settings.allowed_models,
        excluded_models=excluded,
        session_id=session_id_for(thread),
        require_zdr=touches_sensitive_data((*caps, *service_names), settings.sensitive_patterns),
        max_price=settings.max_price,
        pin_served_model_on_followup=settings.pin_served_model_on_followup,
        policy_step='auto',
        excluded_providers=tuple(sorted(out)),
        included_native=dict(included_native or {}),
    )


def order_routes(route_ids: Sequence[str], auto_route_id: str | None) -> tuple[str, ...]:
    """Auto always leads; every other granted route stays, in its existing
    order, as a backup. Routes are never added -- only reordered."""
    if auto_route_id is None or auto_route_id not in route_ids:
        return tuple(route_ids)
    return (auto_route_id, *(r for r in route_ids if r != auto_route_id))


def native_handoff_route(
    settings_context: Any, served_model: Any, candidate_route_ids: Sequence[str]
) -> str | None:
    """The native route a follow-up should move to, or None."""
    if not isinstance(settings_context, dict) or not isinstance(served_model, str):
        return None
    mapping = settings_context.get('included_native')
    if not isinstance(mapping, dict):
        return None
    for route_id, patterns in mapping.items():
        if route_id not in candidate_route_ids or not isinstance(patterns, list):
            continue
        if any(isinstance(p, str) and fnmatch.fnmatchcase(served_model, p) for p in patterns):
            return route_id
    return None


def plans_summary(plans: Sequence[DeclaredPlan], now: float | None = None) -> list[dict[str, Any]]:
    """JSON-safe plan facts for the learning receipt (no notes text)."""
    current = time.time() if now is None else now
    return [
        {
            'provider': plan.provider,
            'mode': plan.mode,
            'effective_cost_fraction': str(plan.effective_cost_fraction),
            'quota_status': plan.quota_status,
            'exhausted': plan.exhausted(current),
            'usable_included': plan.usable_included(current),
        }
        for plan in plans
    ]


# ----------------------------------------------------------------------
# Follow-up stickiness store (executor-owned, durable JSON file)
# ----------------------------------------------------------------------

_MAX_PINS = 2000
_PIN_TTL_SECONDS = 30 * 86400


class ServedModelPins:
    """thread key -> the model/provider Auto served on the thread's first turn."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, thread_key: str) -> tuple[str, str | None] | None:
        entry = self._read().get(thread_key)
        if not isinstance(entry, dict):
            return None
        model = entry.get('model')
        provider = entry.get('provider')
        if not isinstance(model, str) or not SERVED_MODEL_RE.fullmatch(model):
            return None
        if provider is not None and (
            not isinstance(provider, str) or not _PINNED_PROVIDER_RE.fullmatch(provider)
        ):
            provider = None
        return model, provider

    def put(self, thread_key: str, model: str, provider: str | None, now: float | None = None):
        if not SERVED_MODEL_RE.fullmatch(model) or model == AUTO_MODEL:
            return
        current = time.time() if now is None else now
        data = self._read()
        if thread_key in data:
            return  # first served model wins; never re-pinned by a later turn
        data = {
            key: value
            for key, value in data.items()
            if isinstance(value, dict)
            and isinstance(value.get('at'), (int, float))
            and current - value['at'] < _PIN_TTL_SECONDS
        }
        if len(data) >= _MAX_PINS:
            for key in sorted(data, key=lambda k: data[k]['at'])[: len(data) - _MAX_PINS + 1]:
                del data[key]
        data[thread_key] = {
            'model': model,
            'provider': provider
            if isinstance(provider, str) and _PINNED_PROVIDER_RE.fullmatch(provider)
            else None,
            'at': current,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, sort_keys=True), encoding='utf-8')
        tmp.replace(self.path)


def provider_slug(name: str) -> str:
    """Best-effort OpenRouter provider slug from a display name (soft order)."""
    return re.sub(r'[^a-z0-9.-]+', '-', name.strip().casefold()).strip('-')[:64]
