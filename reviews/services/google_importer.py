import re
import requests
import logging
from django.conf import settings
from langdetect import detect, LangDetectException, DetectorFactory
from reviews.models import Review, BusinessProfile
from reviews.tasks import send_negative_review_alert
from reviews.services.ai_responder import detect_review_language
from .exceptions import RateLimitError

# langdetect isn't fully deterministic run-to-run unless seeded — pin it so
# the same review text always yields the same language, not a coin flip.
DetectorFactory.seed = 0

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"

# Fallback only — used when langdetect can't make a confident call (e.g.
# very short text like "Super !" or "Great!"). Requires a *ratio* of hits,
# not just one match, since a single loanword ("a la carte") or one
# accented business name shouldn't be enough to flip the whole review.
_FRENCH_HINTS = re.compile(
    r"\b(le|la|les|un|une|des|est|très|nous|avons|été|pour|avec|c'est|qui|pas)\b",
    re.IGNORECASE,
)


def _guess_language(text: str) -> str:
    """
    Fast first pass with langdetect (no API call, no cost). French,
    English, and Italian results are confident enough to trust directly.
    A "de" result gets a second, smarter pass through Gemini
    (detect_review_language) since langdetect has no concept of
    Swiss-German dialect and will call any Swiss-German text "de" even
    when it should really be "gsw" — that distinction genuinely needs
    the AI check, not a word-list.
    """
    text = (text or "").strip()
    if not text:
        return "fr"

    try:
        detected = detect(text)
    except LangDetectException:
        detected = None

    if detected in ("fr", "en", "it"):
        return detected

    if detected == "de":
        return detect_review_language(text)

    # Unrecognized by langdetect (too short, ambiguous, or a language
    # outside our five) — fall back to the French/English word-hint
    # heuristic rather than guessing blindly.
    words = text.split()
    if not words:
        return "fr"
    hits = len(_FRENCH_HINTS.findall(text))
    return "fr" if (hits / len(words)) > 0.15 else "en"


def _maps_url_from_data_id(data_id: str):
    """
    Converts a SerpAPI Google Maps data_id (e.g. "0x4761...:0x89ab...")
    into a real, working Google Maps URL using the place's CID — the
    hex value after the colon, read as a decimal number. This is more
    reliable than SerpAPI's optional 'link' field, which isn't always
    present in the response.
    """
    try:
        if not data_id or ':' not in data_id:
            return None
        hex_part = data_id.split(':')[-1]
        if hex_part.lower().startswith('0x'):
            hex_part = hex_part[2:]
        cid = int(hex_part, 16)
        return f"https://www.google.com/maps?cid={cid}"
    except (ValueError, AttributeError):
        return None


