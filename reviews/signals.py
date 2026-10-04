from django.dispatch import receiver
from .models import TeamInvite
from django.contrib.auth.signals import user_logged_in
from .models import UserSession


def _link_pending_invites(user):
    """
    Links a pending invite on login ONLY when this user has PROVEN they own
    the invited address (a verified email, e.g. signed in with Google).

    Before: any invite was linked to whoever signed up with that email, and
    email verification is off, so a stranger could type the invited address
    at signup and walk into the team. Everyone else joins through the secret
    link in the invite email (accept_invite_view).
    """
    from allauth.account.models import EmailAddress
    from django.utils import timezone

    if TeamInvite.objects.filter(linked_user=user).exists():
        return  # already on a team (one team per account)
    verified = list(
        EmailAddress.objects.filter(user=user, verified=True).values_list('email', flat=True)
    )
    for email in verified:
        invite = TeamInvite.objects.filter(email__iexact=email, linked_user__isnull=True).first()
        if invite and invite.owner_id != user.id:
            invite.linked_user = user
            invite.accepted_at = timezone.now()
            invite.save(update_fields=['linked_user', 'accepted_at'])
            return


def _get_client_ip(request):
    from reviews.services.client_ip import get_client_ip
    ip = get_client_ip(request)
    return None if ip == 'unknown' else ip


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