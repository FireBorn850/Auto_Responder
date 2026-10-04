from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from django.db.models import Avg, F, Count
from django.db.models.functions import TruncDate, ExtractHour
from datetime import timedelta
import uuid
from django.utils.text import slugify
from django.http import HttpResponse, FileResponse
from .services.qr_generator import generate_qr_with_logo
from .services.pdf_templates import generate_table_tent_pdf, generate_sticker_sheet_pdf, generate_door_sign_pdf
from .models import Review, BusinessProfile, SmartQRCode, Competitor, TeamInvite, EditLog, ActivityLog, QRScanEvent, SyncLog, AccessCode, SyncJob
from .services import sync_jobs
from .services.ai_responder import generate_review_draft, analyze_complaints, is_authentic_review, analyze_review_sentiment, detect_review_language, detect_seo_keyword_used, append_action_link
from .services.google_api import post_reply_to_google
from .services.google_importer import fetch_live_google_reviews
from .services.tripadvisor_importer import fetch_live_tripadvisor_reviews
from .services.review_pipeline import draft_reply
from .permissions import get_business_context, get_or_create_owned_profile, can_manage_settings, can_approve_reviews, check_ai_quota
from django.contrib.sessions.models import Session
from .models import UserSession
from django.core.mail import send_mail
import csv
import io
import re
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from .services.exceptions import RateLimitError
from django.core.paginator import Paginator
from django.db.models import DurationField, ExpressionWrapper
from django.db.models.functions import TruncWeek
from .services.ai_responder import analyze_complaints
import secrets
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime
from .services import gbp_client, billing, polar_billing, ratelimit
from .services.client_ip import get_client_ip
from .services.safe_csv import safe_csv_writer
import logging
logger = logging.getLogger(__name__)
from .models import generate_founder_code
from django.urls import reverse



# ==========================================
# 1. PUBLIC VIEWS
# ==========================================

# ---------------------------------------------------------------- team roles
# owner / admin : everything (settings, syncs, keys, simulator, team)
# reviewer      : read + generate and approve replies
# viewer        : read-only (dashboard, exports, insights)
MANAGE = ('owner', 'admin')
APPROVE = ('owner', 'admin', 'reviewer')
ROLE_DENIED = "Your team role doesn't allow this — ask the owner or an admin."


def role_denied(request, allowed, as_json=False):
    """
    None if the current user's team role is in `allowed`; otherwise a ready
    response (JSON 403 or a redirect with a message). A user with no team
    link is a (new) owner.
    """
    _, role = get_business_context(request.user)
    if role is None or role in allowed:
        return None
    if as_json:
        return JsonResponse({'error': ROLE_DENIED}, status=403)
    messages.error(request, ROLE_DENIED)
    return redirect('dashboard')


def landing_page(request):
    """Public SaaS homepage introducing bilingual AI review management."""
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'reviews/landing.html')


def privacy_policy_view(request):
    return render(request, 'reviews/privacy_policy.html')

def getting_started_view(request):
    return render(request, 'reviews/getting_started.html')


def terms_of_service_view(request):
    return render(request, 'reviews/terms_of_service.html')


def refund_policy_view(request):
    return render(request, 'reviews/refund_policy.html')


def security_view(request):
    return render(request, 'reviews/security.html')



# ==========================================
# BILLING (Polar — Merchant of Record)
# ==========================================

@login_required
def billing_page_view(request):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    starter_yearly = ('starter', 'year') in polar_billing.product_map().values()
    return render(request, 'reviews/billing.html', {
        'profile': profile,
        'access': billing.get_access(profile),
        'prices': {f"{plan}_{interval}": amount for (plan, interval), amount in billing.PRICES.items()},
        'can_manage_billing': role == 'owner',
        'active_tab': 'billing',
        # The yearly Starter option appears once its Polar product is configured.
        'starter_yearly': starter_yearly,
        'starter_yearly_per_month': f"{billing.PRICES[('starter', 'year')] / 12:.2f}",
    })


@login_required
@require_POST
def billing_checkout_view(request, plan, interval):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if role != 'owner':
        messages.error(request, "Only the account owner can change the plan.")
        return redirect('billing')
    if (plan, interval) not in billing.PRICES:
        messages.error(request, "Unknown plan selected.")
        return redirect('billing')
    try:
        url = polar_billing.create_checkout_url(
            profile, plan, interval,
            success_url=request.build_absolute_uri(reverse('billing')) + '?checkout=success',
            return_url=request.build_absolute_uri(reverse('billing')),
        )
    except polar_billing.PolarError as e:
        messages.error(request, str(e))
        return redirect('billing')
    return redirect(url)


@login_required
def billing_portal_view(request):
    """Polar's customer portal: change card, switch plan, download invoices, cancel."""
    profile, role = get_business_context(request.user)
    if profile is None or role != 'owner':
        messages.error(request, "Only the account owner can manage billing.")
        return redirect('billing')
    try:
        return redirect(polar_billing.customer_portal_url(profile, request.build_absolute_uri(reverse('billing'))))
    except polar_billing.PolarError as e:
        messages.error(request, str(e))
        return redirect('billing')


@csrf_exempt
@require_POST
def polar_webhook_view(request):
    """Polar tells us about subscription changes here. Signature-checked."""
    if not polar_billing.verify_webhook(request.body, request.headers):
        return HttpResponse(status=403)
    try:
        import json
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    polar_billing.apply_event(event)
    return HttpResponse(status=202)


# ==========================================
# 2. DEDICATED DASHBOARD & PAGE VIEWS
# ==========================================

@login_required
def dashboard(request):
    """Page 1: Main Dashboard & Live Customer Reviews Stream."""
    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user)

    # Same reset rule as check_ai_quota(), but read-only — just for display,
    # doesn't touch the actual counter or consume a generation.
    ai_used_today = profile.ai_generations_today if profile.ai_last_generation_date == timezone.localdate() else 0

    status_filter = request.GET.get('status', 'all')
    valid_statuses = {choice[0] for choice in Review.STATUS_CHOICES}

    status_counts = {
        'all': business_reviews.count(),
        'pending': business_reviews.filter(status='pending').count(),
        'approved': business_reviews.filter(status='approved').count(),
        'posted': business_reviews.filter(status='posted').count(),
        'flagged': business_reviews.filter(status='flagged').count(),
    }

    if status_filter in valid_statuses:
        reviews = business_reviews.filter(status=status_filter).order_by('-created_at')
    else:
        status_filter = 'all'
        reviews = business_reviews.order_by('-created_at')

    negative_count = business_reviews.filter(rating__lte=2).count()
    rating_filter = request.GET.get('rating')
    if rating_filter == 'negative':
        reviews = business_reviews.filter(rating__lte=2).order_by('-created_at')
        status_filter = None

    avg_rating_result = business_reviews.aggregate(Avg('rating'))['rating__avg']
    avg_rating = round(avg_rating_result, 1) if avg_rating_result else 0.0

    total_reviews_synced = business_reviews.count()

    # Both figures use the same assumption: replying to a review by hand
    # takes about 6 minutes (0.1 hr) on average — sourcing, reading context,
    # writing, checking tone. Named here so it's a single, honest source of
    # truth if that assumption ever needs revisiting.
    AVG_HOURS_SAVED_PER_REPLY = 0.1
    saved_hours = round(total_reviews_synced * AVG_HOURS_SAVED_PER_REPLY, 1)

    # Weekly figure: replies actually handled (approved/posted) in the
    # current calendar week, Monday to now — this is the number that can
    # be shown live in a sales call to back up a "hours saved per week"
    # claim, since it reflects real, recent throughput rather than a
    # lifetime cumulative total.
    today_local = timezone.localdate()
    start_of_week = today_local - timedelta(days=today_local.weekday())
    reviews_handled_this_week = business_reviews.filter(
        status__in=['approved', 'posted'],
        updated_at__date__gte=start_of_week,
    ).count()
    saved_hours_this_week = round(reviews_handled_this_week * AVG_HOURS_SAVED_PER_REPLY, 1)

    total_reviews_handled = business_reviews.filter(status__in=['approved', 'posted']).count()

    responded_reviews = business_reviews.filter(status__in=['approved', 'posted'])
    avg_response_delta = responded_reviews.annotate(
        response_time=ExpressionWrapper(F('updated_at') - F('created_at'), output_field=DurationField())
    ).aggregate(avg=Avg('response_time'))['avg']

    if avg_response_delta:
        total_seconds = avg_response_delta.total_seconds()
        if total_seconds < 3600:
            avg_response_display = f"{int(total_seconds // 60)}m"
        else:
            avg_response_display = f"{total_seconds / 3600:.1f}h"
    else:
        avg_response_display = "—"

    response_rate = 0
    if total_reviews_synced > 0:
        response_rate = round((total_reviews_handled / total_reviews_synced) * 100)

    source_counts = {
        'google': business_reviews.filter(source='google').count(),
        'tripadvisor': business_reviews.filter(source='tripadvisor').count(),
        'webhook': business_reviews.filter(source='webhook').count(),
    }

    analytics = {
        'total_reviews_synced': total_reviews_synced,
        'avg_rating': avg_rating,
        'saved_hours': saved_hours,
        'time_saved': saved_hours,
        'saved_hours_this_week': saved_hours_this_week,
        'reviews_handled_this_week': reviews_handled_this_week,
        'total_reviews_handled': total_reviews_handled,
        'response_rate': response_rate,
        'avg_response_display': avg_response_display,
    }

    competitors = Competitor.objects.filter(user=profile.user).order_by('-avg_rating')

    # Sentiment trend: last 30 days, grouped by day. Only counts reviews
    # that actually got a sentiment classification (i.e. passed the
    # authenticity/spam checks and reached Gemini).
    thirty_days_ago = timezone.now() - timedelta(days=30)
    sentiment_qs = (
        business_reviews
        .filter(created_at__gte=thirty_days_ago)
        .exclude(sentiment__isnull=True)
        .exclude(sentiment='')
        .annotate(day=TruncDate('created_at'))
        .values('day', 'sentiment')
        .annotate(count=Count('id'))
        .order_by('day')
    )

    trend_map = {}
    for row in sentiment_qs:
        day_str = row['day'].strftime('%b %d')
        trend_map.setdefault(day_str, {'positive': 0, 'neutral': 0, 'negative': 0})
        trend_map[day_str][row['sentiment']] = row['count']

    sentiment_labels = list(trend_map.keys())
    sentiment_positive = [v['positive'] for v in trend_map.values()]
    sentiment_neutral = [v['neutral'] for v in trend_map.values()]
    sentiment_negative = [v['negative'] for v in trend_map.values()]
    has_sentiment_data = bool(sentiment_labels)

    context = {
        'reviews': reviews,
        'profile': profile,
        'analytics': analytics,
        'status_filter': status_filter,
        'status_counts': status_counts,
        'rating_filter': rating_filter,
        'negative_count': negative_count,
        'source_counts': source_counts,
        'active_tab': 'dashboard',
        'ai_used_today': ai_used_today,
        'competitors': competitors,
        'can_manage': can_manage_settings(role),
        'sentiment_labels': sentiment_labels,
        'sentiment_positive': sentiment_positive,
        'sentiment_neutral': sentiment_neutral,
        'sentiment_negative': sentiment_negative,
        'has_sentiment_data': has_sentiment_data,
        'active_sync': sync_jobs.active_job(profile.user),
        'quick_queue': _quick_reply_queue(profile) if can_approve_reviews(role) else [],
    }
    return render(request, 'reviews/dashboard.html', context)


