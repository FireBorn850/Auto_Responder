from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import TeamInvite
from django.contrib.auth.signals import user_logged_in
from .models import UserSession


@receiver(post_save, sender=User)
def link_team_invite_on_signup(sender, instance, created, **kwargs):
    """
    The moment a new account is created, check if the email matches a
    pending TeamInvite with no linked_user yet. If so, link it — this is
    what turns a recorded invite into real, enforceable staff access.
    """
    if not created or not instance.email:
        return
    TeamInvite.objects.filter(
        email__iexact=instance.email, linked_user__isnull=True
    ).update(linked_user=instance)


def _get_client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


@receiver(user_logged_in)
def track_session_on_login(sender, request, user, **kwargs):
    if not request.session.session_key:
        request.session.save()
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={
            'user': user,
            'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
            'ip_address': _get_client_ip(request),
        }
    )