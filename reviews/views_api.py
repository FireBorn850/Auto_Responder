import json
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from .models import Review, BusinessProfile
from .services.ai_responder import generate_review_draft, is_authentic_review, QuotaExceededError, detect_review_language, SUPPORTED_LANGUAGES
from .permissions import check_ai_quota


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
        rating = data.get('rating', 5)
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

        # Authenticity check
        if not is_authentic_review(comment):
            review.status = 'flagged'
            review.save()
            return JsonResponse({
                'status': 'success',
                'message': f'Review #{review.id} created but flagged — content did not pass the authenticity check.',
                'review_status': review.status,
            }, status=201)

        # Quota check — same daily cap enforced everywhere else
        if not check_ai_quota(profile):
            review.status = 'flagged'
            review.save()
            return JsonResponse({
                'status': 'success',
                'message': f'Review #{review.id} created but flagged — daily AI generation limit reached ({profile.ai_daily_limit}/day).',
                'review_status': review.status,
            }, status=201)

        # Generate the AI draft directly (same as dashboard) — pass the
        # same settings every other call site uses, so webhook-created
        # reviews respect the owner's length/creativity/blacklist/training.
        try:
            draft_text = generate_review_draft(
                reviewer_name=review.reviewer_name,
                star_rating=review.rating,
                comment=review.comment,
                language=review.detected_language,
                business_name=review.business_name,
                tone=profile.brand_tone,
                custom_prompt=profile.custom_prompt or '',
                signature=profile.signature or '',
                response_length=profile.response_length,
                creativity=profile.creativity_level,
                blacklisted_words=profile.blacklisted_words or '',
                learned_patterns=profile.learned_patterns or '',
            )
        except QuotaExceededError:
            review.status = 'generation_failed'
            review.save()
            return JsonResponse({
                'status': 'success',
                'message': f"Review #{review.id} created, but Gemini's daily free-tier quota is exhausted — it will need a manual retry from the dashboard.",
                'review_status': review.status,
            }, status=201)

        # GUARD: Failed generation
        if not draft_text or draft_text.strip() == "":
            review.status = 'generation_failed'
            review.ai_draft_reply = ''
            review.save()
            return JsonResponse({
                'status': 'success',
                'message': f'Review #{review.id} created, but AI draft generation failed — it will need a manual retry from the dashboard.',
                'review_status': review.status,
            }, status=201)

        # SUCCESS
        review.ai_draft_reply = draft_text

        # Auto-pilot routing
        mode = profile.automation_mode
        if mode == 'all':
            review.status = 'approved'
        elif mode == 'positive_only' and review.rating >= 4:
            review.status = 'approved'
        else:
            review.status = 'pending'

        review.save()

        return JsonResponse({
            'status': 'success',
            'message': f'Review #{review.id} created and AI draft generated.',
            'review_status': review.status,
            'ai_draft': review.ai_draft_reply,
        }, status=201)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)