QUICK_QUEUE_MAX = 50


def _quick_reply_queue(profile):
    """
    Replies that are drafted but not on Google yet, for the Quick Reply flow:
    pre-approved (4-5★) first, then the ones waiting for a check, newest first.
    """
    rows = (
        Review.objects.filter(user=profile.user, is_simulated=False, status__in=('approved', 'pending'))
        .exclude(ai_draft_reply__isnull=True).exclude(ai_draft_reply='')
        .order_by('status', '-created_at')[:QUICK_QUEUE_MAX]
    )
    fallback_url = profile.google_maps_url or 'https://business.google.com/reviews'
    return [{
        'id': r.id,
        'name': r.reviewer_name,
        'rating': r.rating,
        'comment': r.comment,
        'draft': r.ai_draft_reply,
        'url': r.review_url or fallback_url,
        'auto': bool(profile.gbp_connected and (r.external_id or '').startswith('gbp:')),
        'post_url': reverse('quick_post', args=[r.id]),
    } for r in rows]


@login_required
@require_POST
def quick_post_view(request, review_id):
    """
    Quick Reply flow, one review at a time. Saves the (possibly edited) reply
    and marks it as answered:
      - Google Business connected + review from it -> posted through the API;
      - otherwise the owner just pasted it on Google -> marked posted.
    Returns JSON so the dashboard can move to the next review instantly.
    """
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not can_approve_reviews(role):
        return JsonResponse({'ok': False, 'error': ROLE_DENIED}, status=403)

    review = get_object_or_404(Review, id=review_id, user=profile.user, is_simulated=False)
    if review.status == 'posted':
        return JsonResponse({'ok': True, 'posted_via': 'already'})

    text = (request.POST.get('text') or '').strip()[:4000]
    if not text:
        return JsonResponse({'ok': False, 'error': "The reply is empty."}, status=400)

    original = (review.ai_draft_reply or '').strip()
    if original and original != text:
        EditLog.objects.create(user=request.user, review=review, ai_draft=original, final_text=text)
    review.ai_draft_reply = text

    via = 'manual'
    if profile.gbp_connected and (review.external_id or '').startswith('gbp:'):
        try:
            gbp_client.post_reply(profile, review.external_id[4:], text)
            via = 'google'
        except gbp_client.GBPError as e:
            review.status = 'approved'
            review.save(update_fields=['ai_draft_reply', 'status', 'updated_at'])
            return JsonResponse({'ok': False, 'error': f"Couldn't post to Google: {e}. Your reply is saved."}, status=502)

    review.status = 'posted'
    if review.first_response_at is None:
        review.first_response_at = timezone.now()
    review.save(update_fields=['ai_draft_reply', 'status', 'first_response_at', 'updated_at'])
    ActivityLog.objects.create(user=request.user, action='review_approved', detail=f"Quick reply: {review.reviewer_name}"[:255])
    return JsonResponse({'ok': True, 'posted_via': via})


@login_required
def settings_page_view(request):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    hour_choices = [f"{h:02d}:00" for h in range(24)]
    return render(request, 'reviews/settings.html', {'profile': profile, 'active_tab': 'ai_settings', 'hour_choices': hour_choices})


@login_required
def integrations_page_view(request):
    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user)
    source_counts = {
        'google': business_reviews.filter(source='google').count(),
        'tripadvisor': business_reviews.filter(source='tripadvisor').count(),
        'webhook': business_reviews.filter(source='webhook').count(),
    }
    sync_logs = SyncLog.objects.filter(user=profile.user)[:8]
    context = {
        'profile': profile,
        'active_tab': 'integrations',
        'source_counts': source_counts,
        'sync_logs': sync_logs,
        'active_sync': sync_jobs.active_job(profile.user),
    }
    return render(request, 'reviews/integrations.html', context)

