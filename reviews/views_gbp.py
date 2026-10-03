"""
Views for the optional "Connect Google Business Profile" flow.

connect    -> sends the owner to Google's sign-in page
callback   -> Google sends them back here with ?code=...
select     -> pick which location (restaurant) to use, if they have several
disconnect -> forget the connection
"""
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import ActivityLog
from .permissions import get_business_context, get_or_create_owned_profile, can_manage_settings
from .services import gbp_client


def _get_profile_or_deny(request):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not can_manage_settings(role):
        messages.error(request, "Only account owners and admins can connect Google Business Profile.")
        return None
    return profile


def _all_locations(profile):
    """Flat list of every location across the user's Google accounts."""
    out = []
    for acc in gbp_client.list_accounts(profile):
        for loc in gbp_client.list_locations(profile, acc["name"]):
            out.append({
                "account_name": acc["name"],
                "location_name": loc["name"],
                "title": loc["title"] or acc["title"],
                "address": loc["address"],
                "value": f'{acc["name"]}|{loc["name"]}',
            })
    return out


def _save_choice(request, profile, loc):
    gbp_client.save_location(profile, loc["account_name"], loc["location_name"])
    ActivityLog.objects.create(
        user=request.user, action="settings_updated",
        detail=f"Google Business Profile connected: {loc['title']}"[:255],
    )
    messages.success(request, f"Connected to {loc['title']} on Google. Replies can now be posted automatically.")


@login_required
def gbp_connect_view(request):
    profile = _get_profile_or_deny(request)
    if profile is None:
        return redirect("dashboard")

    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        messages.error(request, "Google connection isn't configured on the server yet.")
        return redirect("dashboard")

    state = secrets.token_urlsafe(32)
    request.session["gbp_oauth_state"] = state
    return redirect(gbp_client.build_auth_url(state))


@login_required
def gbp_callback_view(request):
    profile = _get_profile_or_deny(request)
    if profile is None:
        return redirect("dashboard")

    if request.GET.get("error"):
        messages.info(request, "Google connection was cancelled.")
        return redirect("dashboard")

    expected = request.session.pop("gbp_oauth_state", None)
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if not expected or not code or not secrets.compare_digest(state, expected):
        messages.error(request, "Google connection failed a security check. Please try again.")
        return redirect("dashboard")

    try:
        tokens = gbp_client.exchange_code(code)
        gbp_client.store_refresh_token(profile, tokens["refresh_token"])
    except gbp_client.GBPError as e:
        messages.error(request, f"Couldn't finish connecting Google: {e}")
        return redirect("dashboard")

    try:
        locations = _all_locations(profile)
    except gbp_client.GBPError as e:
        gbp_client.disconnect(profile)
        messages.error(request, f"Signed in, but couldn't load your business locations: {e}")
        return redirect("dashboard")

    if not locations:
        gbp_client.disconnect(profile)
        messages.warning(request, "That Google account doesn't manage any business locations.")
        return redirect("dashboard")

    if len(locations) == 1:
        _save_choice(request, profile, locations[0])
        return redirect("dashboard")

    return redirect("gbp_select_location")


@login_required
def gbp_select_location_view(request):
    profile = _get_profile_or_deny(request)
    if profile is None:
        return redirect("dashboard")

    if not profile.google_business_refresh_token:
        messages.info(request, "Connect Google Business Profile first.")
        return redirect("dashboard")

    try:
        locations = _all_locations(profile)
    except gbp_client.GBPError as e:
        messages.error(request, f"Couldn't load your locations: {e}")
        return redirect("dashboard")

    if request.method == "POST":
        choice = request.POST.get("choice", "")
        match = next((l for l in locations if l["value"] == choice), None)  # never trust the raw value
        if not match:
            messages.error(request, "Please pick one of your locations.")
            return redirect("gbp_select_location")
        _save_choice(request, profile, match)
        return redirect("dashboard")

    return render(request, "reviews/gbp_select_location.html", {"locations": locations, "profile": profile})


@login_required
@require_POST
def gbp_disconnect_view(request):
    profile = _get_profile_or_deny(request)
    if profile is None:
        return redirect("dashboard")
    gbp_client.disconnect(profile)
    ActivityLog.objects.create(user=request.user, action="settings_updated", detail="Google Business Profile disconnected")
    messages.info(request, "Google Business Profile disconnected. Replies go back to copy-and-paste.")
    return redirect("dashboard")