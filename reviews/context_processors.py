def billing_status(request):
    """Makes `billing_access` (see services/billing.py) available in every template."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {}
    from reviews.permissions import get_business_context
    from reviews.services import billing

    profile, role = get_business_context(user)
    if profile is None:
        return {}
    return {'billing_access': billing.get_access(profile)}



def site_contact(request):
    """SUPPORT_EMAIL for every page (also logged-out ones like the landing page)."""
    from django.conf import settings
    return {'SUPPORT_EMAIL': settings.SUPPORT_EMAIL}
