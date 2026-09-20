"""Authenticated OmniRoute selector-policy snapshot collection.

Local ``recommend`` needs a fresh server-side policy snapshot for every
configured ``routed_pool`` route; asking operators to hand a per-task file
to ``--routed-policy-snapshot`` does not scale. This module is the bounded
control-plane collector: given a catalog pool route and its provider block,
it derives the fixed ``/v1/selector-policy?selector=<selector>`` endpoint
from the provider's reviewed ``base_url``, authenticates with the
provider's configured credential (read in memory only, never logged or
copied), applies a bounded timeout and response size, and validates the
returned snapshot through the same :func:`routed_pool.match_snapshot`
contract an operator file goes through — freshness, exact selector, pinned
policy revision/fingerprint, full member attestation, caps, provenance,
and the fail-closed context-fit marker.

Failure is fail-closed: :class:`SelectorPolicyError` carries only a
sanitized reason code, never a URL query payload beyond the selector, a
response body, or a credential value. A failed collection produces no
snapshot, so the pool route stays ineligible on
``routed-policy-snapshot-absent`` — there is no stale-file or direct-route
fallback inside this module.

This module makes one read-only HTTPS GET to the router's control plane.
It never calls a model or upstream inference endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from side_lane import routed_pool as _pool
from side_lane.credentials import read_credential

COLLECTION_PATH = "/v1/selector-policy"
# A snapshot is small receipt metadata; 64 KiB is far above any legitimate
# payload and bounds memory/parse cost on a hostile or broken endpoint.
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
# Callers may shorten the timeout but never lengthen it past this bound.
MAX_TIMEOUT_SECONDS = 30.0
# Provider-block opt-in flag in the runtime model allowlist. Automatic
# collection is explicit, config-driven, and only meaningful for the
# routed provider; ordinary routes are untouched.
AUTOMATIC_COLLECTION_FLAG = "automatic_selector_policy"


class SelectorPolicyError(ValueError):
    """Collection failed closed; ``reason`` is a sanitized reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _RefuseRedirect(HTTPRedirectHandler):
    """Refuse every HTTP redirect before a second request is issued.

    ``urllib.request``'s default redirect handler would re-send the
    request — including the bearer ``Authorization`` header — to the
    redirect target, whether same-host HTTPS or a cross-host/plain-HTTP
    downgrade. Returning ``None`` makes the opener raise ``HTTPError``
    for any 3xx instead, so the credential never leaves the one fixed
    endpoint.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Module-level opener: fixed endpoint, no redirect following, no proxy or
# cookie state beyond urllib's defaults.
_REDIRECT_REFUSING_OPENER = build_opener(_RefuseRedirect())


def selector_policy_endpoint(base_url: Any, selector: Any) -> str:
    """Derive the fixed collection endpoint from a reviewed provider base URL.

    Only the origin (scheme + authority) of ``base_url`` is trusted: the
    scheme must be HTTPS, credentials-in-URL and query/fragment/path
    components are rejected, and the request path is always the fixed
    ``/v1/selector-policy``. Arbitrary URL, path, or header overrides are
    not accepted anywhere in this module.
    """

    if not isinstance(base_url, str) or not base_url:
        raise SelectorPolicyError("selector-policy-endpoint-invalid")
    if not isinstance(selector, str) or not selector:
        raise SelectorPolicyError("selector-policy-endpoint-invalid")
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise SelectorPolicyError("selector-policy-endpoint-invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or any(char.isspace() for char in base_url)
    ):
        raise SelectorPolicyError("selector-policy-endpoint-invalid")
    return (
        f"https://{parsed.netloc}{COLLECTION_PATH}"
        f"?selector={quote(selector, safe='')}"
    )


def _reject_secret_shaped(value: Any) -> None:
    """Reject a payload that carries secret-shaped fields or values.

    Same narrow field-name denylist as ``routed_pool._validate_caps``: a
    hygiene check, not a secret detector. Applied to every mapping key and
    string leaf of the response so a credential blob cannot ride inside a
    snapshot the caller then logs or stores.
    """

    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or _pool._SECRET_FIELD.search(key):
                    raise SelectorPolicyError("selector-policy-secret-shaped")
                pending.append(child)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, str) and _pool._SECRET_FIELD.search(item):
            raise SelectorPolicyError("selector-policy-secret-shaped")


def collect_selector_policy(
    route: Mapping[str, Any],
    provider_config: Mapping[str, Any],
    *,
    now_utc: datetime,
    credential_reader: Callable[[str], str] = read_credential,
    opener: Callable[..., Any] = _REDIRECT_REFUSING_OPENER.open,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Fetch and validate one fresh selector-policy snapshot.

    ``route`` is the catalog ``routed_pool`` route and ``provider_config``
    its runtime provider block (``base_url``, ``credential_service``,
    ``auth_method``). Returns ``{"snapshot": ..., "evidence": ...}`` where
    ``snapshot`` is the validated payload to pass to
    ``routing.recommend`` and ``evidence`` is sanitized metadata only
    (selector, observed_at, policy_revision, policy_fingerprint, source).

    ``now_utc`` is the pre-request clock; a server legitimately stamps
    ``observed_at`` after the request was sent, so freshness is checked
    against an effective post-response time sampled immediately after the
    bounded body read — ``clock()`` when injected (deterministic tests),
    otherwise ``datetime.now(timezone.utc)`` — floored at ``now_utc``.
    """

    if provider_config.get("auth_method") != "provider-key":
        raise SelectorPolicyError("selector-policy-auth-unsupported")
    service = provider_config.get("credential_service")
    if not isinstance(service, str) or not service:
        raise SelectorPolicyError("selector-policy-credential-unavailable")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise SelectorPolicyError("selector-policy-timeout-invalid")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 0 < max_bytes <= MAX_RESPONSE_BYTES
    ):
        raise SelectorPolicyError("selector-policy-size-invalid")
    spec = route.get("routed_pool")
    if not isinstance(spec, Mapping):
        raise SelectorPolicyError("selector-policy-not-a-pool-route")
    selector = spec.get("selector")
    url = selector_policy_endpoint(provider_config.get("base_url"), selector)
    try:
        token = credential_reader(service)
    except Exception as exc:
        raise SelectorPolicyError("selector-policy-credential-unavailable") from exc
    if not isinstance(token, str) or not token.strip():
        raise SelectorPolicyError("selector-policy-credential-unavailable")
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        response = opener(request, timeout=timeout_seconds)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise SelectorPolicyError("selector-policy-unauthorized") from exc
        if isinstance(exc.code, int) and 300 <= exc.code < 400:
            raise SelectorPolicyError("selector-policy-redirect-refused") from exc
        raise SelectorPolicyError("selector-policy-http-error") from exc
    except (TimeoutError, URLError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, TimeoutError) or (
            isinstance(reason, OSError) and "timed out" in str(reason).lower()
        ) or isinstance(exc, TimeoutError):
            raise SelectorPolicyError("selector-policy-timeout") from exc
        raise SelectorPolicyError("selector-policy-unreachable") from exc
    except OSError as exc:
        if "timed out" in str(exc).lower():
            raise SelectorPolicyError("selector-policy-timeout") from exc
        raise SelectorPolicyError("selector-policy-unreachable") from exc
    try:
        status = getattr(response, "status", None) or response.getcode()
    except Exception:
        status = None
    if not isinstance(status, int) or not 200 <= status < 300:
        if status in (401, 403):
            raise SelectorPolicyError("selector-policy-unauthorized")
        raise SelectorPolicyError("selector-policy-http-error")
    try:
        body = response.read(max_bytes + 1)
    except OSError as exc:
        raise SelectorPolicyError("selector-policy-unreachable") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if len(body) > max_bytes:
        raise SelectorPolicyError("selector-policy-oversize")
    # Effective freshness clock: sampled after the bounded read so a
    # server-stamped ``observed_at`` between request and response is not
    # rejected as future-dated. Floor at ``now_utc`` so a misbehaving
    # injected clock never widens the freshness window backwards.
    post_now = (
        clock() if clock is not None else datetime.now(timezone.utc)
    )
    effective_now_utc = (
        post_now
        if isinstance(post_now, datetime) and post_now.tzinfo is not None
        else now_utc
    )
    effective_now_utc = max(effective_now_utc, now_utc)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SelectorPolicyError("selector-policy-malformed") from exc
    if not isinstance(payload, dict):
        raise SelectorPolicyError("selector-policy-malformed")
    if payload.get("selector") != selector:
        raise SelectorPolicyError("selector-policy-selector-mismatch")
    _reject_secret_shaped(payload)
    match_reason = _pool.match_snapshot(
        [payload], route, spec, now_utc=effective_now_utc
    )
    if match_reason is not None:
        raise SelectorPolicyError(match_reason)
    evidence = {
        "selector": selector,
        "observed_at": payload.get("observed_at"),
        "policy_revision": payload.get("policy_revision"),
        "policy_fingerprint": payload.get("policy_fingerprint"),
        "source": "automatic-selector-policy-collector",
    }
    return {"snapshot": payload, "evidence": evidence}
