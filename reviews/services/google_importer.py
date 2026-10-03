import re
import requests
import logging
from django.conf import settings
from langdetect import detect, LangDetectException, DetectorFactory
from reviews.models import Review, BusinessProfile
from reviews.tasks import send_negative_review_alert
from reviews.services.ai_responder import detect_review_language
from .exceptions import RateLimitError
from reviews.services.dataforseo_importer import fetch_reviews, place_id_from_url

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

    if detected in ("fr", "en", "it", "de"):
        return detect_review_language(text, fallback_language=detected)


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

    profile = BusinessProfile.objects.filter(user=user).first()
    place_id = (place_id or '').strip() or place_id_from_url(profile.google_review_url if profile else None)

    # Cost control: full backlog only on the first sync, then the 10 newest.
    first_sync = not Review.objects.filter(user=user, business_name=business_name, source='google').exists()
    depth = max_reviews if first_sync else 10
    depth = ((depth + 9) // 10) * 10  # DataForSEO bills per 10 reviews

    try:
        items, info = fetch_reviews(business_name, place_id=place_id, depth=depth, priority=priority, wait_seconds=wait_seconds)
    except RateLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch Google reviews via DataForSEO: {e}")
        raise

    update_fields = {}
    if info.get('cid'):
        update_fields['google_maps_url'] = f"https://www.google.com/maps?cid={info['cid']}"
    if info.get('place_id'):
        update_fields['google_review_url'] = f"https://search.google.com/local/writereview?placeid={info['place_id']}"
    if update_fields:
        BusinessProfile.objects.filter(user=user).update(**update_fields)

    imported_count = 0
    auto_posted_count = 0

    # Reversed on purpose: items arrive newest-first, and created_at is set
    # at save time, so saving oldest-first keeps the dashboard order right.
    for item in reversed(items):
        comment_text = (item.get('original_review_text') or item.get('review_text') or '').strip()
        if not comment_text:
            continue

        external_id = item.get('review_id')
        review_url = item.get('review_url')
        reviewer_name = item.get('profile_name') or 'Anonymous Customer'
        rating = (item.get('rating') or {}).get('value') or 5
        has_owner_response = bool((item.get('owner_answer') or '').strip())

        existing = None
        if external_id:
            existing = Review.objects.filter(
                user=user, business_name=business_name, external_id=external_id
            ).first()
        if existing is None:
            existing = Review.objects.filter(
                user=user, business_name=business_name,
                reviewer_name=reviewer_name, comment=comment_text,
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

        new_review = Review.objects.create(
            user=user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment_text,
            detected_language=language,
            business_name=business_name,
            source='google',
            status='posted' if has_owner_response else 'pending',
            external_id=external_id,
            review_url=review_url,
        )
        imported_count += 1

        if rating <= 2 and not has_owner_response:
            send_negative_review_alert.delay(new_review.id)

    return imported_count, auto_posted_count



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