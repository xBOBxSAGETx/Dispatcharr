"""Shared Xtream Codes credential checks.

Every XC entry point (API, live, VOD, timeshift) authenticates against the
``xc_password`` custom property via these helpers so comparison stays
constant-time and rules stay in one place.
"""

import hmac
import logging
import re

from django.http import JsonResponse

from apps.accounts.models import User
from apps.accounts.throttling import record_xc_failed_auth, xc_failed_auth_limited
from dispatcharr.utils import get_client_ip, network_access_allowed

logger = logging.getLogger(__name__)

# Failure markers used when building error responses (and in tests).
XC_AUTH_NETWORK = "network"
XC_AUTH_CREDENTIALS = "credentials"
XC_AUTH_RATE_LIMIT = "rate_limit"

# ``custom_properties`` key for the per-user "use my own XC login in redirects"
# opt-in. Off (absent) by default.
XC_OWN_LOGIN_KEY = "use_own_xc_login"


def xc_password_matches(user, password):
    """Whether *password* matches *user*'s configured XC password.

    Returns False when the user has no ``xc_password`` set (nothing to
    compare), or when the values differ. Uses ``hmac.compare_digest`` so
    timing does not leak the expected secret. Both sides are compared as
    UTF-8 bytes because ``compare_digest`` raises ``TypeError`` for ``str``
    values containing non-ASCII characters, which a client can send even
    though stored XC passwords are restricted to ASCII-safe characters.
    """
    expected = (user.custom_properties or {}).get("xc_password")
    if not expected:
        return False
    return hmac.compare_digest(str(expected).encode(), str(password).encode())


def uses_own_xc_login(user):
    """Whether provider URLs handed to *user* should carry their own XC login.

    True only for a non-admin user who turned the opt-in on and has an
    ``xc_password``. Admins, internal requests (no user) and users without an
    XC password always keep the account's login.
    """
    if user is None or user.user_level >= User.UserLevel.ADMIN:
        return False
    props = user.custom_properties or {}
    return props.get(XC_OWN_LOGIN_KEY) is True and bool(props.get("xc_password"))


def swap_xc_login(url, user, content_paths):
    """Return *url* with the ``/<user>/<pass>/`` segment set to *user*'s login.

    Only the first ``/<content_path>/<login>/<password>/`` segment is replaced.
    Returns None when the URL has no such segment, so the caller can refuse
    rather than hand out the account's login.
    """
    pattern = r"/(%s)/[^/]+/[^/]+/" % "|".join(map(re.escape, content_paths))
    username = user.username
    password = user.custom_properties["xc_password"]
    new_url, count = re.subn(
        pattern,
        lambda m: f"/{m.group(1)}/{username}/{password}/",
        url,
        count=1,
    )
    return new_url if count else None


def get_xc_user(username):
    """Return the User for *username*, or None when the account does not exist."""
    if not username:
        return None
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        return None


def xc_auth_error_response(failure, retry_after=None):
    """HTTP response for an ``authorize_xc_client`` failure reason."""
    if failure == XC_AUTH_NETWORK:
        return JsonResponse({"error": "Forbidden"}, status=403)
    if failure == XC_AUTH_RATE_LIMIT:
        response = JsonResponse({"error": "Too many attempts"}, status=429)
        if retry_after is not None:
            response["Retry-After"] = str(max(1, int(retry_after)))
        return response
    return JsonResponse({"error": "Unauthorized"}, status=401)


def _xc_credential_failure(request, username):
    """Record a failed credential attempt and return the 401 response.

    When this failure crosses the per-IP rate limit, emit one ``login_failed``
    system event and (subject to cooldown) an admin inbox notice.
    """
    if record_xc_failed_auth(request):
        _emit_xc_auth_lockout(request, username)
    return None, xc_auth_error_response(XC_AUTH_CREDENTIALS)


def _emit_xc_auth_lockout(request, username):
    """Log the lockout trip and maybe refresh the cooldown inbox notice."""
    from core.utils import log_system_event, notify_xc_auth_lockout_inbox

    client_ip = get_client_ip(request) or "unknown"
    user_agent = request.META.get("HTTP_USER_AGENT", "unknown")
    log_system_event(
        event_type="login_failed",
        user=username or "unknown",
        client_ip=client_ip,
        user_agent=user_agent,
        reason="XC auth rate limited",
    )
    logger.info(
        "XC auth rate limited: user=%s ip=%s",
        username or "unknown",
        client_ip,
    )
    try:
        notify_xc_auth_lockout_inbox()
    except Exception:
        logger.exception("Failed to create XC auth lockout inbox notice")


def authorize_xc_client(request, username, password, network_key):
    """Return ``(user, None)`` on success, or ``(None, error_response)``.

    Runs two ``network_access_allowed`` calls (system, then user). The client
    must pass both.
    - System: CoreSettings CIDR allowlist for *network_key*. Checked first
      with no user so a blocked client never hits the database.
    - User: after the user row is loaded and before the password, so a
      denied client is not told whether the password was right. When the user
      has no ``allowed_networks`` for *network_key*, this follows the system
      allowlist; a non-empty list can only tighten access.

    Failed credential attempts are rate-limited per client IP (shared Redis
    cache, scope ``xc_auth``). Successful auth and network denials do not
    consume that budget.
    """
    # System-wide allowlist (CoreSettings), no user row involved.
    system_network_ok = network_access_allowed(request, network_key, user=None)
    if not system_network_ok:
        return None, xc_auth_error_response(XC_AUTH_NETWORK)

    limited, wait = xc_failed_auth_limited(request)
    if limited:
        return None, xc_auth_error_response(XC_AUTH_RATE_LIMIT, retry_after=wait)

    if not username or not password:
        return _xc_credential_failure(request, username)

    user = get_xc_user(username)
    if user is None:
        return _xc_credential_failure(request, username)

    # Per-user allowed_networks (and system list again inside the helper).
    user_network_ok = network_access_allowed(request, network_key, user=user)
    if not user_network_ok:
        return None, xc_auth_error_response(XC_AUTH_NETWORK)

    if not xc_password_matches(user, password):
        return _xc_credential_failure(request, username)

    return user, None