def fetch_live_google_reviews(place_id: str, user, business_name: str = "Geneva Bistro", max_reviews: int = 30) -> int:
    """
    Fetches real public Google Maps reviews for a business by name, using
    SerpAPI (no Google Business Profile API approval required). Falls back
    to demo sample reviews if no SERPAPI_KEY is configured.

    `place_id` is accepted for backward compatibility but is no longer
    required — SerpAPI looks the business up by name.

    Paginates through SerpAPI's next_page_token up to `max_reviews` total,
    since a single call only returns ~8 reviews.
    """
    api_key = getattr(settings, 'SERPAPI_KEY', None)

    if not api_key:
        logger.warning("SERPAPI_KEY not found in settings. Running demo importer.")
        return _import_demo_real_reviews(user, business_name), 0

    try:
        # Step 1: find the business on Google Maps to get its data_id
        search_params = {
            'engine': 'google_maps',
            'q': business_name,
            'type': 'search',
            'api_key': api_key,
        }
        search_resp = requests.get(SERPAPI_BASE_URL, params=search_params, timeout=15)
        if search_resp.status_code == 429:
            raise RateLimitError("SerpAPI rate limit hit while searching Google Maps.")
        search_data = search_resp.json()
        if 'error' in search_data:
            err_msg = search_data['error']
            if any(w in err_msg.lower() for w in ['rate limit', 'quota', 'run out of searches', 'account has run out']):
                raise RateLimitError(err_msg)
            raise Exception(f"SerpAPI error: {err_msg}")

        local_results = search_data.get('local_results') or []
        place_data = search_data.get('place_results')

        data_id = None
        place_id = None
        if place_data:
            data_id = place_data.get('data_id')
            place_id = place_data.get('place_id')
        elif local_results:
            data_id = local_results[0].get('data_id')
            place_id = local_results[0].get('place_id')

        if not data_id:
            logger.error(f"SerpAPI: no Google Maps listing found for '{business_name}'.")
            return 0, 0

        # Build the real Google Maps URL from data_id directly, rather than
        # relying on a 'link' field SerpAPI doesn't consistently return.
        # data_id looks like "0x4761xxxx:0x89abxxxx" — the part after the
        # colon, read as hex, is the place's Maps CID, and
        # maps.google.com/?cid=<decimal CID> reliably opens that exact
        # business's public Maps page for anyone, no login required.
        maps_url = _maps_url_from_data_id(data_id)

        # Google's official "write a review" deep link — sends a customer
        # straight to the review composer for this exact business, not just
        # its general page. Powers QR Code Booster's auto-fill.
        review_url = f"https://search.google.com/local/writereview?placeid={place_id}" if place_id else None

        # Save both so the dashboard's "Open Google Business" button and
        # QR Code Booster's auto-fill both work off real data.
        update_fields = {}
        if maps_url:
            update_fields['google_maps_url'] = maps_url
        if review_url:
            update_fields['google_review_url'] = review_url
        if update_fields:
            BusinessProfile.objects.filter(user=user).update(**update_fields)

        # Step 2: fetch reviews for that listing, paginating for more than
        # the ~8 a single call returns.
        reviews_list = []
        next_page_token = None

        while len(reviews_list) < max_reviews:
            reviews_params = {
                'engine': 'google_maps_reviews',
                'data_id': data_id,
                'api_key': api_key,
                'hl': 'en',
            }
            if next_page_token:
                reviews_params['next_page_token'] = next_page_token

            reviews_resp = requests.get(SERPAPI_BASE_URL, params=reviews_params, timeout=15)
            if reviews_resp.status_code == 429:
                raise RateLimitError("SerpAPI rate limit hit while fetching reviews.")
            reviews_data = reviews_resp.json()
            if 'error' in reviews_data:
                err_msg = reviews_data['error']
                if any(w in err_msg.lower() for w in ['rate limit', 'quota', 'run out of searches', 'account has run out']):
                    raise RateLimitError(err_msg)
                raise Exception(f"SerpAPI error: {err_msg}")

            page_reviews = reviews_data.get('reviews') or []
            if not page_reviews:
                break

            reviews_list.extend(page_reviews)

            next_page_token = (reviews_data.get('serpapi_pagination') or {}).get('next_page_token')
            if not next_page_token:
                break

        reviews_list = reviews_list[:max_reviews]
        imported_count = 0
        auto_posted_count = 0

        # Reversed on purpose: SerpAPI returns the top/most-relevant review
        # first, but Review.created_at is set at save time, and the
        # dashboard sorts newest-first. Saving in reverse means the review
        # that's actually first on Google Maps gets the latest timestamp,
        # so it correctly shows up first on the dashboard too.
        for r in reversed(reviews_list):
            comment_text = (r.get('snippet') or '').strip()
            if not comment_text:
                continue

            reviewer_name = (r.get('user') or {}).get('name', 'Anonymous Customer')
            rating = r.get('rating', 5)
            language = _guess_language(comment_text)

            # SerpAPI includes the owner's existing reply (if any) under
            # 'response'. If it's there, the reply is genuinely live on
            # Google — this is how we auto-detect "posted" instead of
            # guessing or asking the manager to confirm.
            response_obj = r.get('response') or {}
            has_owner_response = bool((response_obj.get('snippet') or '').strip())

            existing = Review.objects.filter(
                user=user,
                business_name=business_name,
                reviewer_name=reviewer_name,
                comment=comment_text,
            ).first()

            if existing:
                if has_owner_response and existing.status != 'posted':
                    existing.status = 'posted'
                    existing.save()
                    auto_posted_count += 1
                continue

            new_review = Review.objects.create(
                user=user,
                reviewer_name=reviewer_name,
                rating=rating,
                comment=comment_text,
                detected_language=language,
                business_name=business_name,
                source='google',
                status='posted' if has_owner_response else 'pending',
            )
            imported_count += 1

            if rating <= 2 and not has_owner_response:
                send_negative_review_alert.delay(new_review.id)

        return imported_count, auto_posted_count

    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch Google reviews via SerpAPI: {e}")
        raise


def _import_demo_real_reviews(user, business_name: str) -> int:
    """
    Fallback method: creates realistic French/English sample reviews
    if no SERPAPI_KEY is configured yet.
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
            business_name=business_name,
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