import re
import requests
import logging
from django.conf import settings
from django.db import IntegrityError, transaction
from reviews.models import Review, BusinessProfile
from reviews.tasks import send_negative_review_alert
from reviews.services.language import guess_language
from .exceptions import RateLimitError
from reviews.services.dataforseo_importer import fetch_reviews, place_id_from_url
from reviews.services.review_pipeline import auto_draft_new_reviews

logger = logging.getLogger(__name__)


def parse_rating(value):
    """
    Star rating as a whole number 1-5, or None when it's missing or unreadable.
    Providers send it as 4, 4.0, "4", or {"value": 4}. A review without a real
    rating is skipped by every importer: guessing (the old code saved 5★)
    would hide unhappy customers and skip their alerts.
    """
    if isinstance(value, dict):
        value = value.get('value')
    try:
        stars = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return stars if 1 <= stars <= 5 else None


def create_review_once(**fields):
    """
    Creates the review, or returns None if the same provider review already
    exists (two syncs overlapping). The database constraint guarantees it;
    this just turns the clash into a quiet skip instead of a crashed sync.
    """
    try:
        with transaction.atomic():
            return Review.objects.create(**fields)
    except IntegrityError:
        return None


_guess_language = guess_language  # old name, still imported by gbp_importer

def fetch_live_google_reviews(place_id: str, user, business_name: str = "Geneva Bistro", max_reviews: int = 100, priority: int = 2, wait_seconds: int = 90):
    """
    Fetches Google reviews via DataForSEO. Returns (imported_count, auto_posted_count).
    Falls back to demo reviews if DataForSEO credentials are missing.
    """
    # Optional: owner connected their own Google Business Profile -> read reviews straight from Google
    _profile = BusinessProfile.objects.filter(user=user).first()
    if _profile and _profile.gbp_connected:
        from reviews.services import gbp_client, gbp_importer
        try:
            return gbp_importer.import_reviews(_profile, user, business_name)
        except gbp_client.GBPError as e:
            logger.warning(f"Google Business Profile import failed, falling back to DataForSEO: {e}")
    if not (getattr(settings, 'DATAFORSEO_LOGIN', None) and getattr(settings, 'DATAFORSEO_PASSWORD', None)):
        logger.warning("DataForSEO credentials not found in settings. Running demo importer.")
        return _import_demo_real_reviews(user, business_name), 0

    place_id, depth = plan_google_fetch(user, place_id, max_reviews)

    try:
        items, info = fetch_reviews(business_name, place_id=place_id, depth=depth, priority=priority, wait_seconds=wait_seconds)
    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch Google reviews via DataForSEO: {e}")
        raise

    return import_google_items(user, business_name, items, info)


def plan_google_fetch(user, place_id='', max_reviews=100):
    """Which place to ask for, and how many reviews (cost control)."""
    profile = BusinessProfile.objects.filter(user=user).first()
    place_id = (place_id or '').strip() or place_id_from_url(profile.google_review_url if profile else None)

    # Cost control: full backlog only on the first sync, then the 10 newest.
    first_sync = not Review.objects.filter(user=user, source='google', is_simulated=False).exists()
    depth = max_reviews if first_sync else 10
    depth = ((depth + 9) // 10) * 10  # DataForSEO bills per 10 reviews
    return place_id, depth


def import_google_items(user, business_name, items, info, draft_now=True):
    """
    Saves a finished DataForSEO result. With draft_now=False (the dashboard's
    step-by-step sync) drafting and alerts are left to the sync job, so this
    stays fast enough for a single web request.
    """
    update_fields = {}
    if info.get('cid'):
        update_fields['google_maps_url'] = f"https://www.google.com/maps?cid={info['cid']}"
    if info.get('place_id'):
        update_fields['google_review_url'] = f"https://search.google.com/local/writereview?placeid={info['place_id']}"
    if update_fields:
        BusinessProfile.objects.filter(user=user).update(**update_fields)

    imported_count = 0
    auto_posted_count = 0
    new_review_ids, alert_review_ids = [], []

    # Reversed on purpose: items arrive newest-first, and created_at is set
    # at save time, so saving oldest-first keeps the dashboard order right.
    for item in reversed(items):
        comment_text = (item.get('original_review_text') or item.get('review_text') or '').strip()
        if not comment_text:
            continue

        external_id = item.get('review_id')
        review_url = item.get('review_url')
        reviewer_name = item.get('profile_name') or 'Anonymous Customer'
        rating = parse_rating(item.get('rating'))
        if rating is None:
            continue  # no readable rating: skip instead of pretending it's 5★
        has_owner_response = bool((item.get('owner_answer') or '').strip())

        existing = None
        if external_id:
            existing = Review.objects.filter(
                user=user, external_id=external_id
            ).first()
        if existing is None:
            existing = Review.objects.filter(
                user=user, reviewer_name=reviewer_name, comment=comment_text,
            ).first()

        if existing:
            changed = False
            if external_id and not existing.external_id:
                existing.external_id = external_id
                changed = True
            if review_url and not existing.review_url:
                existing.review_url = review_url
                changed = True
            if has_owner_response and existing.status != 'posted':
                existing.status = 'posted'
                changed = True
                auto_posted_count += 1
            if changed:
                existing.save()
            continue

        orig_lang = (item.get('original_language') or '').lower()
        language = orig_lang if (orig_lang and orig_lang != 'de') else _guess_language(comment_text)

        new_review = create_review_once(
            user=user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment_text,
            detected_language=language,
            business_name=business_name,
            source='google',
            status='posted' if has_owner_response else 'pending',
            external_id=external_id or None,
            review_url=review_url,
        )
        if new_review is None:
            continue
        imported_count += 1
        if not has_owner_response:
            new_review_ids.append(new_review.id)
            if rating <= 2:
                alert_review_ids.append(new_review.id)

    if draft_now:
        # Draft the newest few automatically (and auto-post when safe), THEN send
        # alerts so the email can truthfully say a draft is waiting.
        auto_draft_new_reviews(new_review_ids)
        for rid in alert_review_ids:
            send_negative_review_alert.delay(rid)

    return imported_count, auto_posted_count



def _import_demo_real_reviews(user, business_name: str) -> int:
    """
    Fallback method: creates realistic French/English sample reviews
    if no DataForSEO credentials are configured yet.
    """
    samples = [
        {
            "name": "Jean-Pierre Blanc",
            "rating": 5,
            "comment": "Excellente expérience ! Le service était impeccable et le café délicieux. Je recommande vivement !",
            "lang": "fr"
        },
        {
            "name": "Sophie Martin",
            "rating": 2,
            "comment": "Attente trop longue pour avoir une table, et la boisson était froide. Déçue par l'accueil.",
            "lang": "fr"
        },
        {
            "name": "Michael Brown",
            "rating": 5,
            "comment": "Amazing atmosphere and great staff! Best espresso in town.",
            "lang": "en"
        }
    ]

    count = 0
    for s in samples:
        exists = Review.objects.filter(
            user=user,
            reviewer_name=s['name'],
            comment=s['comment'],
        ).exists()
        if not exists:
            Review.objects.create(
                user=user,
                reviewer_name=s['name'],
                rating=s['rating'],
                comment=s['comment'],
                detected_language=s['lang'],
                business_name=business_name,
                source='google',
                status='pending'
            )
            count += 1
    return count