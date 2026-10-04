"""
Weekly summary email — one short mail per active business owner.

Run weekly by GitHub Actions (python manage.py send_weekly_summary): free,
no Celery worker needed. Safe to run twice: weekly_summary_sent_at stops a
second email inside the same week. Read-only accounts are skipped.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Avg
from django.utils import timezone

from reviews.models import BusinessProfile, Review
from reviews.services import billing

logger = logging.getLogger(__name__)

WEEK = timedelta(days=7)
MIN_GAP = timedelta(days=6)   # a run a bit early/late must not skip or double-send


def build_summary(profile, now):
    """Returns the numbers for the last 7 days, or None if there is nothing to report."""
    reviews = Review.objects.filter(
        user=profile.user, created_at__gte=now - WEEK, is_simulated=False,
    )
    total = reviews.count()
    if total == 0:
        return None
    avg = reviews.aggregate(avg=Avg('rating'))['avg'] or 0
    return {
        'total': total,
        'average': round(avg, 1),
        'replied': reviews.filter(status__in=('approved', 'posted')).count(),
        'waiting': reviews.filter(status='pending').exclude(ai_draft_reply__isnull=True)
                          .exclude(ai_draft_reply='').count(),
        'negative': reviews.filter(rating__lte=2).count(),
    }


def send_weekly_summaries(now=None):
    """Returns how many emails were sent."""
    now = now or timezone.now()
    sent = 0
    for profile in BusinessProfile.objects.select_related('user'):
        user = profile.user
        if not user or not user.email:
            continue
        if not billing.is_active(profile):
            continue   # read-only: nothing is being synced, so nothing to report
        if profile.weekly_summary_sent_at and now - profile.weekly_summary_sent_at < MIN_GAP:
            continue   # already sent this week

        stats = build_summary(profile, now)
        if stats is None:
            continue   # a quiet week: no email

        body = (
            f"Hi {user.first_name or user.username},\n\n"
            f"Your last 7 days at {profile.business_name}:\n\n"
            f"• New reviews: {stats['total']}\n"
            f"• Average rating: {stats['average']} / 5\n"
            f"• Replied: {stats['replied']}\n"
            f"• Drafts waiting for your approval: {stats['waiting']}\n"
            f"• 1–2★ reviews: {stats['negative']}\n\n"
            f"Open your dashboard to approve drafts and reply to anything still waiting.\n\n"
            f"— Mehrly"
        )
        try:
            send_mail(
                f"Your week at {profile.business_name}: {stats['total']} new review"
                f"{'s' if stats['total'] != 1 else ''}",
                body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False,
            )
        except Exception as e:   # one bad address must not stop everyone else's email
            logger.warning("Weekly summary failed for user %s: %s", user.id, e)
            continue
        BusinessProfile.objects.filter(id=profile.id).update(weekly_summary_sent_at=now)
        sent += 1
    return sent