@login_required
def qr_booster_page_view(request):
    profile, role = get_or_create_owned_profile(request.user)
    qr_codes = SmartQRCode.objects.filter(user=profile.user).order_by('-created_at')

    events = QRScanEvent.objects.filter(qr_code__user=profile.user)

    since = timezone.now() - timedelta(days=14)
    daily_counts = (
        events.filter(scanned_at__gte=since)
        .annotate(day=TruncDate('scanned_at'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('day')
    )
    daily_labels = [d['day'].strftime('%b %d') for d in daily_counts]
    daily_values = [d['count'] for d in daily_counts]

    device_counts = events.values('device_type').annotate(count=Count('id')).order_by('-count')
    device_labels = [dict(QRScanEvent.DEVICE_CHOICES).get(d['device_type'], d['device_type']) for d in device_counts]
    device_values = [d['count'] for d in device_counts]

    hourly = (
        events.annotate(hour=ExtractHour('scanned_at'))
        .values('hour')
        .annotate(count=Count('id'))
    )
    hourly_map = {h['hour']: h['count'] for h in hourly}
    hourly_values = [hourly_map.get(h, 0) for h in range(24)]
    gated_events = events.filter(qr_code__private_feedback_url__isnull=False)
    non_gated_scans = events.filter(qr_code__private_feedback_url__isnull=True).count()

    funnel_scanned = gated_events.count()
    funnel_rated = gated_events.filter(resulted_in_rating__isnull=False).count()
    # Where guests actually CHOSE to go (every rating can pick Google).
    funnel_to_google = gated_events.filter(went_to='google').count()
    funnel_to_private = gated_events.filter(went_to='private').count()

    funnel = {
        'scanned': funnel_scanned,
        'rated': funnel_rated,
        'to_google': funnel_to_google,
        'to_private': funnel_to_private,
        'non_gated_scans': non_gated_scans,
        'rated_pct': round((funnel_rated / funnel_scanned) * 100) if funnel_scanned else 0,
    }

    # per-campaign breakdown, attached directly to each qr for the template
    for qr in qr_codes:
        if qr.private_feedback_url:
            qr_events = events.filter(qr_code=qr)
            qr.funnel_scanned = qr_events.count()
            qr.funnel_rated = qr_events.filter(resulted_in_rating__isnull=False).count()
            qr.funnel_to_google = qr_events.filter(went_to='google').count()
            qr.funnel_rated_pct = round((qr.funnel_rated / qr.funnel_scanned) * 100) if qr.funnel_scanned else 0

    team_members = [profile.user]
    for invite in TeamInvite.objects.filter(owner=profile.user, linked_user__isnull=False):
        team_members.append(invite.linked_user)

    context = {
        'qr_codes': qr_codes,
        'profile': profile,
        'team_members': team_members,
        'active_tab': 'qr_booster',
        'has_scan_data': events.exists(),
        'daily_labels': daily_labels,
        'daily_values': daily_values,
        'device_labels': device_labels,
        'device_values': device_values,
        'hourly_values': hourly_values,
        'funnel': funnel,
    }
    return render(request, 'reviews/qr_booster.html', context)


def _send_invite_email(request, invite):
    join_url = request.build_absolute_uri(reverse('accept_invite', args=[invite.token]))
    profile, _ = get_or_create_owned_profile(request.user)
    send_mail(
        subject=f"You've been invited to {profile.business_name} on Mehrly",
        message=(
            f"Hi,\n\n"
            f"{request.user.username} invited you to help manage review replies "
            f"as a {invite.get_role_display()}.\n\n"
            f"Accept the invitation here (create an account or sign in when asked):\n{join_url}\n\n"
            f"This link is personal — don't forward it.\n\n"
            f"— Mehrly"
        ),
        from_email=None,
        recipient_list=[invite.email],
        fail_silently=False,
    )


def _is_unused_profile(profile):
    """An auto-created, never-used owner profile (safe to drop when joining a team)."""
    return not (
        Review.objects.filter(user=profile.user, is_simulated=False).exists()
        or profile.billing_subscription_id
        or profile.google_maps_url
        or profile.google_business_location_id
        or profile.tripadvisor_url
    )


def accept_invite_view(request, token):
    """
    The only way (besides a verified Google email) to join a team: open the
    secret link from the invite email. Having the link proves access to the
    invited inbox, which plain signup with that email did not.
    """
    invite = TeamInvite.objects.select_related('owner').filter(token=token).first()
    if invite is None:
        messages.error(request, "This invitation link is invalid or was revoked. Ask the owner to invite you again.")
        return redirect('dashboard' if request.user.is_authenticated else 'home')

    if not request.user.is_authenticated:
        return redirect(f"{reverse('account_signup')}?next={request.path}")

    user = request.user
    if invite.linked_user_id == user.id:
        messages.info(request, "You've already joined this team.")
        return redirect('dashboard')
    if invite.linked_user_id:
        messages.error(request, "This invitation has already been used.")
        return redirect('dashboard')
    if invite.owner_id == user.id:
        messages.error(request, "That's an invitation you sent — open it from the invitee's account.")
        return redirect('competitors')
    if TeamInvite.objects.filter(linked_user=user).exists():
        messages.error(request, "This account is already part of another team. Use a different account to accept.")
        return redirect('dashboard')

    owned = BusinessProfile.objects.filter(user=user).first()
    if owned is not None:
        if not _is_unused_profile(owned):
            messages.error(request, "This account already runs its own business on Mehrly. "
                                    "Sign in with another account to join this team.")
            return redirect('dashboard')
        owned.delete()  # empty placeholder created on first visit — nothing is lost

    invite.linked_user = user
    invite.accepted_at = timezone.now()
    invite.save(update_fields=['linked_user', 'accepted_at'])
    ActivityLog.objects.create(user=invite.owner, action='team_invite_accepted', detail=invite.email)
    messages.success(request, f"Welcome! You've joined the team as {invite.get_role_display()}.")
    return redirect('dashboard')



@login_required
def competitors_page_view(request):
    """Team & Access Controls — invite floor managers/staff to help manage replies."""
    profile, actor_role = get_business_context(request.user)
    if profile is None:
        profile, actor_role = get_or_create_owned_profile(request.user)

    if request.method == 'POST':
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to manage team invites.")
            return redirect('competitors')
        if not billing.can(profile, 'team'):
            messages.warning(request, billing.denial_message(profile))
            return redirect('billing')

        role = request.POST.get('role', 'reviewer')
        if role not in dict(TeamInvite.ROLE_CHOICES):
            messages.error(request, "Invalid role selected.")
            return redirect('competitors')

        emails = []

        single_email = request.POST.get('invite_email', '').strip()
        if single_email:
            emails.append(single_email)

        bulk_text = request.POST.get('bulk_emails', '').strip()
        if bulk_text:
            for chunk in bulk_text.replace(',', '\n').split('\n'):
                chunk = chunk.strip()
                if chunk:
                    emails.append(chunk)

        csv_file = request.FILES.get('csv_file')
        if csv_file:
            try:
                decoded = csv_file.read().decode('utf-8-sig')
                reader = csv.reader(io.StringIO(decoded))
                for row in reader:
                    if row and row[0].strip():
                        emails.append(row[0].strip())
            except Exception:
                messages.error(request, "Couldn't read that CSV file — check it's a plain .csv.")
                return redirect('competitors')

        seen = set()
        emails = [e for e in emails if not (e in seen or seen.add(e))]

        if not emails:
            messages.warning(request, "No email addresses found to invite.")
            return redirect('competitors')

        existing_emails = set(
            TeamInvite.objects.filter(owner=profile.user).values_list('email', flat=True)
        )

        invited, skipped, failed = 0, 0, 0
        for email in emails:
            try:
                validate_email(email)
            except ValidationError:
                failed += 1
                continue

            if email in existing_emails:
                skipped += 1
                continue

            invite = TeamInvite.objects.create(owner=profile.user, email=email, role=role)
            ActivityLog.objects.create(user=request.user, action='team_invite_sent', detail=email)
            existing_emails.add(email)

            try:
                _send_invite_email(request, invite)
                invited += 1
            except Exception:
                failed += 1

        parts = []
        if invited:
            parts.append(f"{invited} invitation{'s' if invited != 1 else ''} sent")
        if skipped:
            parts.append(f"{skipped} already invited")
        if failed:
            parts.append(f"{failed} failed")

        if invited:
            messages.success(request, ", ".join(parts).capitalize() + ".")
        else:
            messages.warning(request, ", ".join(parts).capitalize() + ".")

        return redirect('competitors')

    invites = TeamInvite.objects.filter(owner=profile.user).order_by('-created_at')

    recent_activity = ActivityLog.objects.filter(user=request.user)[:10]
    context = {
        'profile': profile,
        'role': actor_role,
        'invites': invites,
        'recent_activity': recent_activity,
        'active_tab': 'competitors',
    }
    return render(request, 'reviews/competitors.html', context)


@login_required
def delete_invite_view(request, invite_id):
    profile, actor_role = get_business_context(request.user)
    if profile is None:
        profile, actor_role = get_or_create_owned_profile(request.user)
    invite = get_object_or_404(TeamInvite, id=invite_id, owner=profile.user)
    if request.method == 'POST':
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to revoke invites.")
            return redirect('competitors')

        invite.delete()
        ActivityLog.objects.create(user=request.user, action='team_invite_revoked', detail=invite.email)
        messages.info(request, "Invitation revoked.")
    return redirect('competitors')


@login_required
def simulator_page_view(request):
    profile, role = get_or_create_owned_profile(request.user)
    sim_reviews_qs = Review.objects.filter(user=profile.user, is_simulated=True).order_by('-created_at')

    stats = {
        'total': sim_reviews_qs.count(),
        'processed': sim_reviews_qs.exclude(status='pending').count(),
        'flagged': sim_reviews_qs.filter(status='flagged').count(),
    }

    paginator = Paginator(sim_reviews_qs, 10)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    context = {
        'profile': profile,
        'active_tab': 'simulator',
        'sim_reviews': page_obj,
        'sim_stats': stats,
    }
    return render(request, 'reviews/simulator.html', context)



@login_required
def delete_simulated_review_view(request, review_id):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE, as_json=True)):
            return denied
        profile, role = get_or_create_owned_profile(request.user)
        review = get_object_or_404(Review, id=review_id, user=profile.user, is_simulated=True)
        review.delete()
        return JsonResponse({'status': 'deleted', 'id': review_id})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def regenerate_simulated_review_view(request, review_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if (denied := role_denied(request, MANAGE, as_json=True)):
        return denied

    profile, role = get_or_create_owned_profile(request.user)
    review = get_object_or_404(Review, id=review_id, user=profile.user, is_simulated=True)
    result = draft_reply(review, profile, force=request.POST.get('force') == '1', is_regeneration=True)
    return JsonResponse(_simulator_payload(review, result))


def _simulator_payload(review, result):
    """JSON shape the Review Simulator page expects."""
    review.refresh_from_db()
    payload = {
        'id': review.id,
        'status': review.status,
        'sentiment': review.sentiment if result.code != 'not_authentic' else None,
        'is_likely_spam': review.is_likely_spam if result.code != 'not_authentic' else None,
        'ai_draft_reply': review.ai_draft_reply if result.ok else None,
        'reviewer_name': review.reviewer_name,
        'rating': review.rating,
    }
    if not result.ok:
        payload['reject_reason'] = result.reason
    return payload



# ==========================================
# 3. ACTION & FORM HANDLERS
# ==========================================

def _rename_business(profile, new_name):
    """
    Renaming only changes the label. Reviews belong to the account, not to a
    name, so nothing is hidden and no API call (or cost) is involved.
    """
    new_name = (new_name or '').strip()[:255]
    if not new_name or new_name == profile.business_name:
        return False
    profile.business_name = new_name
    profile.save(update_fields=['business_name'])
    Review.objects.filter(user=profile.user).update(business_name=new_name)
    return True


def _start_over_with_new_business(profile, new_name):
    """
    Owner explicitly said "this is a different business": forget the old
    Google/TripAdvisor listing and delete its synced reviews (Review
    Simulator tests are kept). The next sync looks the new business up.
    """
    deleted, _ = Review.objects.filter(user=profile.user, is_simulated=False).delete()
    profile.google_review_url = None
    profile.google_maps_url = None
    profile.tripadvisor_url = None
    profile.last_auto_sync = None
    profile.business_name = (new_name or '').strip()[:255] or profile.business_name
    profile.business_switch_count = (profile.business_switch_count or 0) + 1
    profile.last_business_switch_at = timezone.now()
    profile.save(update_fields=['google_review_url', 'google_maps_url', 'tripadvisor_url',
                                'last_auto_sync', 'business_name', 'business_switch_count', 'last_business_switch_at'])
    Review.objects.filter(user=profile.user).update(business_name=profile.business_name)
    return deleted


@login_required
@require_POST
def sync_google_reviews_view(request):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not can_manage_settings(role):
        messages.error(request, "Only the owner or an admin can sync reviews.")
        return redirect('dashboard')
    if not billing.is_active(profile):
        messages.warning(request, billing.READ_ONLY_MESSAGE)
        return redirect('billing')

    blocked = _sync_blocked_reason(profile, 'google')
    if blocked:
        messages.info(request, blocked)
        return redirect('dashboard')

    business_name = (request.POST.get('business_name') or '').strip() or profile.business_name
    place_id = request.POST.get('place_id', '').strip()

    if request.POST.get('switch_business') == '1':
        switch_blocked = _switch_blocked_reason(profile)
        if switch_blocked:
            messages.warning(request, switch_blocked)
            return redirect('dashboard')
        deleted = _start_over_with_new_business(profile, business_name)
        ActivityLog.objects.create(user=request.user, action='settings_updated',
                                   detail=f"Switched business to {profile.business_name} ({deleted} old reviews removed)"[:255])
    elif _rename_business(profile, business_name):
        ActivityLog.objects.create(user=request.user, action='settings_updated',
                                   detail=f"Renamed business to {profile.business_name}"[:255])

    try:
        sync_jobs.start_google_sync(profile, place_id=place_id)
    except Exception as e:
        SyncLog.objects.create(user=profile.user, platform='google', status='failed', detail=str(e)[:255])
        messages.error(request, f"Google sync failed for {profile.business_name}. Please try again.")
        return redirect('dashboard')

    messages.info(request, f"Sync started for {profile.business_name} — you can keep working, the reviews appear here when ready.")
    return redirect('dashboard')


TRIAL_SWITCH_LIMIT = 1                  # business switches allowed during the free trial
PAID_SWITCH_EVERY = timedelta(days=30)  # paid plans: one switch per 30 days


def _sync_blocked_reason(profile, platform):
    """
    Money guards for manual syncs (each one is a paid DataForSEO task):
      - one sync at a time, at most one per platform per hour;
      - at most MANUAL_SYNCS_PER_DAY per account per 24h;
      - a site-wide daily budget, so a wave of free-trial sign-ups typing
        random business names can never run up the bill.
    """
    if sync_jobs.active_job(profile.user):
        return "A sync is already running — it will finish on its own."
    now = timezone.now()
    last = SyncJob.objects.filter(user=profile.user, platform=platform).exclude(state='failed').first()
    if last and (now - last.created_at) < timedelta(hours=1):
        return "You synced recently. New reviews are also checked automatically every day. You can sync again in about an hour."
    day_ago = now - timedelta(hours=24)
    if SyncJob.objects.filter(user=profile.user, created_at__gte=day_ago).count() >= settings.MANUAL_SYNCS_PER_DAY:
        return "You've reached today's manual sync limit. New reviews are still checked automatically every night."
    site_tasks = SyncJob.objects.filter(created_at__gte=day_ago).exclude(task_id='').exclude(task_id__isnull=True).count()
    if site_tasks >= settings.DATAFORSEO_DAILY_TASK_CAP:
        logger.warning("DataForSEO daily task cap reached (%s)", site_tasks)
        return "Syncing is very busy right now — please try again tomorrow. Your existing reviews are safe."
    return None


def _switch_blocked_reason(profile):
    """
    Switching to a different business wipes the old reviews and pays for a
    fresh import. Limited so one account can't hop between businesses
    (e.g. competitors) on our DataForSEO bill.
    """
    level = billing.get_access(profile).level
    if level in ('trial', 'founding'):
        if profile.business_switch_count >= TRIAL_SWITCH_LIMIT:
            return ("During the free trial you can switch to a different business once. "
                    f"Need another change? Email {settings.SUPPORT_EMAIL}.")
        return None
    if profile.last_business_switch_at and timezone.now() - profile.last_business_switch_at < PAID_SWITCH_EVERY:
        days = (profile.last_business_switch_at + PAID_SWITCH_EVERY - timezone.now()).days + 1
        return (f"You can switch to a different business again in {days} day{'s' if days != 1 else ''}. "
                f"Need it sooner? Email {settings.SUPPORT_EMAIL}.")
    return None


@login_required
@require_POST
def sync_status_view(request):
    """
    Polled by the dashboard every few seconds while a sync runs. Each call
    does one short step (check data / import / one AI draft / finish), so no
    request ever comes close to the server's 30-second limit.
    """
    profile, role = get_business_context(request.user)
    if profile is None:
        return JsonResponse({'state': 'idle'})
    job = sync_jobs.active_job(profile.user)
    if job is None:
        return JsonResponse({'state': 'idle'})

    job = sync_jobs.advance(job)
    labels = {
        'waiting': "Fetching reviews…",
        'drafting': f"Imported {job.imported_count} new review{'s' if job.imported_count != 1 else ''} — writing AI drafts ({len(job.draft_queue)} left)…",
        'done': f"Sync finished: {job.detail}.",
        'failed': f"Sync failed: {job.detail}",
    }
    return JsonResponse({'state': job.state, 'platform': job.platform, 'message': labels[job.state]})


@login_required
@require_POST
def sync_tripadvisor_reviews_view(request):
    business_name = request.POST.get('business_name', '').strip()
    if not business_name:
        messages.warning(request, "Enter a business name to sync TripAdvisor reviews.")
        return redirect('integrations')

    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not can_manage_settings(role):
        messages.error(request, "Only the owner or an admin can sync reviews.")
        return redirect('integrations')
    if not billing.can(profile, 'tripadvisor'):
        messages.warning(request, billing.denial_message(profile))
        return redirect('billing')

    if not profile.business_name or profile.business_name == "My Business":
        _rename_business(profile, business_name)

    blocked = _sync_blocked_reason(profile, 'tripadvisor')
    if blocked:
        messages.info(request, blocked)
        return redirect('integrations')

    try:
        job = sync_jobs.start_tripadvisor_sync(profile, business_name)
    except Exception as e:
        SyncLog.objects.create(user=profile.user, platform='tripadvisor', status='failed', detail=str(e)[:255])
        messages.error(request, f"TripAdvisor sync failed for {business_name}. Please try again.")
        return redirect('integrations')

    if job.state == 'failed':
        messages.error(request, f"TripAdvisor sync failed: {job.detail}")
    else:
        messages.info(request, f"TripAdvisor sync started for {business_name} — the reviews appear when ready.")
    return redirect('integrations')


@login_required
def export_reviews_csv_view(request):
    """
    Exports the logged-in user's reviews for their current business as a
    real CSV download — reviewer name, rating, comment, status, source,
    and date. No file is saved on the server; it's streamed straight to
    the browser as an attachment.
    """
    import csv
    from django.http import HttpResponse

    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user).order_by('-created_at')

    response = HttpResponse(content_type='text/csv')
    safe_name = slugify(profile.business_name) or 'mehrly'
    response['Content-Disposition'] = f'attachment; filename="{safe_name}_reviews.csv"'

    writer = safe_csv_writer(response)   # no spreadsheet formulas from review text
    writer.writerow(['Reviewer Name', 'Rating', 'Comment', 'Status', 'Source', 'Date'])

    for review in business_reviews:
        writer.writerow([
            review.reviewer_name,
            review.rating,
            review.comment,
            review.get_status_display(),
            review.get_source_display(),
            review.created_at.strftime('%Y-%m-%d %H:%M'),
        ])

    return response



