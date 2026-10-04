import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Review, BusinessProfile
from .services import billing, ratelimit
from .services.ai_responder import detect_review_language, SUPPORTED_LANGUAGES
from .services.google_importer import create_review_once, parse_rating
from .services.review_pipeline import draft_reply

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 20_000          # a review is a few KB at most
MAX_NAME = 120
MAX_COMMENT = 5_000
MAX_BUSINESS_NAME = 255
MAX_EXTERNAL_ID = 200
WEBHOOK_HOURLY_LIMIT = 120       # per business; Gemini spend is also capped by the daily AI quota


def _error(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _text(data, key, limit, default=''):
    """A string field, trimmed and cut to `limit`. Non-strings are refused."""
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be text")
    return value.strip()[:limit]


@csrf_exempt
def google_review_webhook(request, token):
    """
    Receives a third-party review as JSON and creates it for whichever
    business owns `token` (BusinessProfile.webhook_token).

    Expected JSON: {"reviewer_name": str, "rating": 1-5, "comment": str,
                    "business_name"?: str, "detected_language"?: "fr|en|de|it|gsw",
                    "review_id"?: str  (send it to make retries safe — no duplicates)}
    """
    if request.method != 'POST':
        return _error('Only POST allowed', 405)

    profile = BusinessProfile.objects.filter(webhook_token=token).select_related('user').first()
    if profile is None:
        return _error('Unknown webhook URL', 404)

    if len(request.body) > MAX_BODY_BYTES:
        return _error('Request body too large', 413)

    if ratelimit.too_many(f"webhook:{profile.id}", WEBHOOK_HOURLY_LIMIT, 3600):
        return _error('Too many reviews this hour — try again later', 429)
    ratelimit.hit(f"webhook:{profile.id}", 3600)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error('Invalid JSON body')
    if not isinstance(data, dict):
        return _error('JSON body must be an object')

    rating = parse_rating(data.get('rating'))
    if rating is None or str(data.get('rating')).strip() not in {str(rating), f"{rating}.0"}:
        return _error('rating must be a whole number from 1 to 5')

    try:
        reviewer_name = _text(data, 'reviewer_name', MAX_NAME) or 'Anonymous'
        comment = _text(data, 'comment', MAX_COMMENT)
        business_name = _text(data, 'business_name', MAX_BUSINESS_NAME) or profile.business_name
        review_id = _text(data, 'review_id', MAX_EXTERNAL_ID)
        provided_language = data.get('detected_language')
    except ValueError as e:
        return _error(str(e))
    if not comment:
        return _error('comment is required')

    external_id = f"wh:{review_id}" if review_id else None
    if external_id and Review.objects.filter(user=profile.user, external_id=external_id).exists():
        return JsonResponse({'status': 'duplicate', 'message': 'This review was already received.'}, status=200)

    # Read-only accounts still store the review, but no AI is spent on it.
    can_draft = billing.is_active(profile)

    # Trust a provided language only if we support it; otherwise detect it.
    if provided_language in SUPPORTED_LANGUAGES:
        detected_language = provided_language
    elif can_draft:
        detected_language = detect_review_language(comment)
    else:
        detected_language = 'fr'

    try:
        review = create_review_once(
            user=profile.user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment,
            detected_language=detected_language,
            business_name=business_name,
            source='webhook',
            status='pending',
            external_id=external_id,
        )
        if review is None:
            return JsonResponse({'status': 'duplicate', 'message': 'This review was already received.'}, status=200)

        result = draft_reply(review, profile)
        review.refresh_from_db()
    except Exception:
        # Details go to our logs, never back to the caller.
        logger.exception("Webhook processing failed for business %s", profile.id)
        return _error('Could not process this review — please try again later', 500)

    body = {'status': 'success', 'review_status': review.status, 'review_id': review.id}
    if result.ok:
        body['message'] = f'Review #{review.id} created and AI draft generated.' + (' Reply posted to Google.' if result.posted else '')
        body['ai_draft'] = review.ai_draft_reply
    else:
        body['message'] = f'Review #{review.id} created, but no draft yet: {result.reason}'
    return JsonResponse(body, status=201)
