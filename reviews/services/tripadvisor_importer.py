import re
import logging
from django.conf import settings
from reviews.models import Review, BusinessProfile
from .dataforseo_importer import TRIPADVISOR, post_task, wait_for_result
from .exceptions import RateLimitError
from .review_pipeline import auto_draft_new_reviews

logger = logging.getLogger(__name__)

# DataForSEO needs a location when it looks a restaurant up by name.
# Format: "City,Region,Country". Not verified against their location list yet:
# if the first sync fails with a location error, check the exact spelling in
# DataForSEO's Tripadvisor locations list and edit this line.
TRIPADVISOR_LOCATION = 'Geneva,Geneva,Switzerland'

_LANGUAGE_BY_NAME = {'english': 'en', 'french': 'fr', 'german': 'de', 'italian': 'it'}
_SUPPORTED = ('en', 'fr', 'de', 'it')


def _normalize_language(value):
    text = (value or '').strip().lower()
    if text in _LANGUAGE_BY_NAME:
        return _LANGUAGE_BY_NAME[text]
    return text[:2] if text[:2] in _SUPPORTED else 'fr'


def _url_path_from_listing(url):
    """'https://www.tripadvisor.com/Restaurant_Review-g1-d2-Reviews-X.html' -> 'Restaurant_Review-g1-d2-Reviews-X.html'"""
    match = re.search(r'tripadvisor\.[a-z.]+/(.+?\.html)', url or '')
    return match.group(1) if match else None


def tripadvisor_task(business_name, url_path, depth, priority=2):
    task = {'depth': depth, 'priority': priority}
    if url_path:
        task['url_path'] = url_path
    else:
        task['keyword'] = business_name
        task['location_name'] = TRIPADVISOR_LOCATION
    return task


def _fetch_from_dataforseo(business_name, url_path, depth, wait_seconds=90, priority=2):
    """Blocking — background use only."""
    task_id = post_task(TRIPADVISOR, tripadvisor_task(business_name, url_path, depth, priority))
    return wait_for_result(TRIPADVISOR, task_id, wait_seconds)


def plan_tripadvisor_fetch(user):
    """(url_path, saved_url): reuse the saved listing for a precise, cheap lookup."""
    profile = BusinessProfile.objects.filter(user=user).first()
    saved_url = getattr(profile, 'tripadvisor_url', None)
    return _url_path_from_listing(saved_url), saved_url


def fetch_live_tripadvisor_reviews(user, business_name: str = "Geneva Bistro", max_reviews: int = 30):
    """
    Fetches real public TripAdvisor reviews through DataForSEO.
    Same shape as before: returns (imported_count, listing_url), and saves the
    resolved TripAdvisor listing URL on the BusinessProfile.
    """
    if not getattr(settings, 'DATAFORSEO_LOGIN', None) or not getattr(settings, 'DATAFORSEO_PASSWORD', None):
        logger.warning("DATAFORSEO credentials not found in settings — cannot fetch TripAdvisor reviews.")
        return 0, None

    # The saved listing is reused as-is (a precise lookup, no name search).
    # Switching to a different business clears it, so it can't be stale.
    url_path, saved_url = plan_tripadvisor_fetch(user)

    depth = 10  # newest 10 reviews only: DataForSEO bills per 10 reviews

    try:    
        result = _fetch_from_dataforseo(business_name, url_path, depth)
    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch TripAdvisor reviews via DataForSEO: {e}")
        raise

    return import_tripadvisor_result(user, business_name, result, saved_url, max_reviews)


def import_tripadvisor_result(user, business_name, result, saved_url=None, max_reviews=30, draft_now=True):
    """Saves a finished DataForSEO TripAdvisor result. Returns (imported_count, listing_url)."""
    resolved = result.get('url_path')
    listing_url = f"https://www.tripadvisor.com/{resolved}" if resolved else saved_url
    if resolved:
        BusinessProfile.objects.filter(user=user).update(tripadvisor_url=listing_url)

    imported_count = 0
    new_review_ids = []
    for item in reversed((result.get('items') or [])[:max_reviews]):
        comment_text = (item.get('review_text') or '').strip()
        if not comment_text:
            continue

        rating_raw = item.get('rating')
        rating_raw = rating_raw.get('value') if isinstance(rating_raw, dict) else rating_raw
        try:
            rating = min(5, max(1, int(round(float(rating_raw)))))
        except (TypeError, ValueError):
            continue

        reviewer_name = ((item.get('user_profile') or {}).get('name') or 'Anonymous Traveler')[:255]
        review_id = item.get('review_id')
        external_id = f"ta:{review_id}" if review_id else None

        if external_id and Review.objects.filter(user=user, external_id=external_id).exists():
            continue
        # Older reviews imported before this change have no external_id.
        if Review.objects.filter(
            user=user, reviewer_name=reviewer_name, comment=comment_text,
        ).exists():
            continue

        new_review = Review.objects.create(
            user=user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment_text,
            detected_language=_normalize_language(item.get('original_language') or item.get('language')),
            business_name=business_name,
            status='pending',
            source='tripadvisor',
            external_id=external_id,
            review_url=(item.get('url') or None),
        )
        imported_count += 1
        new_review_ids.append(new_review.id)

    if draft_now:
        # TripAdvisor replies can't be posted automatically, but drafts are ready.
        auto_draft_new_reviews(new_review_ids)
    return imported_count, listing_url