@login_required
def export_insights_report_view(request):
    """
    A richer export than the raw reviews CSV: time-saved summary, weekly
    average-rating trend, plus AI-clustered recurring complaint themes
    from negative reviews (via analyze_complaints, which already existed
    but wasn't wired up).
    """
    import csv
    from django.http import HttpResponse

    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not billing.can(profile, 'insights'):
        messages.warning(request, billing.denial_message(profile))
        return redirect('billing')
    business_reviews = Review.objects.filter(user=profile.user)

    # --- Section 0: time saved (same figures shown on the dashboard) ---
    AVG_HOURS_SAVED_PER_REPLY = 0.1
    total_reviews_synced = business_reviews.count()
    saved_hours = round(total_reviews_synced * AVG_HOURS_SAVED_PER_REPLY, 1)

    today_local = timezone.localdate()
    start_of_week = today_local - timedelta(days=today_local.weekday())
    reviews_handled_this_week = business_reviews.filter(
        status__in=['approved', 'posted'],
        updated_at__date__gte=start_of_week,
    ).count()
    saved_hours_this_week = round(reviews_handled_this_week * AVG_HOURS_SAVED_PER_REPLY, 1)

    # --- Section 1: weekly avg rating trend ---
    weekly_trend = (
        business_reviews
        .annotate(week=TruncWeek('created_at'))
        .values('week')
        .annotate(avg_rating=Avg('rating'), review_count=Count('id'))
        .order_by('week')
    )

    # --- Section 2: AI complaint clustering on negative reviews (1-3 stars) ---
    negative_comments = list(
        business_reviews.filter(rating__lte=3, is_likely_spam=False).exclude(comment='').values_list('comment', flat=True)[:50]
    )
    from .services.ai_responder import QuotaExceededError
    complaint_analysis = {'summary': 'AI analysis skipped: daily AI limit reached. Try again tomorrow.',
                          'top_issues': [], 'actionable_tip': ''}
    if not negative_comments:
        complaint_analysis = analyze_complaints([])          # free: no AI call
    elif check_ai_quota(profile):
        try:
            complaint_analysis = analyze_complaints(negative_comments)
        except QuotaExceededError:
            pass

    response = HttpResponse(content_type='text/csv')
    safe_name = slugify(profile.business_name) or 'mehrly'
    response['Content-Disposition'] = f'attachment; filename="{safe_name}_insights_report.csv"'

    writer = safe_csv_writer(response)   # no spreadsheet formulas from review text

    writer.writerow(['Mehrly — Insights Report'])
    writer.writerow([f'Business: {profile.business_name}'])
    writer.writerow([f'Generated: {timezone.now().strftime("%Y-%m-%d %H:%M")}'])
    writer.writerow([])

    writer.writerow(['TIME SAVED'])
    writer.writerow(['This Week (since Monday)', f'{saved_hours_this_week} hrs', f'{reviews_handled_this_week} replies handled'])
    writer.writerow(['Lifetime', f'{saved_hours} hrs', f'{total_reviews_synced} reviews synced'])
    writer.writerow([])

    writer.writerow(['WEEKLY RATING TREND'])
    writer.writerow(['Week Starting', 'Avg Rating', 'Review Count'])
    for row in weekly_trend:
        writer.writerow([
            row['week'].strftime('%Y-%m-%d'),
            round(row['avg_rating'], 2),
            row['review_count'],
        ])
    writer.writerow([])

    writer.writerow(['TOP COMPLAINT THEMES (AI analysis of 1-3★ reviews)'])
    writer.writerow(['Summary', complaint_analysis.get('summary', '')])
    writer.writerow([])
    writer.writerow(['Category', 'Mentions', 'Severity', 'Sample Quote'])
    for issue in complaint_analysis.get('top_issues', []):
        writer.writerow([
            issue.get('category', ''),
            issue.get('mentions_count', ''),
            issue.get('severity', ''),
            issue.get('sample_quote', ''),
        ])
    writer.writerow([])
    writer.writerow(['Actionable Tip', complaint_analysis.get('actionable_tip', '')])

    return response


