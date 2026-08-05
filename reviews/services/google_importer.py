import requests
import logging
from django.conf import settings
from reviews.models import Review

logger = logging.getLogger(__name__)

def fetch_live_google_reviews(place_id: str, user, business_name: str = "Geneva Bistro") -> int:
    """
    Fetches actual public reviews for a Google Place ID using Google Places API
    and populates them directly into the database.
    """
    api_key = getattr(settings, 'GOOGLE_PLACES_API_KEY', None)
    
    # Fallback to demo mode if no API key is provided
    if not api_key:
        logger.warning("GOOGLE_PLACES_API_KEY not found in settings. Running demo importer.")
        return _import_demo_real_reviews(user, business_name)

    # Google Places API (Details Endpoint)
    url = f"https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        'place_id': place_id,
        'fields': 'name,reviews',
        'key': api_key,
        'reviews_no_translations': 'true'
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if data.get('status') != 'OK':
            logger.error(f"Google Places API Error: {data.get('error_message', data.get('status'))}")
            return 0

        result = data.get('result', {})
        reviews_data = result.get('reviews', [])
        imported_count = 0

        for r in reviews_data:
            comment_text = r.get('text', '').strip()
            if not comment_text:
                continue

            reviewer_name = r.get('author_name', 'Anonymous Customer')
            rating = r.get('rating', 5)
            language = r.get('language', 'fr') # Default to French if unspecified

            # Avoid importing exact duplicate reviews
            already_exists = Review.objects.filter(
                user=user,
                reviewer_name=reviewer_name,
                comment=comment_text
            ).exists()

            if not already_exists:
                Review.objects.create(
                    user=user,
                    reviewer_name=reviewer_name,
                    rating=rating,
                    comment=comment_text,
                    detected_language=language,
                    business_name=business_name or result.get('name', 'Local Business'),
                    status='pending'
                )
                imported_count += 1

        return imported_count

    except Exception as e:
        logger.error(f"Failed to fetch Google reviews: {e}")
        return 0


def _import_demo_real_reviews(user, business_name: str) -> int:
    """
    Fallback method: Creates realistic French/English sample reviews 
    if no API key is configured yet.
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
        exists = Review.objects.filter(user=user, reviewer_name=s['name'], comment=s['comment']).exists()
        if not exists:
            Review.objects.create(
                user=user,
                reviewer_name=s['name'],
                rating=s['rating'],
                comment=s['comment'],
                detected_language=s['lang'],
                business_name=business_name,
                status='pending'
            )
            count += 1
    return count