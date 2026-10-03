"""
Manual syncs that never block a web request.

Before: the "Import Reviews" button submitted a DataForSEO task and then sat
inside the same request waiting up to 90 s for the data (plus AI drafting).
Gunicorn kills requests after 30 s, so in production the sync died.

Now:
  start_*_sync()  -> submits the task (1 fast call) and saves a SyncJob.
  advance(job)    -> called by the dashboard every few seconds; does ONE
                     short step: check if the data is ready, OR import it,
                     OR write one AI draft, OR finish up.

No Celery worker or Redis needed, so it costs nothing extra. If the owner
closes the tab, the job simply continues the next time they open the
dashboard (or during the nightly run).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from reviews.models import BusinessProfile, Review, SyncJob, SyncLog
from reviews.services import dataforseo_importer as dfs
from reviews.services.review_pipeline import draft_reply

logger = logging.getLogger(__name__)

GIVE_UP_AFTER = timedelta(hours=6)   # DataForSEO normally answers in minutes


def _manual_priority():
    # 1 = normal queue (cheapest). 2 = high priority, faster but billed higher.
    return int(getattr(settings, 'DATAFORSEO_MANUAL_PRIORITY', 1))


def _has_dataforseo():
    return bool(getattr(settings, 'DATAFORSEO_LOGIN', None) and getattr(settings, 'DATAFORSEO_PASSWORD', None))


def active_job(user):
    return SyncJob.objects.filter(user=user, state__in=('waiting', 'drafting')).first()


# ---------------------------------------------------------------- starting

def start_google_sync(profile, place_id=''):
    from reviews.services import google_importer

    job = SyncJob.objects.create(user=profile.user, platform='google', business_name=profile.business_name)

    if profile.gbp_connected:
        from reviews.services import gbp_client, gbp_importer
        try:
            imported, answered = gbp_importer.import_reviews(profile, profile.user, profile.business_name, draft_now=False)
            return _to_drafting(job, imported, answered)
        except gbp_client.GBPError as e:
            logger.warning("GBP import failed, falling back to DataForSEO: %s", e)

    if not _has_dataforseo():
        imported = google_importer._import_demo_real_reviews(profile.user, profile.business_name)
        return _to_drafting(job, imported, 0)

    place_id, depth = google_importer.plan_google_fetch(profile.user, place_id)
    job.task_id = dfs.post_task(dfs.GOOGLE, dfs.google_task(profile.business_name, place_id, depth, _manual_priority()))
    job.save(update_fields=['task_id', 'updated_at'])
    return job


def start_tripadvisor_sync(profile, business_name):
    from reviews.services import tripadvisor_importer as ta

    job = SyncJob.objects.create(user=profile.user, platform='tripadvisor', business_name=business_name)
    if not _has_dataforseo():
        return _fail(job, "DataForSEO isn't configured on the server.")
    url_path, _ = ta.plan_tripadvisor_fetch(profile.user)
    job.task_id = dfs.post_task(dfs.TRIPADVISOR, ta.tripadvisor_task(business_name, url_path, 10, _manual_priority()))
    job.save(update_fields=['task_id', 'updated_at'])
    return job


# ---------------------------------------------------------------- stepping

def advance(job):
    """Does one short step on an active job. Safe to call from a web request."""
    with transaction.atomic():
        job = SyncJob.objects.select_for_update().get(pk=job.pk)
        if job.state == 'waiting':
            _step_waiting(job)
        elif job.state == 'drafting':
            _step_drafting(job)
    return job


def _step_waiting(job):
    endpoint = dfs.GOOGLE if job.platform == 'google' else dfs.TRIPADVISOR
    try:
        result = dfs.get_task_result(endpoint, job.task_id)
    except Exception as e:
        return _fail(job, str(e))

    if result is None:
        if timezone.now() - job.created_at > GIVE_UP_AFTER:
            return _fail(job, "DataForSEO didn't return the reviews in time. Please try again later.")
        return job

    if job.platform == 'google':
        from reviews.services import google_importer
        items, info = dfs.google_items(result)
        imported, answered = google_importer.import_google_items(job.user, job.business_name, items, info, draft_now=False)
    else:
        from reviews.services import tripadvisor_importer as ta
        profile = BusinessProfile.objects.filter(user=job.user).first()
        imported, listing_url = ta.import_tripadvisor_result(
            job.user, job.business_name, result, getattr(profile, 'tripadvisor_url', None), draft_now=False)
        answered = 0
    return _to_drafting(job, imported, answered)


def _to_drafting(job, imported, answered):
    limit = getattr(settings, 'AUTO_DRAFT_MAX_PER_SYNC', 5)
    queue = list(
        Review.objects.filter(user=job.user, created_at__gte=job.created_at, status='pending', is_simulated=False)
        .filter(Q(ai_draft_reply__isnull=True) | Q(ai_draft_reply=''))
        .order_by('-id').values_list('id', flat=True)[:limit]
    )
    job.state = 'drafting'
    job.imported_count = imported
    job.already_answered_count = answered
    job.draft_queue = queue
    job.save()
    return job


def _step_drafting(job):
    if job.draft_queue:
        review_id = job.draft_queue.pop(0)
        job.save(update_fields=['draft_queue', 'updated_at'])
        review = Review.objects.filter(id=review_id, status='pending').first()
        profile = BusinessProfile.objects.filter(user=job.user).first()
        if review and profile and not review.ai_draft_reply:
            try:
                draft_reply(review, profile)
            except Exception as e:  # one bad draft must not stop the sync
                logger.warning("Draft failed for review %s: %s", review_id, e)
        return job
    return _finish(job)


def _finish(job):
    from reviews.tasks import send_negative_review_alert

    # Alerts go out after drafting, so the email can say a draft is waiting.
    new_negative = Review.objects.filter(
        user=job.user, created_at__gte=job.created_at, rating__lte=2, is_simulated=False,
        alert_sent_at__isnull=True, alert_due_at__isnull=True,
    ).exclude(status='posted').values_list('id', flat=True)
    for rid in new_negative:
        send_negative_review_alert.delay(rid)

    parts = []
    if job.imported_count:
        parts.append(f"{job.imported_count} new review{'s' if job.imported_count != 1 else ''}")
    if job.already_answered_count:
        parts.append(f"{job.already_answered_count} already answered on Google")
    job.detail = (', '.join(parts) if parts else 'No new reviews found')[:255]
    job.state = 'done'
    job.save(update_fields=['detail', 'state', 'updated_at'])
    SyncLog.objects.create(user=job.user, platform=job.platform, status='success', detail=job.detail)
    return job


def _fail(job, reason):
    job.state = 'failed'
    job.detail = (reason or 'Sync failed')[:255]
    job.save(update_fields=['state', 'detail', 'updated_at'])
    SyncLog.objects.create(user=job.user, platform=job.platform, status='failed', detail=job.detail)
    return job


def finish_abandoned_jobs(max_steps=50):
    """
    Nightly safety net (run by the GitHub Actions sync): completes jobs whose
    owner closed the tab. Fetching a finished DataForSEO task is free.
    """
    finished = 0
    for job in SyncJob.objects.filter(state__in=('waiting', 'drafting')):
        for _ in range(max_steps):
            job = advance(job)
            if not job.is_active:
                finished += 1
                break
            if job.state == 'waiting':
                break   # data still not ready: try again tomorrow, don't hammer the API
    return finished
