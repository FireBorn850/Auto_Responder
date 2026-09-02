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
from .models import Review, BusinessProfile, SmartQRCode, Competitor, TeamInvite, EditLog, ActivityLog, QRScanEvent, SyncLog, AccessCode
from .services.ai_responder import generate_review_draft, analyze_complaints, is_authentic_review, analyze_review_sentiment, detect_review_language, detect_seo_keyword_used, append_action_link
from .services.google_api import post_reply_to_google
from .services.google_importer import fetch_live_google_reviews
from .services.tripadvisor_importer import fetch_live_tripadvisor_reviews
from .permissions import get_business_context, get_or_create_owned_profile, can_manage_settings, can_approve_reviews, check_ai_quota
from django.contrib.sessions.models import Session
from .models import UserSession
from django.core.mail import send_mail
import csv
import io
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from .services.exceptions import RateLimitError
from django.core.paginator import Paginator
from django.db.models import DurationField, ExpressionWrapper
from django.db.models.functions import TruncWeek
from .services.ai_responder import analyze_complaints
import secrets


# ==========================================
# 1. PUBLIC VIEWS
# ==========================================

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


# ==========================================
# 2. DEDICATED DASHBOARD & PAGE VIEWS
# ==========================================

@login_required
def dashboard(request):
    """Page 1: Main Dashboard & Live Customer Reviews Stream."""
    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user, business_name=profile.business_name)

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
    }
    return render(request, 'reviews/dashboard.html', context)


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
    business_reviews = Review.objects.filter(user=profile.user, business_name=profile.business_name)
    source_counts = {
        'google': business_reviews.filter(source='google').count(),
        'tripadvisor': business_reviews.filter(source='tripadvisor').count(),
        'webhook': business_reviews.filter(source='webhook').count(),
    }
    sync_logs = SyncLog.objects.filter(user=request.user)[:8]
    context = {
        'profile': profile,
        'active_tab': 'integrations',
        'source_counts': source_counts,
        'sync_logs': sync_logs,
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
    funnel_to_google = gated_events.filter(resulted_in_rating__gte=4).count()
    funnel_to_private = gated_events.filter(resulted_in_rating__gte=1, resulted_in_rating__lt=4).count()

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
            qr.funnel_to_google = qr_events.filter(resulted_in_rating__gte=4).count()
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


def _send_invite_email(request, invite_email, role):
    login_url = request.build_absolute_uri('/accounts/login/')
    profile, _ = get_or_create_owned_profile(request.user)
    send_mail(
        subject=f"You've been invited to {profile.business_name} on SwissReply.AI",
        message=(
            f"Hi,\n\n"
            f"{request.user.username} invited you to help manage review replies "
            f"as a {dict(TeamInvite.ROLE_CHOICES).get(role, role)}.\n\n"
            f"Sign in here: {login_url}\n\n"
            f"— SwissReply.AI"
        ),
        from_email=None,
        recipient_list=[invite_email],
        fail_silently=False,
    )



@login_required
def competitors_page_view(request):
    """Team & Access Controls — invite floor managers/staff to help manage replies."""
    if request.method == 'POST':
        _, actor_role = get_business_context(request.user)
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to manage team invites.")
            return redirect('competitors')

        role = request.POST.get('role', 'reviewer')
        if role not in dict(TeamInvite.ROLE_CHOICES):
            messages.error(request, "Invalid role selected.")
            return redirect('competitors')

        emails = []

        # Single-email field (existing form)
        single_email = request.POST.get('invite_email', '').strip()
        if single_email:
            emails.append(single_email)

        # Bulk paste field — comma, newline, or space separated
        bulk_text = request.POST.get('bulk_emails', '').strip()
        if bulk_text:
            for chunk in bulk_text.replace(',', '\n').split('\n'):
                chunk = chunk.strip()
                if chunk:
                    emails.append(chunk)

        # CSV upload — takes the first column of every row
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

        # Dedup, keep order
        seen = set()
        emails = [e for e in emails if not (e in seen or seen.add(e))]

        if not emails:
            messages.warning(request, "No email addresses found to invite.")
            return redirect('competitors')

        existing_emails = set(
            TeamInvite.objects.filter(owner=request.user).values_list('email', flat=True)
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

            TeamInvite.objects.create(owner=request.user, email=email, role=role)
            ActivityLog.objects.create(user=request.user, action='team_invite_sent', detail=email)
            existing_emails.add(email)

            try:
                _send_invite_email(request, email, role)
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

    profile, role = get_or_create_owned_profile(request.user)
    invites = TeamInvite.objects.filter(owner=request.user).order_by('-created_at')

    recent_activity = ActivityLog.objects.filter(user=request.user)[:10]
    context = {
        'profile': profile,
        'role': role,
        'invites': invites,
        'recent_activity': recent_activity,
        'active_tab': 'competitors',
    }
    return render(request, 'reviews/competitors.html', context)


@login_required
def delete_invite_view(request, invite_id):
    invite = get_object_or_404(TeamInvite, id=invite_id, owner=request.user)
    if request.method == 'POST':
        _, actor_role = get_business_context(request.user)
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
        profile, role = get_or_create_owned_profile(request.user)
        review = get_object_or_404(Review, id=review_id, user=profile.user, is_simulated=True)
        review.delete()
        return JsonResponse({'status': 'deleted', 'id': review_id})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def regenerate_simulated_review_view(request, review_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    profile, role = get_or_create_owned_profile(request.user)
    review = get_object_or_404(Review, id=review_id, user=profile.user, is_simulated=True)
    force = request.POST.get('force') == '1'

    if not force and not is_authentic_review(review.comment):
        review.status = 'flagged'
        review.ai_draft_reply = ''
        review.save()
        return JsonResponse({
            'id': review.id, 'status': review.status, 'sentiment': None, 'is_likely_spam': None,
            'ai_draft_reply': None, 'reject_reason': 'Failed authenticity check.',
        })

    if not check_ai_quota(profile):
        review.status = 'flagged'
        review.save()
        return JsonResponse({
            'id': review.id, 'status': review.status, 'sentiment': None, 'is_likely_spam': None,
            'ai_draft_reply': None, 'reject_reason': f'Daily AI generation limit reached ({profile.ai_daily_limit}/day).',
        })

    analysis = analyze_review_sentiment(review.comment, review.rating)
    review.sentiment = analysis['sentiment']
    review.is_likely_spam = analysis['is_likely_spam']

    if not force and review.is_likely_spam:
        review.status = 'flagged'
        review.save()
        return JsonResponse({
            'id': review.id, 'status': review.status, 'sentiment': review.sentiment, 'is_likely_spam': True,
            'ai_draft_reply': None, 'reject_reason': 'Flagged as likely spam.',
        })

    active_seo_keywords = profile.seo_keywords if (profile.geo_seo_enabled and profile.seo_keywords) else ''
    offer_qualifies = (
        profile.action_link_enabled and profile.action_link_url and profile.action_link_label
        and review.rating >= profile.action_link_min_rating
    )
    action_offer_label = profile.action_link_label if offer_qualifies else ''

    from .services.ai_responder import QuotaExceededError
    try:
        draft_text = generate_review_draft(
            reviewer_name=review.reviewer_name,
            star_rating=review.rating,
            comment=review.comment,
            language=review.detected_language,
            business_name=review.business_name,
            tone=profile.brand_tone,
            custom_prompt=profile.custom_prompt or '',
            signature=profile.signature or '',
            response_length=profile.response_length,
            creativity=profile.creativity_level,
            blacklisted_words=profile.blacklisted_words or '',
            learned_patterns=profile.learned_patterns or '',
            seo_keywords=active_seo_keywords,
            action_offer_label=action_offer_label,
        )
    except QuotaExceededError:
        review.status = 'generation_failed'
        review.save()
        return JsonResponse({
            'id': review.id, 'status': review.status, 'sentiment': review.sentiment, 'is_likely_spam': review.is_likely_spam,
            'ai_draft_reply': None, 'reject_reason': "Gemini's daily free-tier quota is exhausted — try again later.",
        })

    if not draft_text:
        review.status = 'generation_failed'
        review.save()
        return JsonResponse({
            'id': review.id, 'status': review.status, 'sentiment': review.sentiment, 'is_likely_spam': review.is_likely_spam,
            'ai_draft_reply': None, 'reject_reason': 'AI draft generation failed.',
        })

    if offer_qualifies:
        draft_text = append_action_link(draft_text, profile.action_link_url, profile.action_link_label)
        review.action_link_shown = True

    review.ai_draft_reply = draft_text
    review.seo_keyword_used = detect_seo_keyword_used(draft_text, active_seo_keywords)
    mode = profile.automation_mode
    if mode == 'all':
        review.status = 'approved'
    elif mode == 'positive_only' and review.rating >= 4:
        review.status = 'approved'
    else:
        review.status = 'pending'
    review.save()

    return JsonResponse({
        'id': review.id, 'status': review.status, 'sentiment': review.sentiment, 'is_likely_spam': review.is_likely_spam,
        'ai_draft_reply': review.ai_draft_reply, 'reviewer_name': review.reviewer_name, 'rating': review.rating,
    })




# ==========================================
# 3. ACTION & FORM HANDLERS
# ==========================================

@login_required
def sync_google_reviews_view(request):
    if request.method == 'POST':
        business_name = request.POST.get('business_name', 'Geneva Bistro').strip()
        place_id = request.POST.get('place_id', '').strip()

        profile, role = get_or_create_owned_profile(request.user)
        profile.business_name = business_name
        profile.save()

        try:
            imported_count, auto_posted_count = fetch_live_google_reviews(
                place_id=place_id,
                user=profile.user,
                business_name=business_name
            )
        except Exception as e:
            SyncLog.objects.create(user=request.user, platform='google', status='failed', detail=str(e)[:255])
            messages.error(request, f"Google sync failed for {business_name}. Please try again.")
            return redirect('dashboard')

        parts = []
        if imported_count > 0:
            parts.append(f"Imported {imported_count} new review{'s' if imported_count != 1 else ''}")
        if auto_posted_count > 0:
            parts.append(f"detected {auto_posted_count} reply{'ies' if auto_posted_count != 1 else 'y'} now live on Google")

        SyncLog.objects.create(
            user=request.user, platform='google', status='success',
            detail=', '.join(parts) if parts else 'No new reviews found'
        )

        if parts:
            messages.success(request, f"{' and '.join(parts).capitalize()} for {business_name}.")
        else:
            messages.info(request, f"Switched to {business_name} — no new reviews found (you may already have them, or none exist yet).")

    return redirect('dashboard')


@login_required
def sync_tripadvisor_reviews_view(request):
    if request.method == 'POST':
        business_name = request.POST.get('business_name', '').strip()

        if not business_name:
            messages.warning(request, "Enter a business name to sync TripAdvisor reviews.")
            return redirect('integrations')

        profile, role = get_or_create_owned_profile(request.user)
        if not profile.business_name or profile.business_name == "My Business":
            profile.business_name = business_name
            profile.save()

        try:
            # ✅ CHANGED: Now receives 2 values
            imported_count, listing_url = fetch_live_tripadvisor_reviews(
                user=profile.user,
                business_name=business_name,
            )
            
            # ✅ ADDED: Save the TripAdvisor URL if found
            if listing_url:
                profile.tripadvisor_url = listing_url
                profile.save(update_fields=['tripadvisor_url'])
                
        except RateLimitError as e:
            SyncLog.objects.create(user=request.user, platform='tripadvisor', status='rate_limited', detail=str(e)[:255])
            messages.error(request, "TripAdvisor sync hit SerpAPI's rate limit — try again shortly.")
            return redirect('integrations')
        except Exception as e:
            SyncLog.objects.create(user=request.user, platform='tripadvisor', status='failed', detail=str(e)[:255])
            messages.error(request, f"TripAdvisor sync failed for {business_name}. Please try again.")
            return redirect('integrations')

        SyncLog.objects.create(
            user=request.user, platform='tripadvisor', status='success',
            detail=f"{imported_count} new review{'s' if imported_count != 1 else ''}" if imported_count > 0 else 'No new reviews found'
        )

        if imported_count > 0:
            messages.success(request, f"Imported {imported_count} new TripAdvisor review{'s' if imported_count != 1 else ''} for {business_name}.")
        else:
            messages.info(request, f"You're all caught up — no new TripAdvisor reviews found for {business_name}.")

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
    business_reviews = Review.objects.filter(
        user=profile.user, business_name=profile.business_name
    ).order_by('-created_at')

    response = HttpResponse(content_type='text/csv')
    safe_name = profile.business_name.replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="{safe_name}_reviews.csv"'

    writer = csv.writer(response)
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

    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user, business_name=profile.business_name)

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
        business_reviews.filter(rating__lte=3).exclude(comment='').values_list('comment', flat=True)[:50]
    )
    complaint_analysis = analyze_complaints(negative_comments)

    response = HttpResponse(content_type='text/csv')
    safe_name = profile.business_name.replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="{safe_name}_insights_report.csv"'

    writer = csv.writer(response)

    writer.writerow(['SwissReply.AI — Insights Report'])
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


@login_required
def redeem_access_code_view(request):
    profile, role = get_or_create_owned_profile(request.user)

    if request.method == 'POST':
        code_input = request.POST.get('code', '').strip().upper()
        try:
            access_code = AccessCode.objects.get(code=code_input)
        except AccessCode.DoesNotExist:
            messages.error(request, "That code wasn't recognized — double check it and try again.")
            return redirect('redeem_access_code')

        if access_code.is_redeemed():
            messages.error(request, "That code has already been used.")
            return redirect('redeem_access_code')

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

    writer = csv.writer(response)
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
        profile.business_hours_start = business_hours_start
        profile.business_hours_end = business_hours_end
        profile.timezone_name = timezone_name

        ActivityLog.objects.create(user=request.user, action='settings_updated', detail=f"Tone: {brand_tone}, Mode: {automation_mode}")

        profile.save()
        messages.success(request, "AI configuration saved.")

    next_url = request.POST.get('next') or 'ai_settings'
    return redirect(next_url)


@login_required
def update_sync_frequency_view(request):
    if request.method == 'POST':
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
                recipient_list=['hello@swissreply.ai'],
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

    profile, role = get_or_create_owned_profile(request.user)

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


def public_demo_preview_view(request):
    """
    Public, unauthenticated preview for the landing page's live demo.
    """
    from django.core.cache import cache

    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'unknown')
    rate_key = f'public_demo_rate:{ip}'
    attempts = cache.get(rate_key, 0)
    if attempts >= 5:
        return JsonResponse({'error': "You've hit the demo limit for now — try again in a few minutes, or sign up to use this on real reviews."}, status=429)
    cache.set(rate_key, attempts + 1, timeout=300)

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
        business_name = request.POST.get('business_name', '').strip()
        email = request.POST.get('email', '').strip()

        if not business_name or not email:
            messages.error(request, "Please fill in both fields.")
            return redirect('request_access_code')

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "That doesn't look like a valid email.")
            return redirect('request_access_code')

        code = 'FOUNDER-' + secrets.token_hex(3).upper()  # e.g. FOUNDER-A1B2C3
        AccessCode.objects.create(code=code, business_name=business_name, notes=f"Requested by {email}")

        try:
            send_mail(
                subject="Your SwissReply.AI Founding Partner code",
                message=(
                    f"Hi,\n\n"
                    f"Thanks for your interest in SwissReply.AI! Here is your Founding Partner access code:\n\n"
                    f"{code}\n\n"
                    f"1. Sign up here: {request.build_absolute_uri('/accounts/signup/')}\n"
                    f"2. Once logged in, go to: {request.build_absolute_uri('/redeem/')}\n"
                    f"3. Enter the code above to unlock full Premium access, free for 30 days.\n\n"
                    f"— SwissReply.AI"
                ),
                from_email=None,
                recipient_list=[email],
                fail_silently=False,
            )
            messages.success(request, "Check your inbox — we've sent your access code!")
        except Exception:
            messages.error(request, "Something went wrong sending the email. Please try again or contact us directly.")

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
    profile, role = get_or_create_owned_profile(request.user)
    business_reviews = Review.objects.filter(user=profile.user, business_name=profile.business_name)

    negative_comments = list(
        business_reviews.filter(rating__lte=3).exclude(comment='').values_list('comment', flat=True)[:50]
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
    force = request.POST.get('force') == '1'

    if not force and not is_authentic_review(review.comment):
        review.status = 'flagged'
        review.ai_draft_reply = ''
        review.save()
        return JsonResponse({'ok': False, 'reason': "Skipped — this doesn't look like a genuine review (failed authenticity check)."})

    if not check_ai_quota(profile):
        return JsonResponse({'ok': False, 'reason': f"Daily AI generation limit reached ({profile.ai_daily_limit}/day) — try again tomorrow."})

    analysis = analyze_review_sentiment(review.comment, review.rating)
    review.sentiment = analysis['sentiment']
    review.is_likely_spam = analysis['is_likely_spam']

    if not force and review.is_likely_spam:
        review.status = 'flagged'
        review.save()
        return JsonResponse({'ok': False, 'reason': "Skipped — flagged as likely spam."})

    active_seo_keywords = profile.seo_keywords if (profile.geo_seo_enabled and profile.seo_keywords) else ''
    offer_qualifies = (
        profile.action_link_enabled and profile.action_link_url and profile.action_link_label
        and review.rating >= profile.action_link_min_rating
    )
    action_offer_label = profile.action_link_label if offer_qualifies else ''

    from .services.ai_responder import QuotaExceededError
    try:
        draft_text = generate_review_draft(
            reviewer_name=review.reviewer_name,
            star_rating=review.rating,
            comment=review.comment,
            language=review.detected_language,
            business_name=review.business_name,
            tone=profile.brand_tone,
            custom_prompt=profile.custom_prompt or '',
            signature=profile.signature or '',
            response_length=profile.response_length,
            creativity=profile.creativity_level,
            blacklisted_words=profile.blacklisted_words or '',
            learned_patterns=profile.learned_patterns or '',
            seo_keywords=active_seo_keywords,
            action_offer_label=action_offer_label,
        )
    except QuotaExceededError:
        return JsonResponse({'ok': False, 'reason': "Gemini's daily free-tier quota is exhausted for now — try again later, or enable billing on your Google AI project to raise the limit."})

    if not draft_text or draft_text.strip() == "":
        review.status = 'generation_failed'
        review.ai_draft_reply = ''
        review.save()
        return JsonResponse({'ok': False, 'reason': "AI draft generation failed — please try again."})

    if offer_qualifies:
        draft_text = append_action_link(draft_text, profile.action_link_url, profile.action_link_label)
        review.action_link_shown = True

    review.ai_draft_reply = draft_text
    review.seo_keyword_used = detect_seo_keyword_used(draft_text, active_seo_keywords)
    mode = profile.automation_mode
    if mode == 'all':
        review.status = 'approved'
    elif mode == 'positive_only' and review.rating >= 4:
        review.status = 'approved'
    else:
        review.status = 'pending'

    review.save()
    return JsonResponse({'ok': True})


@login_required
def approve_review_view(request, review_id):
    if request.method == 'POST':
        profile, actor_role = get_business_context(request.user)
        if profile is None:
            profile, actor_role = get_or_create_owned_profile(request.user)
        if not can_approve_reviews(actor_role):
            messages.error(request, "You don't have permission to approve replies.")
            return redirect('dashboard')

        review = get_object_or_404(Review, id=review_id, user=profile.user)
        edited_text = request.POST.get('ai_draft_reply')
        original_draft = review.ai_draft_reply

        review.ai_draft_reply = edited_text

        if original_draft and edited_text and original_draft.strip() != edited_text.strip():
            EditLog.objects.create(
                user=request.user,
                review=review,
                ai_draft=original_draft,
                final_text=edited_text,
            )

        has_google_maps_url = bool(profile.google_maps_url)

        if has_google_maps_url:
            success, google_url = post_reply_to_google(review.id, edited_text, request.user)

            if success:
                review.status = 'posted'
                messages.success(request, f"Reply to {review.reviewer_name} was posted to Google.")
            else:
                review.status = 'approved'
                if google_url:
                    request.session['pending_post_url'] = google_url
                    request.session['pending_reply_text'] = edited_text
                    messages.warning(
                        request,
                        f"Reply is ready! Click the 'Open Google Business' button below to post it manually."
                    )
                else:
                    messages.warning(
                        request,
                        f"Reply to {review.reviewer_name} was approved but needs to be posted manually."
                    )
        else:
            review.status = 'approved'
            messages.info(
                request,
                f"Reply to {review.reviewer_name} was saved. Connect your Google Business Profile on the Dashboard to auto-post."
            )

        review.save()

        ActivityLog.objects.create(user=request.user, action='review_approved', detail=f"Reply to {review.reviewer_name}")

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
    reviewer_name = request.POST.get('reviewer_name', 'Anonymous')
    rating = int(request.POST.get('rating', 5))
    comment = request.POST.get('comment', '')
    language = request.POST.get('language', 'fr')
    business_name = request.POST.get('business_name', 'Geneva Bistro')

    if language == 'auto':
        language = detect_review_language(comment)

    profile, role = get_or_create_owned_profile(request.user)
    review = Review.objects.create(
        user=profile.user,
        reviewer_name=reviewer_name,
        rating=rating,
        comment=comment,
        detected_language=language,
        business_name=business_name,
        status='pending',
        is_simulated=True,
    )

    if not is_authentic_review(comment):
        review.status = 'flagged'
        review.save()
        return JsonResponse({
            'id': review.id,
            'status': review.status,
            'sentiment': None,
            'is_likely_spam': None,
            'ai_draft_reply': None,
            'reject_reason': 'Failed authenticity check (gibberish/keyboard-mash detected).',
        })

    if not check_ai_quota(profile):
        review.status = 'flagged'
        review.save()
        return JsonResponse({
            'id': review.id,
            'status': review.status,
            'sentiment': None,
            'is_likely_spam': None,
            'ai_draft_reply': None,
            'reject_reason': f'Daily AI generation limit reached ({profile.ai_daily_limit}/day).',
        })

    analysis = analyze_review_sentiment(comment, rating)
    review.sentiment = analysis['sentiment']
    review.is_likely_spam = analysis['is_likely_spam']

    if review.is_likely_spam:
        review.status = 'flagged'
        review.save()
        return JsonResponse({
            'id': review.id,
            'status': review.status,
            'sentiment': review.sentiment,
            'is_likely_spam': True,
            'ai_draft_reply': None,
            'reject_reason': 'Flagged as likely spam by AI analysis.',
        })

    active_seo_keywords = profile.seo_keywords if (profile.geo_seo_enabled and profile.seo_keywords) else ''
    offer_qualifies = (
        profile.action_link_enabled and profile.action_link_url and profile.action_link_label
        and rating >= profile.action_link_min_rating
    )
    action_offer_label = profile.action_link_label if offer_qualifies else ''

    from .services.ai_responder import QuotaExceededError
    try:
        draft_text = generate_review_draft(
            reviewer_name=reviewer_name,
            star_rating=rating,
            comment=comment,
            language=language,
            business_name=business_name,
            tone=profile.brand_tone,
            custom_prompt=profile.custom_prompt or '',
            signature=profile.signature or '',
            response_length=profile.response_length,
            creativity=profile.creativity_level,
            blacklisted_words=profile.blacklisted_words or '',
            learned_patterns=profile.learned_patterns or '',
            seo_keywords=active_seo_keywords,
            action_offer_label=action_offer_label,
        )
    except QuotaExceededError:
        review.status = 'generation_failed'
        review.save()
        return JsonResponse({
            'id': review.id,
            'status': review.status,
            'sentiment': review.sentiment,
            'is_likely_spam': review.is_likely_spam,
            'ai_draft_reply': None,
            'reject_reason': "Gemini's daily free-tier quota is exhausted — try again later.",
        })

    if not draft_text:
        review.status = 'generation_failed'
        review.save()
        return JsonResponse({
            'id': review.id,
            'status': review.status,
            'sentiment': review.sentiment,
            'is_likely_spam': review.is_likely_spam,
            'ai_draft_reply': None,
            'reject_reason': 'AI draft generation failed.',
        })

    if offer_qualifies:
        draft_text = append_action_link(draft_text, profile.action_link_url, profile.action_link_label)
        review.action_link_shown = True

    review.ai_draft_reply = draft_text
    review.seo_keyword_used = detect_seo_keyword_used(draft_text, active_seo_keywords)
    mode = profile.automation_mode
    if mode == 'all':
        review.status = 'approved'
    elif mode == 'positive_only' and rating >= 4:
        review.status = 'approved'
    else:
        review.status = 'pending'
    review.save()

    return JsonResponse({
        'status': review.status,
        'sentiment': review.sentiment,
        'is_likely_spam': review.is_likely_spam,
        'ai_draft_reply': review.ai_draft_reply,
        'automation_mode': mode,
        'reviewer_name': review.reviewer_name,
        'rating': review.rating,
    })

    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def run_ai_training_view(request):
    if request.method == 'POST':
        profile, actor_role = get_business_context(request.user)
        if profile is None:
            profile, actor_role = get_or_create_owned_profile(request.user)
        if not can_manage_settings(actor_role):
            messages.error(request, "You don't have permission to run AI training.")
            return redirect('ai_settings')

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


def qr_redirect_view(request, slug):
    """
    Public entry point for scanning QR codes.

    If the business has set a private feedback URL, this shows a small
    "How was your visit?" star picker first (the Smart Rating Gate) — 4-5★
    routes to the public Google review, 1-3★ routes privately instead.
    If no private feedback URL is set, it skips straight to the review link
    like before.
    """
    qr = get_object_or_404(SmartQRCode, slug=slug)

    if not qr.is_currently_active():
        return HttpResponse(
            "<div style='font-family:sans-serif;text-align:center;padding:4rem 1rem;color:#333;'>"
            "<h2>This code isn't active right now</h2>"
            "<p>Please check back later.</p></div>"
        )

    rating_param = request.GET.get('rating')

    if rating_param:
        try:
            rating = int(rating_param)
            recent_event = qr.scan_events.filter(resulted_in_rating__isnull=True).first()
            if recent_event:
                recent_event.resulted_in_rating = rating
                recent_event.save(update_fields=['resulted_in_rating'])

            if rating >= 4 and qr.google_review_url:
                return redirect(qr.google_review_url)
            elif rating < 4 and qr.private_feedback_url:
                return redirect(qr.private_feedback_url)
        except ValueError:
            pass
        return redirect(qr.fallback_url or qr.google_review_url)

    SmartQRCode.objects.filter(pk=qr.pk).update(total_scans=F('total_scans') + 1)
    QRScanEvent.objects.create(
        qr_code=qr,
        device_type=_detect_device_type(request.META.get('HTTP_USER_AGENT')),
    )

    if qr.private_feedback_url:
        return render(request, 'reviews/qr_gate.html', {'qr': qr})

    return redirect(qr.fallback_url or qr.google_review_url)



def qr_image_view(request, slug):
    """
    Serves a QR code PNG for the given slug, generated on the fly.
    Embeds the business's logo in the center if one is set.
    Publicly accessible (no login) since this is what gets embedded
    in <img> tags and printed materials — it must load for anyone scanning it.
    """
    qr = get_object_or_404(SmartQRCode, slug=slug)
    size = int(request.GET.get('size', 500))
    color = request.GET.get('color', '#000000')
    if not color.startswith('#'):
        color = '#' + color

    target_url = f"{request.scheme}://{request.get_host()}/qr/{qr.slug}"

    logo_path = None
    try:
        profile = BusinessProfile.objects.get(user=qr.user)
        if profile.logo and profile.logo.name:
            logo_path = profile.logo.path
    except BusinessProfile.DoesNotExist:
        pass

    buffer = generate_qr_with_logo(target_url, logo_path=logo_path, fill_color=color, size=size)
    return HttpResponse(buffer, content_type='image/png')



@login_required
def qr_print_template_view(request, slug):
    """
    Generates a print-ready PDF (table tent, sticker sheet, or door sign)
    for the given QR code. ?template=tent|stickers|sign, defaults to tent.
    """
    qr = get_object_or_404(SmartQRCode, slug=slug, user=request.user)
    template = request.GET.get('template', 'tent')
    target_url = f"{request.scheme}://{request.get_host()}/qr/{qr.slug}"

    logo_path = None
    try:
        profile = BusinessProfile.objects.get(user=request.user)
        if profile.logo and profile.logo.name:
            logo_path = profile.logo.path
    except BusinessProfile.DoesNotExist:
        pass

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