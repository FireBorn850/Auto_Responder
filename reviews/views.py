from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required  # <--- Protect views
from .models import Review
from .services.ai_responder import generate_review_draft
from .services.google_api import post_reply_to_google

@login_required
def dashboard(request):
    """Lists ONLY reviews that belong to the logged-in business owner."""
    reviews = Review.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'reviews/dashboard.html', {'reviews': reviews})

@login_required
def generate_draft_view(request, review_id):
    """Generates draft only if review belongs to the logged-in user."""
    review = get_object_or_404(Review, id=review_id, user=request.user)
    
    draft_text = generate_review_draft(
        reviewer_name=review.reviewer_name,
        star_rating=review.rating,
        comment=review.comment,
        language=review.detected_language,
        business_name=review.business_name
    )
    
    review.ai_draft_reply = draft_text
    review.save()
    return redirect('dashboard')

@login_required
def approve_review_view(request, review_id):
    """Approves and posts reply only if review belongs to the logged-in user."""
    if request.method == 'POST':
        review = get_object_or_404(Review, id=review_id, user=request.user)
        edited_text = request.POST.get('ai_draft_reply')
        
        review.ai_draft_reply = edited_text
        
        success = post_reply_to_google(review.id, edited_text)
        
        if success:
            review.status = 'posted'
        else:
            review.status = 'approved'
            
        review.save()
        
    return redirect('dashboard')