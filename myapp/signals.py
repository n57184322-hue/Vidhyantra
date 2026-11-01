from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models import F
from django.dispatch import receiver

from . import remember
from .devices import forget_device, register_device
from .models import StoreProfile


@receiver(user_logged_in)
def claim_device_login(sender, request, user, **kwargs):
    if request is None:
        return
    if not request.session.session_key:
        request.session.save()
    restoring = getattr(request, 'remember_restore', None)
    if restoring is not None:
        # Signed back in by its "stay signed in" token (myapp.remember): the
        # same device, now under a new session — not a new login.
        remember.rebind(restoring, request.session.session_key)
        return
    # myapp.device_links sets this while logging a device in with a link.
    register_device(user, request.session.session_key, request,
                    via_link=getattr(request, 'device_login_link', False))
    profile, _ = StoreProfile.objects.get_or_create(user=user)
    StoreProfile.objects.filter(pk=profile.pk).update(login_count=F('login_count') + 1)
    remember.issue(request, user)


@receiver(user_logged_out)
def release_device_login(sender, request, user, **kwargs):
    if request is None:
        return
    remember.forget(request)
    if user is None:
        return
    forget_device(user, request.session.session_key)
