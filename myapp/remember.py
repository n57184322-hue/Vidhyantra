"""Stay signed in when the session is lost.

Every login also gets a "remember" token: a random selector and validator
in an HttpOnly cookie, with only a SHA-256 hash of the validator kept in the
database. If the browser later arrives without a working session (a deploy
that changed DJANGO_SECRET_KEY, a cleared session table, an expired session)
but with a valid token, it is logged straight back in.

It never outlives what a session itself could not:

* The token belongs to one device (ActiveUserSession). Logging out, a
  device being logged out from "Connected devices", or a password login on
  another device that takes over the main slot (see myapp.devices) deletes
  the device and, with it, the token.
* A password change on the account makes every token stop working.
* Inactive accounts and expired tokens are refused.

Nothing here depends on SECRET_KEY, so a changed or leaked key cannot be
used to forge a token.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login
from django.db import IntegrityError, transaction
from django.utils import timezone

COOKIE_NAME = 'vd_keep'
LIFETIME = timedelta(days=365)
SKIP_PREFIXES = ('/static/', '/media/', '/favicon')


def _sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _password_check(user):
    """Changes whenever the password does, so old tokens stop working."""
    return _sha256(user.password or '')


def issue(request, user):
    """Give the device this request is logged in on a fresh token. The
    cookie is written on the way out by RememberMeMiddleware."""
    from myapp.models import ActiveUserSession, RememberedLogin
    session_key = request.session.session_key
    device = ActiveUserSession.objects.filter(user=user, session_key=session_key).first() if session_key else None
    if device is None:
        return None
    selector, validator = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
    with transaction.atomic():
        RememberedLogin.objects.filter(device=device).delete()
        RememberedLogin.objects.create(
            device=device, user=user, selector=selector, validator_hash=_sha256(validator),
            password_check=_password_check(user), expires_at=timezone.now() + LIFETIME,
        )
    request._remember_cookie = f'{selector}:{validator}'
    return selector


def _find(raw):
    """The token a cookie value stands for, or None (deleting a token whose
    selector matched but whose validator, password or account no longer does)."""
    from myapp.models import RememberedLogin
    selector, _, validator = (raw or '').partition(':')
    if not selector or not validator or len(selector) > 32 or len(validator) > 64:
        return None
    token = RememberedLogin.objects.select_related('user', 'device').filter(selector=selector).first()
    if token is None:
        return None
    if (not hmac.compare_digest(token.validator_hash, _sha256(validator))
            or token.expires_at <= timezone.now()
            or not token.user.is_active
            or not hmac.compare_digest(token.password_check, _password_check(token.user))):
        token.delete()
        return None
    return token


def _backend():
    backends = getattr(settings, 'AUTHENTICATION_BACKENDS', None) or ['django.contrib.auth.backends.ModelBackend']
    return backends[0]


def restore(request):
    """Log a signed-out visitor back in from their cookie. True when it did."""
    raw = request.COOKIES.get(COOKIE_NAME)
    if not raw:
        return False
    token = _find(raw)
    if token is None:
        request._remember_clear = True
        return False
    # myapp.signals sees this and moves the device over to the new session
    # instead of treating it as a fresh password login.
    request.remember_restore = token
    login(request, token.user, backend=_backend())
    type(token).objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return True


def rebind(token, session_key):
    """Point the token's device at the session the visitor was just given."""
    from myapp.models import ActiveUserSession
    device = token.device
    try:
        with transaction.atomic():
            ActiveUserSession.objects.filter(session_key=session_key).exclude(pk=device.pk).delete()
            device.session_key = session_key
            device.save(update_fields=['session_key', 'updated_at'])
    except IntegrityError:
        pass


def ensure(request):
    """A device that was logged in before these tokens existed gets one
    on its next visit, once (devices that have one are left alone)."""
    from myapp.models import RememberedLogin
    user = getattr(request, 'user', None)
    if (user is None or not user.is_authenticated or COOKIE_NAME in request.COOKIES
            or getattr(request, '_remember_cookie', None) or getattr(request, '_remember_clear', False)):
        return
    session_key = request.session.session_key
    if session_key and not RememberedLogin.objects.filter(device__session_key=session_key).exists():
        issue(request, user)


def forget(request):
    """On log out: the cookie goes too (the device and its token are deleted
    by myapp.devices.forget_device)."""
    request._remember_clear = True


def write_cookie(request, response):
    value = getattr(request, '_remember_cookie', None)
    if value:
        response.set_cookie(
            COOKIE_NAME, value, max_age=int(LIFETIME.total_seconds()), httponly=True,
            secure=settings.SESSION_COOKIE_SECURE, samesite='Lax', path='/',
        )
    elif getattr(request, '_remember_clear', False) and COOKIE_NAME in request.COOKIES:
        response.delete_cookie(COOKIE_NAME, path='/', samesite='Lax')
    return response
