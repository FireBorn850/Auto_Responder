import requests
import logging
from django.conf import settings
from reviews.models import Review, BusinessProfile
from .exceptions import RateLimitError

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"

# TripAdvisor tags each review with its own detected language code, which
# is reliable enough to trust directly for the languages we now support —
# no need to re-run detection ourselves and spend an extra API call.
_TA_LANGUAGE_MAP = {
    'en': 'en',
    'fr': 'fr',
    'de': 'de',
    'it': 'it',
}


def _normalize_language(ta_language: str) -> str:
    code = (ta_language or '').lower()[:2]
    return _TA_LANGUAGE_MAP.get(code, 'fr')


def fetch_live_tripadvisor_reviews(user, business_name: str = "Geneva Bistro", max_reviews: int = 30):
    """
    Fetches real public TripAdvisor reviews for a business by name, using
    SerpAPI's TripAdvisor engines — first resolving the business to a
    place_id via a name search, then pulling its reviews. Mirrors the
    Google Maps importer's shape: returns (imported_count, listing_url).

    Also saves the resolved TripAdvisor listing URL onto the business's
    BusinessProfile, so it can be reused elsewhere (e.g. shown in
    Integrations) without a repeat lookup.
    """
    api_key = getattr(settings, 'SERPAPI_KEY', None)

    if not api_key:
        logger.warning("SERPAPI_KEY not found in settings — cannot fetch TripAdvisor reviews.")
        return 0, None

    try:
        # Step 1: find the business on TripAdvisor to get its place_id.
        search_params = {
            'engine': 'tripadvisor',
            'q': business_name,
            'api_key': api_key,
        }
        search_resp = requests.get(SERPAPI_BASE_URL, params=search_params, timeout=15)
        if search_resp.status_code == 429:
            raise RateLimitError("SerpAPI rate limit hit while searching TripAdvisor.")
        search_data = search_resp.json()
        if 'error' in search_data:
            err_msg = search_data['error']
            if any(w in err_msg.lower() for w in ['rate limit', 'quota', 'run out of searches', 'account has run out']):
                raise RateLimitError(err_msg)
            raise Exception(f"SerpAPI error: {err_msg}")

        places = search_data.get('places') or []
        if not places:
            logger.error(f"SerpAPI: no TripAdvisor listing found for '{business_name}'.")
            return 0, None

        first_result = places[0]
        place_id = first_result.get('place_id')
        listing_url = first_result.get('link')

        if not place_id:
            logger.error(f"SerpAPI: TripAdvisor result for '{business_name}' has no place_id.")
            return 0, None

        if listing_url:
            BusinessProfile.objects.filter(user=user).update(tripadvisor_url=listing_url)

        # Step 2: fetch reviews for that listing
        reviews_params = {
            'engine': 'tripadvisor_reviews',
            'place_id': place_id,
            'api_key': api_key,
        }
        reviews_resp = requests.get(SERPAPI_BASE_URL, params=reviews_params, timeout=15)
        if reviews_resp.status_code == 429:
            raise RateLimitError("SerpAPI rate limit hit while fetching TripAdvisor reviews.")
        reviews_data = reviews_resp.json()
        if 'error' in reviews_data:
            err_msg = reviews_data['error']
            if any(w in err_msg.lower() for w in ['rate limit', 'quota', 'run out of searches', 'account has run out']):
                raise RateLimitError(err_msg)
            raise Exception(f"SerpAPI error: {err_msg}")

        reviews_list = (reviews_data.get('reviews') or [])[:max_reviews]
        imported_count = 0

        for r in reversed(reviews_list):
            comment_text = (r.get('snippet') or '').strip()
            if not comment_text:
                continue

            author = r.get('author') or {}
            reviewer_name = author.get('display_name') or author.get('username') or 'Anonymous Traveler'
            rating = r.get('rating', 5)
            language = _normalize_language(r.get('language'))

            already_exists = Review.objects.filter(
                user=user,
                business_name=business_name,
                reviewer_name=reviewer_name,
                comment=comment_text,
            ).exists()

            if not already_exists:
                Review.objects.create(
                    user=user,
                    reviewer_name=reviewer_name,
                    rating=rating,
                    comment=comment_text,
                    detected_language=language,
                    business_name=business_name,
                    status='pending',
                    source='tripadvisor',
                )
                imported_count += 1

        return imported_count, listing_url

    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch TripAdvisor reviews via SerpAPI: {e}")
        raise