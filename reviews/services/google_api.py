import time
import logging
from urllib.parse import quote
from django.urls import reverse

logger = logging.getLogger(__name__)

def post_reply_to_google(review_id, reply_text, user=None):
    """
    Attempts to post reply to Google via API if available,
    otherwise provides manual posting instructions.
    
    Returns: (success, google_url) tuple
    """
    try:
        from reviews.models import Review, BusinessProfile
        review = Review.objects.get(id=review_id)
        profile = BusinessProfile.objects.get(user=review.user)
        
        # Check if user has Google Business API credentials
        if hasattr(profile, 'google_business_token') and profile.google_business_token:
            # REAL API POST - For future implementation
            return _post_via_google_api(review_id, reply_text, profile)
        else:
            # Manual posting flow - opens Google Maps
            return _manual_post_flow(review_id, reply_text, profile)
            
    except Review.DoesNotExist:
        logger.error(f"Review {review_id} not found")
        return False, None
    except BusinessProfile.DoesNotExist:
        logger.error(f"Business profile not found for user")
        return False, None
    except Exception as e:
        logger.error(f"Error in post_reply_to_google: {str(e)}")
        return False, None

def _manual_post_flow(review_id, reply_text, profile):
    """
    Generates a Google Maps URL for manual posting. Nothing is actually
    submitted to Google here — this just hands the caller a link so the
    business owner can open Google Business and paste the reply in
    themselves. success is always False; only _post_via_google_api
    (once implemented) represents a genuine automated post.
    """
    google_url = profile.google_maps_url

    if google_url:
        return False, google_url
    else:
        logger.warning(f"No Google Maps URL found for review {review_id}")
        return False, None

def _post_via_google_api(review_id, reply_text, profile):
    """
    Placeholder for future Google Business API integration.
    """
    logger.info("Google Business API posting not yet implemented")
    return False, None