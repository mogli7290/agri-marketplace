"""Rate limiting for the public HTML forms.

The API is already metered by DRF (``DEFAULT_THROTTLE_CLASSES``), but DRF only
sees views it dispatches. The login, register, and password-reset pages are
plain Django ``FormView``s, so until now nothing stopped anyone from trying a
password a thousand times a minute or using the reset form to mail-bomb a
stranger. Those are the cheapest attacks in the app and the ones that get a
free-tier deployment banned.

Design:

* **Cache-backed, not in-process.** A counter in the default cache survives a
  worker restart, and with ``REDIS_URL`` set it is shared across every gunicorn
  worker, which is what makes the limit mean anything in production. Locally it
  falls back to ``LocMemCache`` — still correct for one process, which is all a
  dev server is.
* **Per client IP, with a graceful fallback.** ``X-Forwarded-For`` is only
  trusted when the app is behind a proxy we recognise; otherwise a client could
  forge a fresh identity per request. When no address can be determined at all
  the request is counted under a single shared key rather than waved through —
  failing closed is the safer default for a security control.
* **Written to refuse, not to explain.** A throttle returns ``429`` and a short
  message. It deliberately does not say how many attempts remain.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)

#: Used when the client address cannot be determined. Collapsing unknown
#: callers onto one key means a flood without a usable address throttles
#: itself rather than escaping the limit entirely.
UNKNOWN_CLIENT = "unknown"

#: Minutes a blocked client stays blocked. Kept modest so a shared-address
#: office or a mobile carrier NAT recovers without an admin.
DEFAULT_BLOCK_MINUTES = 15


def client_key(request) -> str:
    """A stable, non-forgeable identity for ``request``.

    Behind a proxy the socket address is the proxy, so the forwarded client is
    the real identity — but only when the deployment actually runs behind one.
    In DEBUG the app is usually reached directly, where ``X-Forwarded-For`` is
    attacker-controlled and must be ignored.
    """
    if settings.DEBUG:
        return request.META.get("REMOTE_ADDR") or UNKNOWN_CLIENT

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # Left-most entry is the original client; the rest are proxies we add.
        return forwarded.split(",")[0].strip() or UNKNOWN_CLIENT

    return request.META.get("REMOTE_ADDR") or UNKNOWN_CLIENT


def _limit_for(scope: str) -> tuple[int, int]:
    """(attempts, window seconds) for a named scope, from settings."""
    defaults = {
        "login": (10, 300),
        "register": (5, 3600),
        "password_reset": (5, 3600),
        "verify_resend": (5, 3600),
    }
    attempts, window = defaults.get(scope, (10, 300))
    return attempts, window


def _count_key(scope: str, identity: str) -> str:
    return f"ratelimit:{scope}:{identity}:count"


def _block_key(scope: str, identity: str) -> str:
    return f"ratelimit:{scope}:{identity}:blocked"


def is_limited(scope: str, identity: str) -> int:
    """Seconds until ``identity`` may use ``scope`` again; 0 if allowed now.

    This reads the *block* key, not the counter. They are separate on purpose:
    the counter is a plain number of attempts, and reading that number back as
    if it were "seconds remaining" blocks everyone after a single try.
    """
    seconds_left = cache.get(_block_key(scope, identity))
    if not seconds_left:
        return 0
    try:
        return max(0, int(seconds_left))
    except (TypeError, ValueError):
        return 0


def record(scope: str, identity: str, *, count: bool = True) -> int:
    """Count one attempt. Returns seconds left blocked (0 when under the cap).

    A cache failure must never lock a real user out of their own account, so
    every error here degrades to "allowed" and is logged rather than raised.
    """
    if not count:
        return 0

    attempts, window = _limit_for(scope)
    key = _count_key(scope, identity)
    try:
        # add() only succeeds on first use, so a flurry of attempts cannot keep
        # resetting the window — otherwise a fast attacker is never blocked.
        if not cache.add(key, 1, window):
            try:
                cache.incr(key)
            except ValueError:
                # The key expired between add() and incr(); start a new window.
                cache.set(key, 1, window)
        total = int(cache.get(key) or 1)
    except Exception as exc:  # noqa: BLE001 - never block a user on cache health
        logger.warning("Rate-limit counter unavailable for %s: %s", scope, exc)
        return 0

    if total > attempts:
        # Block for the remainder of the window, and remember it so that
        # dispatch() can refuse without re-counting on every later request.
        try:
            cache.set(_block_key(scope, identity), window, window)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record the %s block: %s", scope, exc)
        logger.warning(
            "%s rate limit exceeded for %s (%s attempts)", scope, identity, total
        )
        return window
    return 0


def reset(scope: str, identity: str) -> None:
    """Clear both the counter and any block — only after real success."""
    try:
        cache.delete_many([_count_key(scope, identity), _block_key(scope, identity)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not clear the %s rate limit: %s", scope, exc)


def throttle_response(seconds: int) -> HttpResponse:
    """A plain, honest 429. No attempt counts leak back to the caller."""
    minutes = max(1, round(seconds / 60))
    response = HttpResponse(
        f"Too many attempts. Please wait {minutes} minute"
        f"{'s' if minutes != 1 else ''} and try again.",
        status=429,
        content_type="text/plain",
    )
    response["Retry-After"] = str(max(1, int(seconds)))
    return response
