import re
import time
import logging
import unicodedata
import requests
from django.conf import settings
from reviews.models import Review, BusinessProfile
from .dataforseo_importer import _auth, _base, _STILL_WORKING
from .exceptions import RateLimitError

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


def _slug(text):
    ascii_text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', ascii_text.lower())


def _url_path_from_listing(url):
    """'https://www.tripadvisor.com/Restaurant_Review-g1-d2-Reviews-X.html' -> 'Restaurant_Review-g1-d2-Reviews-X.html'"""
    match = re.search(r'tripadvisor\.[a-z.]+/(.+?\.html)', url or '')
    return match.group(1) if match else None


def _fetch_from_dataforseo(business_name, url_path, depth, wait_seconds=90):
    task = {
        'depth': depth,
        'sort_by': 'most_recent',
        'translate_reviews': False,   # keep the original language so replies match it
        'priority': 2,                # high priority: results in about a minute
    }
    if url_path:
        task['url_path'] = url_path
    else:
        task['keyword'] = business_name
        task['location_name'] = TRIPADVISOR_LOCATION

    resp = requests.post(
        f"{_base()}/business_data/tripadvisor/reviews/task_post",
        json=[task], auth=_auth(), timeout=30,
    )
    if resp.status_code == 429:
        raise RateLimitError("DataForSEO rate limit hit.")
    body = resp.json()
    if body.get('status_code') != 20000:
        raise Exception(f"DataForSEO {body.get('status_code')}: {body.get('status_message')}")
    created = body['tasks'][0]
    if created['status_code'] != 20100:
        raise Exception(f"DataForSEO {created['status_code']}: {created['status_message']}")

    task_id = created['id']
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        got = requests.get(
            f"{_base()}/business_data/tripadvisor/reviews/task_get/{task_id}",
            auth=_auth(), timeout=30,
        ).json()['tasks'][0]
        if got.get('result'):
            return got['result'][0]
        if got['status_code'] not in _STILL_WORKING:
            raise Exception(f"DataForSEO {got['status_code']}: {got['status_message']}")
    raise TimeoutError(f"DataForSEO task {task_id} not ready after {wait_seconds}s.")


def fetch_live_tripadvisor_reviews(user, business_name: str = "Geneva Bistro", max_reviews: int = 30):
    """
    Fetches real public TripAdvisor reviews through DataForSEO.
    Same shape as before: returns (imported_count, listing_url), and saves the
    resolved TripAdvisor listing URL on the BusinessProfile.
    """
    if not getattr(settings, 'DATAFORSEO_LOGIN', None) or not getattr(settings, 'DATAFORSEO_PASSWORD', None):
        logger.warning("DATAFORSEO credentials not found in settings — cannot fetch TripAdvisor reviews.")
        return 0, None

    profile = BusinessProfile.objects.filter(user=user).first()
    saved_url = getattr(profile, 'tripadvisor_url', None)
    url_path = _url_path_from_listing(saved_url)
    # Only trust the saved link if it belongs to the business being synced.
    if url_path and _slug(business_name) not in _slug(url_path):
        url_path = None

    depth = max(10, -(-max_reviews // 10) * 10)  # DataForSEO bills per 10 reviews

    try:
        result = _fetch_from_dataforseo(business_name, url_path, depth)
    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch TripAdvisor reviews via DataForSEO: {e}")
        raise

    resolved = result.get('url_path')
    listing_url = f"https://www.tripadvisor.com/{resolved}" if resolved else saved_url
    if resolved:
        BusinessProfile.objects.filter(user=user).update(tripadvisor_url=listing_url)

    imported_count = 0
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
            user=user, business_name=business_name,
            reviewer_name=reviewer_name, comment=comment_text,
        ).exists():
            continue

        Review.objects.create(
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

    return imported_count, listing_url