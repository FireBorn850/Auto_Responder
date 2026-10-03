import json
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from .models import Review, BusinessProfile
from .services.ai_responder import detect_review_language, SUPPORTED_LANGUAGES
from .services.review_pipeline import draft_reply


@csrf_exempt
def google_review_webhook(request, token):
    """
    Receives a third-party review as JSON and creates it for whichever
    business owns `token` (BusinessProfile.webhook_token).
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Only POST allowed'}, status=405)

    profile = get_object_or_404(BusinessProfile, webhook_token=token)
    owner = profile.user

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    try:
        reviewer_name = data.get('reviewer_name', 'Anonymous')
        try:
            rating = int(data.get('rating'))
        except (TypeError, ValueError):
            return JsonResponse({'status': 'error', 'message': 'rating must be a whole number from 1 to 5'}, status=400)
        if not 1 <= rating <= 5:
            return JsonResponse({'status': 'error', 'message': 'rating must be a whole number from 1 to 5'}, status=400)
        comment = data.get('comment', '')
        business_name = data.get('business_name') or profile.business_name

        # Third-party integrations may send a language code (if their
        # platform already knows it), send an unsupported one, or send
        # nothing at all. Trust it only if it's one we actually support;
        # otherwise run real detection on the comment text rather than
        # silently defaulting to French and drafting in the wrong language.
        provided_language = data.get('detected_language')
        if provided_language in SUPPORTED_LANGUAGES:
            detected_language = provided_language
        else:
            detected_language = detect_review_language(comment)

        review = Review.objects.create(
            user=owner,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment,
            detected_language=detected_language,
            business_name=business_name,
            source='webhook',
            status='pending'
        )

        result = draft_reply(review, profile)
        review.refresh_from_db()

        body = {
            'status': 'success',
            'review_status': review.status,
        }
        if result.ok:
            body['message'] = f'Review #{review.id} created and AI draft generated.' + (' Reply posted to Google.' if result.posted else '')
            body['ai_draft'] = review.ai_draft_reply
        else:
            body['message'] = f'Review #{review.id} created, but no draft yet: {result.reason}'
        return JsonResponse(body, status=201)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)