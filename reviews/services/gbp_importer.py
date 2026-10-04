"""
Import reviews directly from Google Business Profile (only for owners who connected their account).

Imported reviews get external_id = "gbp:<googleReviewId>". That prefix is how
approve_review_view knows it can post the reply to Google automatically.
"""
import logging

from reviews.models import Review
from reviews.services import gbp_client
from reviews.services.google_importer import _guess_language, create_review_once, parse_rating
from reviews.tasks import send_negative_review_alert
from reviews.services.review_pipeline import auto_draft_new_reviews

logger = logging.getLogger(__name__)


def import_reviews(profile, user, business_name, draft_now=True):
    """Returns (imported_count, already_replied_count) - same shape as fetch_live_google_reviews."""
    first_sync = not Review.objects.filter(user=user, source='google', is_simulated=False).exists()
    items = gbp_client.fetch_reviews(profile, max_reviews=200 if first_sync else 50)

    imported_count = 0
    replied_count = 0
    new_review_ids, alert_review_ids = [], []

    # Newest-first from Google; save oldest-first so the dashboard order stays right.
    for item in reversed(items):
        google_id = item.get('external_id')
        comment_text = (item.get('comment') or '').strip()
        rating = parse_rating(item.get('rating'))
        # Rating-only reviews (no text) and unreadable ratings are skipped, like the DataForSEO import.
        if not google_id or not comment_text or not rating:
            continue

        external_id = f"gbp:{google_id}"
        reviewer_name = item.get('reviewer_name') or 'Anonymous Customer'
        has_reply = item.get('has_reply', False)

        existing = Review.objects.filter(
            user=user, external_id=external_id
        ).first()
        if existing is None:
            # Same review may already exist from the DataForSEO import - adopt it
            # so it can be auto-posted too, instead of creating a duplicate.
            existing = Review.objects.filter(
                user=user, reviewer_name=reviewer_name, comment=comment_text,
            ).first()

        if existing:
            changed = False
            if existing.external_id != external_id:
                existing.external_id = external_id
                changed = True
            if has_reply and existing.status != 'posted':
                existing.status = 'posted'
                changed = True
                replied_count += 1
            if changed:
                existing.save()
            continue

        new_review = create_review_once(
            user=user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment_text,
            detected_language=_guess_language(comment_text),
            business_name=business_name,
            source='google',
            status='posted' if has_reply else 'pending',
            external_id=external_id,
        )
        if new_review is None:
            continue
        imported_count += 1
        if not has_reply:
            new_review_ids.append(new_review.id)
            if rating <= 2:
                alert_review_ids.append(new_review.id)

    if draft_now:
        # Draft the newest few automatically (and auto-post when safe), THEN send
        # alerts so the email can truthfully say a draft is waiting.
        auto_draft_new_reviews(new_review_ids)
        for rid in alert_review_ids:
            send_negative_review_alert.delay(rid)

    return imported_count, replied_count