REDEEM_MAX_FAILS = 5            # wrong codes allowed per account...
REDEEM_WINDOW = 60 * 60         # ...per hour
REQUEST_HOURLY_CAP = 20         # founder requests accepted per hour, site-wide


@login_required
def redeem_access_code_view(request):
    if (denied := role_denied(request, ('owner',))):
        return denied
    profile, role = get_or_create_owned_profile(request.user)

    if request.method == 'POST':
        limit_key = f"redeem:{request.user.id}"
        if ratelimit.too_many(limit_key, REDEEM_MAX_FAILS, REDEEM_WINDOW):
            messages.error(request, "Too many wrong codes — please wait an hour and try again.")
            return redirect('redeem_access_code')

        code_input = request.POST.get('code', '').strip().upper()
        try:
            access_code = AccessCode.objects.get(code=code_input)
        except AccessCode.DoesNotExist:
            ratelimit.hit(limit_key, REDEEM_WINDOW)
            messages.error(request, "That code wasn't recognized — double check it and try again.")
            return redirect('redeem_access_code')

        if access_code.is_redeemed():
            messages.error(request, "That code has already been used.")
            return redirect('redeem_access_code')
        if access_code.status != 'approved':
            messages.error(request, "That code hasn't been approved yet — you'll get an email once it is.")
            return redirect('redeem_access_code')
        if billing.get_access(profile).level == 'paid':
            messages.info(request, "You already have an active paid plan — the code isn't needed.")
            return redirect('billing')

        access_code.redeemed_by = request.user
        access_code.redeemed_at = timezone.now()
        access_code.expires_at = timezone.now() + timedelta(days=30)
        access_code.save()

        profile.plan = 'founding_partner'
        profile.plan_expires_at = access_code.expires_at
        profile.save(update_fields=['plan', 'plan_expires_at'])

        messages.success(request, "You're in! Full Premium access unlocked for 30 days. Thank you for being a Founding Partner.")
        return redirect('dashboard')

    return render(request, 'reviews/redeem_code.html', {'profile': profile})



@login_required
def export_simulated_reviews_csv_view(request):
    """Same as export_reviews_csv_view but only simulated reviews from the Review Simulator."""
    import csv
    from django.http import HttpResponse

    profile, role = get_or_create_owned_profile(request.user)
    sim_reviews = Review.objects.filter(user=profile.user, is_simulated=True).order_by('-created_at')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="simulation_history.csv"'

    writer = safe_csv_writer(response)   # no spreadsheet formulas from review text
    writer.writerow(['Reviewer Name', 'Rating', 'Comment', 'Status', 'Sentiment', 'AI Draft', 'Date'])

    for review in sim_reviews:
        writer.writerow([
            review.reviewer_name,
            review.rating,
            review.comment,
            review.get_status_display(),
            review.sentiment or '',
            review.ai_draft_reply or '',
            review.created_at.strftime('%Y-%m-%d %H:%M'),
        ])

    return response


@login_required
def clear_simulation_history_view(request):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE)):
            return denied
        profile, role = get_or_create_owned_profile(request.user)
        deleted_count, _ = Review.objects.filter(user=profile.user, is_simulated=True).delete()
        messages.info(request, f"Cleared {deleted_count} simulated review{'s' if deleted_count != 1 else ''}.")
    return redirect('review_simulator')



@login_required
def update_settings_view(request):
    if request.method == 'POST':
        profile_ctx, actor_role = get_business_context(request.user)
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to change AI settings.")
            return redirect('ai_settings')

        profile = profile_ctx if profile_ctx else BusinessProfile.objects.get_or_create(user=request.user)[0]
        automation_mode = request.POST.get('automation_mode')
        brand_tone = request.POST.get('brand_tone')
        custom_prompt = request.POST.get('custom_prompt', '').strip()
        signature = request.POST.get('signature', '').strip()
        response_length = request.POST.get('response_length', 'medium')
        creativity_level = request.POST.get('creativity_level', 'medium')
        blacklisted_words = request.POST.get('blacklisted_words', '').strip()
        quiet_hours_enabled = request.POST.get('quiet_hours_enabled') == 'on'
        business_hours_start = request.POST.get('business_hours_start', '09:00')
        business_hours_end = request.POST.get('business_hours_end', '20:00')
        timezone_name = request.POST.get('timezone_name', 'Europe/Zurich')
        if automation_mode in ('positive_only', 'all') and not billing.can(profile, 'auto_post'):
            messages.warning(request, "Auto-posting needs an active plan — saved as manual approval for now.")
            automation_mode = 'manual'
        elif automation_mode == 'all' and not billing.can(profile, 'hands_free'):
            messages.warning(request, "Hands-Free is a Premium feature — saved as Smart Guardrail (4–5★ auto-post) on your Starter plan.")
            automation_mode = 'positive_only'
        if automation_mode in ['positive_only', 'all', 'manual']:
            profile.automation_mode = automation_mode

        if brand_tone in ['friendly', 'professional', 'casual']:
            profile.brand_tone = brand_tone

        if response_length in ['short', 'medium', 'long']:
            profile.response_length = response_length

        if creativity_level in ['low', 'medium', 'high']:
            profile.creativity_level = creativity_level

        profile.custom_prompt = custom_prompt
        profile.signature = signature
        profile.blacklisted_words = blacklisted_words
        profile.seo_keywords = request.POST.get('seo_keywords', '').strip()
        profile.geo_seo_enabled = request.POST.get('geo_seo_enabled') == 'on'
        profile.action_link_enabled = request.POST.get('action_link_enabled') == 'on'
        profile.action_link_url = request.POST.get('action_link_url', '').strip()
        profile.action_link_label = request.POST.get('action_link_label', '').strip()
        action_min_rating = request.POST.get('action_link_min_rating', '4')
        if action_min_rating in ('3', '4', '5'):
            profile.action_link_min_rating = int(action_min_rating)

        if 'logo' in request.FILES:
            profile.logo = request.FILES['logo']
        profile.quiet_hours_enabled = quiet_hours_enabled
        # Validate before saving: a bad time or timezone used to crash the
        # alert email and the public QR page.
        try:
            profile.business_hours_start = datetime.strptime(business_hours_start, '%H:%M').time()
            profile.business_hours_end = datetime.strptime(business_hours_end, '%H:%M').time()
        except (TypeError, ValueError):
            messages.error(request, "Business hours must look like 09:00.")
            return redirect('ai_settings')
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone_name)
            profile.timezone_name = timezone_name
        except Exception:
            messages.error(request, f"Unknown timezone \"{timezone_name}\". Use a name like Europe/Zurich.")
            return redirect('ai_settings')

        ActivityLog.objects.create(user=request.user, action='settings_updated', detail=f"Tone: {brand_tone}, Mode: {automation_mode}")

        profile.save()
        messages.success(request, "AI configuration saved.")

    return redirect(safe_next(request, 'ai_settings'))


# Pages a form may send the user back to after saving.
SAFE_NEXT_PAGES = {'ai_settings', 'qr_booster', 'dashboard', 'integrations', 'competitors', 'billing'}


