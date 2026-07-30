import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Review

@csrf_exempt
def google_review_webhook(request):
    """
    Simulates receiving a webhook push notification when a new 
    review is posted on Google Business Profile.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            
            # Extract review details from JSON body
            review = Review.objects.create(
                reviewer_name=data.get('reviewer_name', 'Anonymous'),
                rating=data.get('rating', 5),
                comment=data.get('comment', ''),
                detected_language=data.get('detected_language', 'fr'),
                business_name=data.get('business_name', 'Geneva Local Business'),
                status='pending'
            )
            
            # Automatically trigger Gemini AI draft generation upon creation
            review.generate_ai_reply()
            
            return JsonResponse({
                'status': 'success',
                'message': f'Review #{review.id} created and AI draft generated automatically!',
                'ai_draft': review.ai_draft_reply
            }, status=201)
            
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
            
    return JsonResponse({'status': 'error', 'message': 'Only POST allowed'}, status=405)