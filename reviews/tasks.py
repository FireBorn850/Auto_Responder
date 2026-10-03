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
from django.db.models import Q

SYNC_INTERVALS = {
    'hourly': timedelta(hours=1),
    'daily': timedelta(days=1),
}

@shared_task
def poll_google_reviews():
    """
    Runs hourly via Celery Beat; each business syncs at its own frequency. For each business with auto-sync
    enabled, checks whether enough time has passed for their chosen
    frequency, and if so, pulls new reviews the same way the manual
    'Sync Reviews' button does.
    """
    from reviews.services.google_importer import fetch_live_google_reviews

    profiles = BusinessProfile.objects.exclude(sync_frequency='manual').filter(
        (Q(google_maps_url__isnull=False) & ~Q(google_maps_url='')) |
        (Q(google_business_location_id__isnull=False) & ~Q(google_business_location_id=''))
    )

    from reviews.services import billing

    for profile in profiles:
        if not billing.is_active(profile):
            continue  # read-only accounts don't trigger paid DataForSEO syncs
        interval = SYNC_INTERVALS.get(profile.sync_frequency)
        if not interval:
            continue

        now = dj_timezone.now()
        # 30 min grace: a scheduler firing slightly early must not skip a whole day
        if profile.last_auto_sync and (now - profile.last_auto_sync) < interval - timedelta(minutes=30):
            continue

        try:
            fetch_live_google_reviews(
                place_id='',
                user=profile.user,
                business_name=profile.business_name,
                priority=1,
                wait_seconds=300,
            )
            profile.last_auto_sync = now
            profile.save(update_fields=['last_auto_sync'])
            print(f"[AUTO-SYNC] Synced {profile.business_name} ({profile.sync_frequency})")
        except Exception as e:
            print(f"[AUTO-SYNC ERROR] {profile.business_name}: {e}")


DEFAULT_TZ = 'Europe/Zurich'
OVERDUE_SEND_ANYWAY = timedelta(hours=24)  # safety net: never hold an alert longer than a day


def _business_tz(profile):
    try:
        return ZoneInfo((profile.timezone_name if profile else None) or DEFAULT_TZ)
    except Exception:  # unknown/typo'd timezone name must never crash an alert
        return ZoneInfo(DEFAULT_TZ)


def next_opening_if_quiet(profile, now=None):
    """
    None if an alert may be sent right now. Otherwise the (aware) datetime of
    the next opening time, when it should be sent instead.
    """
    if not profile or not profile.quiet_hours_enabled:
        return None
    now_local = (now or dj_timezone.now()).astimezone(_business_tz(profile))
    start, end = profile.business_hours_start, profile.business_hours_end
    current = now_local.time()
    within_hours = start <= current <= end if start <= end else not (end < current < start)
    if within_hours:
        return None
    opening = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if opening <= now_local:
        opening += timedelta(days=1)
    return opening


def _deliver_alert(review):
    """Sends the email once. Returns True if it was sent (or no longer needed)."""
    if review.alert_sent_at:
        return True
    if review.status == 'posted':
        # Already answered while the alert was waiting — nothing to warn about.
        Review.objects.filter(id=review.id).update(alert_sent_at=dj_timezone.now(), alert_due_at=None)
        return True

    draft_line = (
        "An AI-drafted reply is already waiting for your approval in your dashboard."
        if review.ai_draft_reply else
        "Open your dashboard to generate a reply."
    )
    subject = f"⚠️ New {review.rating}★ review needs your attention — {review.business_name}"
    message = (
        f"Hi {review.user.first_name or review.user.username},\n\n"
        f"A new {review.rating}-star review just came in from {review.reviewer_name} "
        f"for {review.business_name}:\n\n"
        f"\"{review.comment}\"\n\n"
        f"{draft_line}\n\n"
        f"— Mehrly"
    )
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [review.user.email], fail_silently=False)
    except Exception as e:
        print(f"[ALERT ERROR] review {review.id}: {e}")
        # Leave it due now, so the next send_due_alerts run retries it.
        Review.objects.filter(id=review.id).update(alert_due_at=dj_timezone.now())
        return False
    Review.objects.filter(id=review.id).update(alert_sent_at=dj_timezone.now(), alert_due_at=None)
    return True


@shared_task
def send_negative_review_alert(review_id):
    """
    Emails the owner about a new 1-2★ review. With quiet hours on and the
    business closed, the alert is NOT sent now: its due time is saved on the
    review and the send_due_alerts command delivers it at opening time.
    (The old version re-queued itself with a Celery ETA, which in eager mode
    ran instantly, forever, and crashed the sync.)
    """
    review = Review.objects.select_related('user').filter(id=review_id).first()
    if review is None or not review.user or not review.user.email or review.alert_sent_at:
        return

    profile = BusinessProfile.objects.filter(user=review.user).first()
    from reviews.services import billing
    if not billing.can(profile, 'alerts'):
        return  # instant negative-review alerts are a Premium feature
    opening = next_opening_if_quiet(profile)
    if opening is not None:
        Review.objects.filter(id=review_id).update(alert_due_at=opening)
        return

    _deliver_alert(review)


def send_due_alerts(now=None):
    """
    Delivers alerts that were held back by quiet hours and are now due.
    Run hourly by GitHub Actions (python manage.py send_due_alerts) — free,
    no Celery worker or Redis needed. Returns how many emails went out.
    """
    now = now or dj_timezone.now()
    sent = 0
    due = Review.objects.select_related('user').filter(
        alert_due_at__lte=now, alert_sent_at__isnull=True
    )
    for review in due:
        if not review.user or not review.user.email:
            continue
        profile = BusinessProfile.objects.filter(user=review.user).first()
        opening = next_opening_if_quiet(profile, now)
        overdue = now - review.alert_due_at > OVERDUE_SEND_ANYWAY
        if opening is not None and not overdue:
            # Hours were changed since, or the run came early: wait for the real opening.
            Review.objects.filter(id=review.id).update(alert_due_at=opening)
            continue
        if _deliver_alert(review) and review.status != 'posted':
            sent += 1
    return sent


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

    from reviews.services import billing

    for profile in target_profiles:
        if not billing.is_active(profile):
            continue  # no Gemini spend on read-only accounts
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

@shared_task
def auto_draft_review(review_id):
    """
    Drafts (and, when safe, auto-posts) a reply for one newly imported review.
    Queued by the importers after every sync. Skips reviews that already have
    a draft or were already handled, so running it twice is harmless.
    """
    from reviews.services.review_pipeline import draft_reply

    review = Review.objects.filter(id=review_id).select_related('user').first()
    if review is None or review.status != 'pending' or review.ai_draft_reply:
        return
    profile = BusinessProfile.objects.filter(user=review.user).first()
    if profile is None:
        return
    try:
        result = draft_reply(review, profile)
        print(f"[AUTO-DRAFT] review {review_id}: {result.code}{' + posted' if result.posted else ''}")
    except Exception as e:
        print(f"[AUTO-DRAFT ERROR] review {review_id}: {e}")