def safe_next(request, default):
    """
    Where to go after a form. Only our own page names or a path on this site.
    Before: any value was followed, so a crafted link could bounce a
    logged-in owner to a look-alike phishing site right after saving.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    target = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if target in SAFE_NEXT_PAGES:
        return reverse(target)
    if target.startswith('/') and not target.startswith(('//', '/\\')) and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return reverse(default)


@login_required
def update_sync_frequency_view(request):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE)):
            return denied
        profile, role = get_or_create_owned_profile(request.user)
        frequency = request.POST.get('sync_frequency', 'manual')
        if frequency in dict(BusinessProfile.SYNC_FREQUENCY_CHOICES):
            profile.sync_frequency = frequency
            profile.save(update_fields=['sync_frequency'])
            messages.success(request, f"Auto-sync set to: {dict(BusinessProfile.SYNC_FREQUENCY_CHOICES)[frequency]}.")
    return redirect('integrations')


@login_required
def join_trustpilot_waitlist_view(request):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE)):
            return denied
        profile, role = get_or_create_owned_profile(request.user)
        if not profile.trustpilot_waitlist_joined_at:
            profile.trustpilot_waitlist_joined_at = timezone.now()
            profile.save(update_fields=['trustpilot_waitlist_joined_at'])
            messages.success(request, "You're on the Trustpilot early access list — we'll email you when it opens up.")
        else:
            messages.info(request, "You're already on the list.")
    return redirect('integrations')



@login_required
def regenerate_webhook_token_view(request):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE)):
            return denied
        profile, role = get_or_create_owned_profile(request.user)
        import uuid
        profile.webhook_token = uuid.uuid4()
        profile.save(update_fields=['webhook_token'])
        ActivityLog.objects.create(user=request.user, action='settings_updated', detail='Webhook key rotated')
        messages.success(request, "Webhook key rotated — the old URL no longer works. Update it anywhere you're using it.")
    return redirect('integrations')


@login_required
def request_integration_view(request):
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE)):
            return denied
        tool_name = request.POST.get('tool_name', '').strip()
        if not tool_name:
            messages.warning(request, "Enter a tool name to request.")
            return redirect('integrations')

        profile, role = get_or_create_owned_profile(request.user)

        try:
            send_mail(
                subject=f"Integration request: {tool_name}",
                message=(
                    f"Business: {profile.business_name}\n"
                    f"User: {request.user.username} ({request.user.email})\n"
                    f"Requested tool: {tool_name}\n"
                ),
                from_email=None,
                recipient_list=[settings.ADMIN_NOTIFY_EMAIL],   # hello@ didn't exist: requests were lost
                fail_silently=True,
            )
        except Exception:
            pass

        messages.success(request, f"Thanks — we've noted your request for {tool_name}. We'll be in touch if we build it.")

    return redirect('integrations')



@login_required
def preview_ai_response_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if (denied := role_denied(request, MANAGE, as_json=True)):
        return denied

    brand_tone = request.POST.get('brand_tone', 'friendly')
    custom_prompt = request.POST.get('custom_prompt', '').strip()
    signature = request.POST.get('signature', '').strip()
    response_length = request.POST.get('response_length', 'medium')
    creativity_level = request.POST.get('creativity_level', 'medium')
    blacklisted_words = request.POST.get('blacklisted_words', '').strip()

    if brand_tone not in ['friendly', 'professional', 'casual']:
        brand_tone = 'friendly'
    if response_length not in ['short', 'medium', 'long']:
        response_length = 'medium'
    if creativity_level not in ['low', 'medium', 'high']:
        creativity_level = 'medium'

    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not billing.is_active(profile):
        return JsonResponse({'error': billing.READ_ONLY_MESSAGE}, status=402)

    if not check_ai_quota(profile, amount=2):
        return JsonResponse({'error': f'Daily AI generation limit reached ({profile.ai_daily_limit}/day).'}, status=429)

    sample_reviews = {
        'en': "Food was good but we waited almost 20 minutes for a table even though it wasn't that busy. Staff were friendly once we sat down though.",
        'fr': "La nourriture était bonne mais nous avons attendu presque 20 minutes pour une table alors que ce n'était pas si occupé. Le personnel était sympathique une fois assis.",
    }

    from .services.ai_responder import QuotaExceededError
    drafts = {}
    for lang, sample_comment in sample_reviews.items():
        try:
            draft = generate_review_draft(
                reviewer_name="Alex",
                star_rating=3,
                comment=sample_comment,
                language=lang,
                business_name=profile.business_name,
                tone=brand_tone,
                custom_prompt=custom_prompt,
                signature=signature,
                response_length=response_length,
                creativity=creativity_level,
                blacklisted_words=blacklisted_words,
                contact_email=profile.user.email,
            )
        except QuotaExceededError:
            return JsonResponse({'error': "Gemini's daily free-tier quota is exhausted — try again later."}, status=429)

        drafts[lang] = draft

    if drafts['en'] is None and drafts['fr'] is None:
        return JsonResponse({'error': 'AI preview generation failed. Please try again in a moment.'}, status=502)

    return JsonResponse({
        'draft_en': drafts['en'],
        'draft_fr': drafts['fr'],
        'sample_en': sample_reviews['en'],
        'sample_fr': sample_reviews['fr'],
    })


DEMO_DAILY_CAP = 200   # public demo drafts per day, whole site


def public_demo_preview_view(request):
    """
    Public, unauthenticated preview for the landing page's live demo.
    """
    from django.core.cache import cache

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # Real visitor IP (can't be faked with a header) + a site-wide daily cap,
    # so the free public demo can never burn the whole Gemini quota.
    ip = get_client_ip(request)
    if ratelimit.too_many(f'demo:{ip}', 5, 300) or ratelimit.too_many('demo:all', DEMO_DAILY_CAP, 86400):
        return JsonResponse({'error': "You've hit the demo limit for now — try again in a few minutes, or sign up to use this on real reviews."}, status=429)
    ratelimit.hit(f'demo:{ip}', 300)
    ratelimit.hit('demo:all', 86400)

    reviewer_name = (request.POST.get('reviewer_name') or 'Alex').strip()[:60]
    comment = (request.POST.get('comment') or '').strip()[:600]
    language = request.POST.get('language', 'en')
    if language not in ('en', 'fr'):
        language = 'en'
    try:
        rating = int(request.POST.get('rating', 5))
    except (TypeError, ValueError):
        rating = 5
    rating = max(1, min(5, rating))

    if not comment:
        return JsonResponse({'error': 'Please enter a review to preview.'}, status=400)

    if not is_authentic_review(comment):
        return JsonResponse({'error': "That doesn't look like a genuine review — try a real customer comment."}, status=400)

    from .services.ai_responder import QuotaExceededError
    try:
        draft = generate_review_draft(
            reviewer_name=reviewer_name,
            star_rating=rating,
            comment=comment,
            language=language,
            business_name="Demo Bistro",
            tone='friendly',
            custom_prompt='',
            signature='',
            response_length='medium',
            creativity='medium',
            blacklisted_words='',
        )
    except QuotaExceededError:
        return JsonResponse({'error': "Our demo AI quota is exhausted right now — please try again shortly."}, status=429)

    if not draft:
        return JsonResponse({'error': 'Could not generate a reply — please try again.'}, status=502)

    auto_post = rating >= 4
    return JsonResponse({
        'reply': draft,
        'auto_post': auto_post,
    })


import secrets

def request_access_code_view(request):
    if request.method == 'POST':
        thanks = "Thanks! We've received your request — you'll get an email once it's approved."
        business_name = request.POST.get('business_name', '').strip()[:255]
        email = request.POST.get('email', '').strip().lower()

        if request.POST.get('website'):
            # Hidden field only bots fill in: pretend it worked, store nothing.
            messages.success(request, thanks)
            return redirect('request_access_code')

        ip_key = f"founder-request:{get_client_ip(request)}"
        if ratelimit.too_many(ip_key, 3, 3600):
            messages.success(request, thanks)   # same answer, nothing stored
            return redirect('request_access_code')
        ratelimit.hit(ip_key, 3600)

        if not business_name or not email:
            messages.error(request, "Please fill in both fields.")
            return redirect('request_access_code')

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "That doesn't look like a valid email.")
            return redirect('request_access_code')

        if AccessCode.objects.filter(requested_email__iexact=email, redeemed_by__isnull=True,
                                     status__in=('pending', 'approved')).exists():
            # Already asked: no second row and no second email to the inbox.
            messages.success(request, thanks)
            return redirect('request_access_code')

        if AccessCode.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count() >= REQUEST_HOURLY_CAP:
            # A flood (bots or abuse): don't store or email anything more this hour.
            messages.success(request, thanks)
            return redirect('request_access_code')

        AccessCode.objects.create(
            code=generate_founder_code(),
            business_name=business_name,
            requested_email=email,
            status='pending',
            notes=f"Requested by {email}",
        )

        try:
            send_mail(
                subject=f"New Founder access request: {business_name}",
                message=(
                    f"Business: {business_name}\n"
                    f"Email: {email}\n\n"
                    f"Review and approve in Django admin:\n"
                    f"{request.build_absolute_uri('/admin/reviews/accesscode/')}"
                ),
                from_email=None,
                recipient_list=[settings.ADMIN_NOTIFY_EMAIL],
                fail_silently=True,
            )
        except Exception:
            pass

        messages.success(request, thanks)
        return redirect('request_access_code')

    return render(request, 'reviews/request_access_code.html')


@login_required
def dashboard_insights_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    from django.core.cache import cache
    lock_key = f'dashboard_insights_lock:{request.user.id}'
    if not cache.add(lock_key, True, timeout=30):
        return JsonResponse({'error': 'Analysis already in progress — please wait.'}, status=429)

    try:
        return _dashboard_insights_impl(request)
    finally:
        cache.delete(lock_key)


def _dashboard_insights_impl(request):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not billing.can(profile, 'insights'):
        return JsonResponse({'error': billing.denial_message(profile)}, status=402)
    business_reviews = Review.objects.filter(user=profile.user)

    negative_comments = list(
        business_reviews.filter(rating__lte=3, is_likely_spam=False).exclude(comment='').values_list('comment', flat=True)[:50]
    )

    if not negative_comments:
        return JsonResponse({
            'summary': 'No negative reviews found.',
            'top_issues': [],
            'actionable_tip': 'Keep up the excellent service!',
            'negative_count': 0,
        })

    if not check_ai_quota(profile):
        return JsonResponse({'error': f'Daily AI generation limit reached ({profile.ai_daily_limit}/day).'}, status=429)

    from .services.ai_responder import analyze_complaints, QuotaExceededError
    try:
        analysis = analyze_complaints(negative_comments)
    except QuotaExceededError:
        # Gemini's own free-tier cap was hit, not the app's — refund the
        # app-level quota unit we just consumed since no analysis happened.
        profile.ai_generations_today = max(0, profile.ai_generations_today - 1)
        profile.save(update_fields=['ai_generations_today'])
        return JsonResponse({'error': "Gemini's daily free-tier quota is exhausted — try again later."}, status=429)

    return JsonResponse({
        'summary': analysis.get('summary', ''),
        'top_issues': analysis.get('top_issues', []),
        'actionable_tip': analysis.get('actionable_tip', ''),
        'negative_count': len(negative_comments),
    })


@login_required
@require_POST
def generate_draft_view(request, review_id):
    from django.core.cache import cache
    lock_key = f'generate_draft_lock:{review_id}'
    if not cache.add(lock_key, True, timeout=15):
        return JsonResponse({'ok': False, 'reason': 'A generation is already in progress for this review — please wait.'})

    try:
        return _generate_draft_impl(request, review_id)
    finally:
        cache.delete(lock_key)


def _generate_draft_impl(request, review_id):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)

    review = get_object_or_404(Review, id=review_id, user=profile.user)
    if not can_approve_reviews(role):
        return JsonResponse({'ok': False, 'reason': "Read-only access — you can't generate replies."})

    result = draft_reply(
        review, profile,
        force=request.POST.get('force') == '1',
        is_regeneration=bool(review.ai_draft_reply),
    )
    if not result.ok:
        return JsonResponse({'ok': False, 'reason': result.reason})
    return JsonResponse({'ok': True, 'posted': result.posted})


@login_required
@require_POST
def approve_review_view(request, review_id):
    """
    Saves the (possibly edited) reply and marks it approved. If the owner has
    connected Google Business Profile and this review came from it, the reply
    is also published to Google and the review is marked posted.

    The review is ALWAYS saved, even if posting to Google fails — the owner's
    edited text must never be lost.
    """
    profile, actor_role = get_business_context(request.user)
    if profile is None:
        profile, actor_role = get_or_create_owned_profile(request.user)

    review = get_object_or_404(Review, id=review_id, user=profile.user)

    if not can_approve_reviews(actor_role):
        messages.error(request, "You don't have permission to approve replies.")
        return redirect('dashboard')

    edited_text = (request.POST.get('ai_draft_reply') or '').strip()
    if not edited_text:
        messages.error(request, "The reply is empty — write something before approving.")
        return redirect('dashboard')

    original_draft = (review.ai_draft_reply or '').strip()
    if original_draft and original_draft != edited_text:
        EditLog.objects.create(
            user=request.user,
            review=review,
            ai_draft=original_draft,
            final_text=edited_text,
        )

    review.ai_draft_reply = edited_text

    posted_to_google = False
    gbp_failed = False
    if profile.gbp_connected and (review.external_id or '').startswith('gbp:'):
        try:
            posted_to_google = bool(gbp_client.post_reply(profile, review.external_id[4:], edited_text))
        except gbp_client.GBPError as e:
            gbp_failed = True
            messages.error(request, f"Couldn't post to Google automatically: {e}. Your reply is saved — paste it on Google, then click “I posted it”.")

    review.status = 'posted' if posted_to_google else 'approved'
    if review.first_response_at is None:
        review.first_response_at = timezone.now()
    review.save(update_fields=['ai_draft_reply', 'status', 'first_response_at', 'updated_at'])

    ActivityLog.objects.create(user=request.user, action='review_approved', detail=f"Reply to {review.reviewer_name}"[:255])

    if posted_to_google:
        messages.success(request, f"Reply to {review.reviewer_name} was posted to Google.")
    elif not gbp_failed:
        messages.success(
            request,
            f"Reply to {review.reviewer_name} is saved and copied. Paste it on the Google tab "
            f"that just opened, then click “I posted it” here."
        )

    return redirect('dashboard')


@login_required
def account_settings_view(request):
    """Account settings page for the logged-in user."""
    sessions = UserSession.objects.filter(user=request.user)
    current_key = request.session.session_key
    return render(request, 'reviews/account_settings.html', {
        'active_tab': 'account_settings',
        'sessions': sessions,
        'current_session_key': current_key,
    })


@login_required
def revoke_session_view(request, session_key):
    if request.method == 'POST':
        user_session = get_object_or_404(UserSession, session_key=session_key, user=request.user)
        Session.objects.filter(session_key=session_key).delete()
        user_session.delete()
        messages.info(request, "That device has been signed out.")
    return redirect('account_settings')



@login_required
def add_review_view(request):
    """Processes manual review simulation submission and returns the real
    pipeline result as JSON so the Review Simulator page can display it
    without navigating away."""
    if request.method == 'POST':
        if (denied := role_denied(request, MANAGE, as_json=True)):
            return denied
        from django.core.cache import cache
        lock_key = f'add_review_lock:{request.user.id}'
        if not cache.add(lock_key, True, timeout=5):
            return JsonResponse({'error': 'A submission is already in progress — please wait a moment.'}, status=429)

        try:
            return _add_review_impl(request)
        finally:
            cache.delete(lock_key)

    return JsonResponse({'error': 'POST required'}, status=405)


def _add_review_impl(request):
    reviewer_name = (request.POST.get('reviewer_name') or 'Anonymous').strip()[:255]
    try:
        rating = max(1, min(5, int(request.POST.get('rating', 5))))
    except (TypeError, ValueError):
        rating = 5
    comment = request.POST.get('comment', '')
    language = request.POST.get('language', 'fr')

    if language == 'auto':
        language = detect_review_language(comment)

    profile, role = get_or_create_owned_profile(request.user)

    review = Review.objects.create(
        user=profile.user,
        reviewer_name=reviewer_name,
        rating=rating,
        comment=comment,
        detected_language=language,
        business_name=profile.business_name,
        status='pending',
        is_simulated=True,
    )

    result = draft_reply(review, profile)
    payload = _simulator_payload(review, result)
    payload['automation_mode'] = profile.automation_mode
    return JsonResponse(payload)


@login_required
def run_ai_training_view(request):
    if request.method == 'POST':
        profile, actor_role = get_business_context(request.user)
        if profile is None:
            profile, actor_role = get_or_create_owned_profile(request.user)
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to run AI training.")
            return redirect('ai_settings')
        if not billing.is_active(profile):
            messages.warning(request, billing.READ_ONLY_MESSAGE)
            return redirect('billing')

        from .tasks import analyze_edit_patterns
        analyze_edit_patterns.delay(request.user.id)
        profile.last_training_run = timezone.now()
        profile.save(update_fields=['last_training_run'])
        messages.info(request, "AI Training started — check back in a minute for your updated style summary.")
    return redirect('ai_settings')


# ==========================================
# 4. COMPETITOR & TEAM ACTIONS
# ==========================================

@login_required
def add_competitor_view(request):
    if request.method == 'POST':
        profile, role = get_or_create_owned_profile(request.user)
        if not can_manage_settings(role):
            messages.error(request, "You don't have permission to manage competitors.")
            return redirect('dashboard')
        if not billing.can(profile, 'competitors'):
            messages.warning(request, billing.denial_message(profile))
            return redirect('billing')

        name = request.POST.get('name')
        location = request.POST.get('location', 'Geneva')
        avg_rating = float(request.POST.get('avg_rating', 4.0))
        total_reviews = int(request.POST.get('total_reviews', 0))
        google_maps_url = request.POST.get('google_maps_url', '')

        if name:
            Competitor.objects.create(
                user=profile.user,
                name=name,
                location=location,
                avg_rating=avg_rating,
                total_reviews=total_reviews,
                google_maps_url=google_maps_url
            )

    return redirect('dashboard')


@login_required
def delete_competitor_view(request, competitor_id):
    profile, role = get_or_create_owned_profile(request.user)
    competitor = get_object_or_404(Competitor, id=competitor_id, user=profile.user)
    if request.method == 'POST':
        if not can_manage_settings(role):
            messages.error(request, "You don't have permission to manage competitors.")
            return redirect('dashboard')
        competitor.delete()
    return redirect('dashboard')


# ==========================================
# 5. SMART QR CODE ACTIONS & ROUTER
# ==========================================

def _detect_device_type(user_agent):
    ua = (user_agent or '').lower()
    if 'tablet' in ua or 'ipad' in ua:
        return 'tablet'
    if 'mobi' in ua or 'android' in ua or 'iphone' in ua:
        return 'mobile'
    if ua:
        return 'desktop'
    return 'other'


QR_EVENT_COOKIE = 'mehrly_qr_scan'
QR_EVENT_SALT = 'qr-scan-event'


def _qr_event_from_cookie(request, qr):
    """The scan event of THIS visitor (signed cookie), so ratings land on their own scan."""
    from django.core import signing
    raw = request.COOKIES.get(QR_EVENT_COOKIE)
    if not raw:
        return None
    try:
        event_id = signing.loads(raw, salt=QR_EVENT_SALT, max_age=24 * 3600)
    except signing.BadSignature:
        return None
    return QRScanEvent.objects.filter(id=event_id, qr_code=qr).first()


def qr_redirect_view(request, slug):
    """
    Public entry point for scanning QR codes — the Smart Feedback Router.

    Without a private feedback URL: straight to the Google review page.
    With one: "How was your visit?" first, then EVERY guest chooses where to go:
      4-5★ -> "Share it on Google" first, private message as the second option
      1-3★ -> "Tell the manager privately" first, Google as a clearly visible
              second option.
    Unhappy guests are never kept away from Google (no review gating, which
    Google's policy forbids); they're just offered the faster fix first.
    """
    qr = get_object_or_404(SmartQRCode, slug=slug)

    if not qr.is_currently_active():
        return HttpResponse(
            "<div style='font-family:sans-serif;text-align:center;padding:4rem 1rem;color:#333;'>"
            "<h2>This code isn't active right now</h2>"
            "<p>Please check back later.</p></div>"
        )

    google_url = qr.google_review_url or qr.fallback_url

    if not qr.private_feedback_url:
        if not request.GET:
            SmartQRCode.objects.filter(pk=qr.pk).update(total_scans=F('total_scans') + 1)
            QRScanEvent.objects.create(qr_code=qr, device_type=_detect_device_type(request.META.get('HTTP_USER_AGENT')))
        return redirect(qr.fallback_url or qr.google_review_url)

    event = _qr_event_from_cookie(request, qr)

    # Step 3: the guest picked a destination.
    go = request.GET.get('go')
    if go in ('google', 'private'):
        if event and not event.went_to:
            event.went_to = go
            event.save(update_fields=['went_to'])
        return redirect(google_url if go == 'google' else qr.private_feedback_url)

    # Step 2: the guest tapped a star -> show both options.
    try:
        rating = int(request.GET.get('rating', ''))
    except ValueError:
        rating = None
    if rating is not None and 1 <= rating <= 5:
        if event and event.resulted_in_rating is None:
            event.resulted_in_rating = rating
            event.save(update_fields=['resulted_in_rating'])
        return render(request, 'reviews/qr_gate.html', {'qr': qr, 'rating': rating, 'happy': rating >= 4})

    # Junk parameters (e.g. ?rating=99): show the stars again, no new scan.
    if request.GET:
        return render(request, 'reviews/qr_gate.html', {'qr': qr})

    # Step 1: a fresh scan.
    from django.core import signing
    SmartQRCode.objects.filter(pk=qr.pk).update(total_scans=F('total_scans') + 1)
    event = QRScanEvent.objects.create(qr_code=qr, device_type=_detect_device_type(request.META.get('HTTP_USER_AGENT')))
    response = render(request, 'reviews/qr_gate.html', {'qr': qr})
    response.set_cookie(QR_EVENT_COOKIE, signing.dumps(event.id, salt=QR_EVENT_SALT), max_age=24 * 3600,
                        httponly=True, samesite='Lax', secure=request.is_secure())
    return response


def qr_image_view(request, slug):
    """
    Serves a QR code PNG for the given slug, generated on the fly.
    Embeds the business's logo in the center if one is set.
    Publicly accessible (no login) since this is what gets embedded
    in <img> tags and printed materials — it must load for anyone scanning it.
    """
    qr = get_object_or_404(SmartQRCode, slug=slug)
    size, color, theme = _qr_render_params(request)

    target_url = f"{request.scheme}://{request.get_host()}/qr/{qr.slug}"

    logo_path = None
    try:
        profile = BusinessProfile.objects.get(user=qr.user)
        if profile.logo and profile.logo.name:
            logo_path = profile.logo.path
    except BusinessProfile.DoesNotExist:
        pass

    # Public endpoint: drawing a QR costs CPU, so each variant is drawn once
    # and then served from the shared cache (a table in the database).
    from django.core.cache import cache
    logo_stamp = profile_logo_stamp(logo_path)
    cache_key = f"qrimg:{qr.slug}:{size}:{color.lower()}:{theme}:{logo_stamp}:{request.get_host()}"
    png = cache.get(cache_key)
    if png is None:
        buffer = generate_qr_with_logo(target_url, logo_path=logo_path, fill_color=color, size=size, theme=theme)
        png = buffer.getvalue() if hasattr(buffer, 'getvalue') else bytes(buffer.read())
        cache.set(cache_key, png, QR_CACHE_SECONDS)
    response = HttpResponse(png, content_type='image/png')
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def profile_logo_stamp(logo_path):
    """Changes when the logo file changes, so a new logo shows up right away."""
    import os
    try:
        return int(os.path.getmtime(logo_path)) if logo_path else 0
    except OSError:
        return 0


QR_SIZES = (128, 200, 240, 300, 400, 500, 900, 1200)   # the sizes the app itself asks for
QR_CACHE_SECONDS = 24 * 60 * 60


def _qr_render_params(request):
    """
    Shared, validated rendering options for QR images.
    ?theme=dark|light -> transparent on-screen variant matching the app UI
    (default "print" = dark-on-white, used for downloads and PDFs).
    Size is clamped so a public URL can't be used to render giant images.
    """
    try:
        size = int(request.GET.get('size', 500))
    except (TypeError, ValueError):
        size = 500
    # Snap to a few fixed sizes: still clamped, and the public image can be
    # cached instead of re-drawn for every random ?size= value.
    size = min(QR_SIZES, key=lambda s: abs(s - size))

    color = request.GET.get('color', '#000000').strip()
    if not color.startswith('#'):
        color = '#' + color
    if not re.fullmatch(r'#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}', color):
        color = '#000000'   # junk colours used to crash the image renderer (500)

    theme = request.GET.get('theme', 'print')
    if theme not in ('print', 'dark', 'light'):
        theme = 'print'
    return size, color, theme


@login_required
def qr_preview_image_view(request):
    """
    Live preview for the 'Create QR' form, rendered by our own generator so it
    looks identical to the final code (replaces the third-party qrserver.com
    preview, which also leaked slugs to an outside service).
    """
    size, color, theme = _qr_render_params(request)
    size = min(size, 400)
    slug = slugify(request.GET.get('slug', ''))[:50] or 'preview'
    target_url = f"{request.scheme}://{request.get_host()}/qr/{slug}"

    profile, role = get_business_context(request.user)
    logo_path = profile.logo.path if (profile and profile.logo and profile.logo.name) else None

    buffer = generate_qr_with_logo(target_url, logo_path=logo_path, fill_color=color, size=size, theme=theme)
    response = HttpResponse(buffer, content_type='image/png')
    response['Cache-Control'] = 'private, max-age=300'
    return response



@login_required
def qr_print_template_view(request, slug):
    """
    Generates a print-ready PDF (table tent, sticker sheet, or door sign)
    for the given QR code. ?template=tent|stickers|sign, defaults to tent.
    """
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    qr = get_object_or_404(SmartQRCode, slug=slug, user=profile.user)
    template = request.GET.get('template', 'tent')
    target_url = f"{request.scheme}://{request.get_host()}/qr/{qr.slug}"

    logo_path = None
    if profile.logo and profile.logo.name:
        logo_path = profile.logo.path

    if template == 'stickers':
        buffer = generate_sticker_sheet_pdf(qr, target_url, logo_path=logo_path)
        filename = f"qr-stickers-{qr.slug}.pdf"
    elif template == 'sign':
        buffer = generate_door_sign_pdf(qr, target_url, logo_path=logo_path)
        filename = f"qr-door-sign-{qr.slug}.pdf"
    else:
        buffer = generate_table_tent_pdf(qr, target_url, logo_path=logo_path)
        filename = f"qr-table-tent-{qr.slug}.pdf"

    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response



@login_required
def create_qr_view(request):
    if request.method == 'POST':
        profile, role = get_or_create_owned_profile(request.user)
        if not can_manage_settings(role):
            messages.error(request, "You don't have permission to create QR codes.")
            return redirect('qr_booster')
        if not billing.can(profile, 'qr'):
            messages.warning(request, billing.denial_message(profile))
            return redirect('billing')

        title = request.POST.get('name', '').strip() or 'Main QR Code'
        google_review_url = request.POST.get('target_url', '').strip()
        private_feedback_url = request.POST.get('private_feedback_url', '').strip()
        slug_input = request.POST.get('slug', '').strip()

        if not google_review_url:
            messages.error(request, "A Target Google Review URL is required to create a QR code.")
            return redirect('qr_booster')

        if slug_input:
            slug = slugify(slug_input)
            if SmartQRCode.objects.filter(slug=slug).exists():
                messages.error(request, f'The slug "{slug}" is already taken — try a different one.')
                return redirect('qr_booster')
        else:
            slug = uuid.uuid4().hex[:8]
            while SmartQRCode.objects.filter(slug=slug).exists():
                slug = uuid.uuid4().hex[:8]

        assigned_to_id = request.POST.get('assigned_to', '').strip()
        assigned_to = None
        if assigned_to_id:
            try:
                assigned_to = User.objects.get(id=assigned_to_id)
            except (User.DoesNotExist, ValueError):
                pass

        active_hours_enabled = request.POST.get('active_hours_enabled') == 'on'
        active_hours_start = request.POST.get('active_hours_start') or None
        active_hours_end = request.POST.get('active_hours_end') or None
        expires_at = request.POST.get('expires_at') or None

        SmartQRCode.objects.create(
            user=profile.user,
            title=title,
            google_review_url=google_review_url,
            fallback_url=google_review_url,
            private_feedback_url=private_feedback_url or None,
            slug=slug,
            assigned_to=assigned_to,
            active_hours_enabled=active_hours_enabled,
            active_hours_start=active_hours_start,
            active_hours_end=active_hours_end,
            expires_at=expires_at,
        )
        messages.success(request, f'QR code "{title}" created.')

    return redirect('qr_booster')


@login_required
def delete_qr_view(request, qr_id):
    profile, role = get_or_create_owned_profile(request.user)
    qr = get_object_or_404(SmartQRCode, id=qr_id, user=profile.user)
    if request.method == 'POST':
        if not can_manage_settings(role):
            messages.error(request, "You don't have permission to delete QR codes.")
            return redirect('qr_booster')
        qr.delete()
        messages.info(request, "QR code deleted.")
    return redirect('qr_booster')



@login_required
@require_POST
def mark_posted_view(request, review_id):
    profile, role = get_business_context(request.user)
    if profile is None:
        profile, role = get_or_create_owned_profile(request.user)
    if not can_approve_reviews(role):
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    review = get_object_or_404(Review, id=review_id, user=profile.user, status='approved')
    review.status = 'posted'
    if review.first_response_at is None:
        review.first_response_at = timezone.now()
    review.save(update_fields=['status', 'first_response_at', 'updated_at'])
    ActivityLog.objects.create(user=request.user, action='review_approved', detail=f"Marked posted: {review.reviewer_name}"[:255])
    messages.success(request, f"Marked as posted for {review.reviewer_name}.")
    return redirect('dashboard')