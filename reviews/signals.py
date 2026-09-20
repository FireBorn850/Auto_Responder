from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import TeamInvite
from django.contrib.auth.signals import user_logged_in
from .models import UserSession


def _link_pending_invites(user):
    """
    Shared logic: link any pending TeamInvite matching this user's email
    that hasn't been claimed yet. Called both on brand-new signup and on
    every login, so an invite sent to an *already-existing* account still
    gets linked the next time they sign in — not just at signup time.
    """
    if not user.email:
        return
    TeamInvite.objects.filter(
        email__iexact=user.email, linked_user__isnull=True
    ).update(linked_user=user)


@receiver(post_save, sender=User)
def link_team_invite_on_signup(sender, instance, created, **kwargs):
    if not created:
        return
    _link_pending_invites(instance)


def _get_client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


@receiver(user_logged_in)
def track_session_on_login(sender, request, user, **kwargs):
    _link_pending_invites(user)
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