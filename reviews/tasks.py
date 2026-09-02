from celery import shared_task
from allauth.socialaccount.models import SocialToken, SocialAccount
from django.core.mail import send_mail
from django.conf import settings
from reviews.models import Review
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.utils import timezone as dj_timezone
from reviews.models import BusinessProfile
from reviews.permissions import get_business_context
from django.contrib.auth.models import User

SYNC_INTERVALS = {
    'realtime': timedelta(minutes=5),
    'hourly': timedelta(hours=1),
    'daily': timedelta(days=1),
}

@shared_task
def poll_google_reviews():
    """
    Runs every 5 min via Celery Beat. For each business with auto-sync
    enabled, checks whether enough time has passed for their chosen
    frequency, and if so, pulls new reviews the same way the manual
    'Sync Reviews' button does.
    """
    from reviews.services.google_importer import fetch_live_google_reviews

    profiles = BusinessProfile.objects.exclude(sync_frequency='manual').exclude(google_maps_url__isnull=True).exclude(google_maps_url='')

    for profile in profiles:
        interval = SYNC_INTERVALS.get(profile.sync_frequency)
        if not interval:
            continue

        now = dj_timezone.now()
        if profile.last_auto_sync and (now - profile.last_auto_sync) < interval:
            continue

        try:
            fetch_live_google_reviews(
                place_id='',
                user=profile.user,
                business_name=profile.business_name,
            )
            profile.last_auto_sync = now
            profile.save(update_fields=['last_auto_sync'])
            print(f"[AUTO-SYNC] Synced {profile.business_name} ({profile.sync_frequency})")
        except Exception as e:
            print(f"[AUTO-SYNC ERROR] {profile.business_name}: {e}")


@shared_task
def send_negative_review_alert(review_id):
    try:
        review = Review.objects.select_related('user').get(id=review_id)
    except Review.DoesNotExist:
        return

    if not review.user or not review.user.email:
        return

    profile = BusinessProfile.objects.filter(user=review.user).first()

    if profile and profile.quiet_hours_enabled:
        tz = ZoneInfo(profile.timezone_name or 'Europe/Zurich')
        now_local = dj_timezone.now().astimezone(tz)
        start, end = profile.business_hours_start, profile.business_hours_end
        current = now_local.time()

        within_hours = start <= current <= end if start <= end else not (end < current < start)

        if not within_hours:
            next_run = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            if next_run <= now_local:
                next_run += timedelta(days=1)
            send_negative_review_alert.apply_async(args=[review_id], eta=next_run)
            return

    subject = f"⚠️ New {review.rating}★ review needs your attention — {review.business_name}"
    message = (
        f"Hi {review.user.first_name or review.user.username},\n\n"
        f"A new {review.rating}-star review just came in from {review.reviewer_name} "
        f"for {review.business_name}:\n\n"
        f"\"{review.comment}\"\n\n"
        f"An AI-drafted reply is already waiting for your approval in your dashboard.\n\n"
        f"— SwissReply.AI"
    )

    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [review.user.email],
        fail_silently=True,
    )


@shared_task
def analyze_edit_patterns(user_id=None):
    """
    AI Training job. Builds one shared "team style" summary per business —
    pooling edits from the owner AND any invited teammates who've edited
    drafts — rather than a per-editor summary, since learned_patterns is a
    single field on BusinessProfile shared by the whole team. If user_id
    is given (the manual 'Run Now' button), only that user's business is
    processed; otherwise every business with recent edit activity runs.
    """
    from django.utils import timezone
    from datetime import timedelta
    from reviews.models import EditLog, BusinessProfile, TeamInvite
    from reviews.services.ai_responder import summarize_edit_patterns

    cutoff = timezone.now() - timedelta(days=30)
    logs_qs = EditLog.objects.filter(created_at__gte=cutoff)

    if user_id:
        try:
            editor = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return
        profile, role = get_business_context(editor)
        if profile is None:
            return
        target_profiles = [profile]
    else:
        # Every business whose owner OR whose invited teammates have edited
        # a draft in the last 30 days.
        editor_ids = set(logs_qs.values_list('user_id', flat=True).distinct())
        profile_ids = set()
        for uid in editor_ids:
            try:
                editor = User.objects.get(id=uid)
            except User.DoesNotExist:
                continue
            profile, role = get_business_context(editor)
            if profile is not None:
                profile_ids.add(profile.id)
        target_profiles = BusinessProfile.objects.filter(id__in=profile_ids)

    for profile in target_profiles:
        # Pool edits from the owner AND any linked teammates for this business.
        team_user_ids = [profile.user_id]
        team_user_ids += list(
            TeamInvite.objects.filter(owner=profile.user, linked_user__isnull=False)
            .values_list('linked_user_id', flat=True)
        )

        logs = logs_qs.filter(user_id__in=team_user_ids).order_by('-created_at')[:20]
        pairs = [{'draft': log.ai_draft, 'final': log.final_text} for log in logs]
        if not pairs:
            continue

        summary = summarize_edit_patterns(pairs)
        if not summary:
            continue

        profile.learned_patterns = summary
        profile.save(update_fields=['learned_patterns'])