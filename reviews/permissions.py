from .models import BusinessProfile, TeamInvite


def get_business_context(user):
    """
    Resolves who a logged-in user is acting as: either the business owner
    themselves, or an invited staff member linked to an owner's account.
    Returns (profile, role) — role is 'owner', 'admin', 'reviewer', 'viewer',
    or None if this user has no business access at all.

    NOTE: a user can currently only act as ONE business. If someone owns a
    business AND is separately invited to another one, only their owned
    business is returned here — the invite is intentionally deprioritized,
    not lost. This is a known limitation, not a bug: proper multi-business
    support needs a session-based "active business" switcher, not just a
    reordering of this check.
    """
    profile = BusinessProfile.objects.filter(user=user).first()
    if profile:
        return profile, 'owner'

    membership = TeamInvite.objects.filter(linked_user=user).select_related('owner').first()
    if membership:
        owner_profile = BusinessProfile.objects.filter(user=membership.owner).first()
        if owner_profile:
            return owner_profile, membership.role

    return None, None


def get_or_create_owned_profile(user):
    """
    Only creates a new BusinessProfile if this user has NO business
    relationship at all (a genuine new owner signing up). If they're
    already linked as invited staff, or already own a profile, this
    never silently creates a second one.
    """
    profile, role = get_business_context(user)
    if profile is not None:
        return profile, role
    profile, _ = BusinessProfile.objects.get_or_create(user=user)
    return profile, 'owner'



def can_manage_settings(role):
    return role in ('owner', 'admin')


def can_approve_reviews(role):
    return role in ('owner', 'admin', 'reviewer')


def check_ai_quota(profile, amount=1):
    """
    Enforces a daily cap on Gemini-calling actions per business, so a bug,
    abuse, or someone mashing 'Regenerate' can't run up the API bill.
    Resets automatically at local midnight. Call this BEFORE making any
    Gemini API call — returns True and consumes `amount` units if the
    WHOLE amount fits within today's remaining quota, False otherwise
    (nothing is consumed on a False return — an all-or-nothing check,
    so a bilingual preview needing 2 units doesn't burn 1 and then fail).

    NOTE: read-then-write, not atomic under heavy concurrent load — fine
    for now, but revisit with select_for_update() if traffic ever makes
    that a real risk.
    """
    from django.utils import timezone
    today = timezone.localdate()

    if profile.ai_last_generation_date != today:
        profile.ai_generations_today = 0
        profile.ai_last_generation_date = today

    if profile.ai_generations_today + amount > profile.ai_daily_limit:
        profile.save(update_fields=['ai_generations_today', 'ai_last_generation_date'])
        return False

    profile.ai_generations_today += amount
    profile.save(update_fields=['ai_generations_today', 'ai_last_generation_date'])
    return True