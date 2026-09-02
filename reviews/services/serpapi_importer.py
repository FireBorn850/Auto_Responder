# import requests
# import logging
# from django.conf import settings
# from reviews.models import Review

# logger = logging.getLogger(__name__)

# FRENCH_MARKERS = [
#     " le ", " la ", " les ", " des ", " une ", " est ", " et ", " avec ",
#     " pas ", " très ", " nous ", " vous ", " service ", " merci ", " bon ",
# ]


# def _guess_language(text: str) -> str:
#     lowered = f" {text.lower()} "
#     hits = sum(1 for marker in FRENCH_MARKERS if marker in lowered)
#     return 'fr' if hits >= 2 else 'en'


# def find_data_id(business_name: str, location: str = "") -> str | None:
#     api_key = getattr(settings, 'SERPAPI_KEY', None)
#     if not api_key:
#         logger.warning("SERPAPI_KEY not configured.")
#         return None

#     query = f"{business_name} {location}".strip()
#     params = {
#         'engine': 'google_maps',
#         'q': query,
#         'api_key': api_key,
#     }

#     try:
#         response = requests.get('https://serpapi.com/search', params=params, timeout=10)
#         data = response.json()

#         place = data.get('place_results')
#         if place and place.get('data_id'):
#             return place['data_id']

#         local_results = data.get('local_results', [])
#         if local_results:
#             return local_results[0].get('data_id')

#         return None
#     except Exception as e:
#         logger.error(f"SerpApi place lookup failed: {e}")
#         return None


# def fetch_reviews_via_serpapi(business_name: str, user, location: str = "", data_id: str = None) -> int:
#     api_key = getattr(settings, 'SERPAPI_KEY', None)
#     if not api_key:
#         logger.warning("SERPAPI_KEY not configured — cannot fetch live reviews.")
#         return 0

#     resolved_data_id = data_id or find_data_id(business_name, location)
#     if not resolved_data_id:
#         logger.error(f"Could not resolve a data_id for '{business_name}'.")
#         return 0

#     params = {
#         'engine': 'google_maps_reviews',
#         'data_id': resolved_data_id,
#         'hl': 'en',
#         'api_key': api_key,
#     }

#     try:
#         response = requests.get('https://serpapi.com/search', params=params, timeout=15)
#         data = response.json()

#         place_info = data.get('place_info', {})
#         resolved_business_name = place_info.get('title', business_name)

#         reviews_data = data.get('reviews', [])
#         imported_count = 0

#         for r in reviews_data:
#             comment_text = (r.get('snippet') or '').strip()
#             if not comment_text:
#                 continue

#             reviewer_name = r.get('user', {}).get('name', 'Anonymous Customer')
#             rating = r.get('rating', 5)
#             language = _guess_language(comment_text)

#             already_exists = Review.objects.filter(
#                 user=user,
#                 reviewer_name=reviewer_name,
#                 comment=comment_text
#             ).exists()

#             if not already_exists:
#                 Review.objects.create(
#                     user=user,
#                     reviewer_name=reviewer_name,
#                     rating=rating,
#                     comment=comment_text,
#                     detected_language=language,
#                     business_name=resolved_business_name,
#                     status='pending'
#                 )
#                 imported_count += 1

#         return imported_count

#     except Exception as e:
#         logger.error(f"SerpApi reviews fetch failed: {e}")
#         return 0