from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.db.models import Avg, F
from .models import Review, BusinessProfile, SmartQRCode, Competitor
from .services.ai_responder import generate_review_draft, analyze_complaints, is_authentic_review
from .services.google_api import post_reply_to_google
from .services.google_importer import fetch_live_google_reviews

# ==========================================
# 1. PUBLIC VIEWS
# ==========================================

def landing_page(request):
    """Public SaaS homepage introducing bilingual AI review management."""
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'reviews/landing.html')


# ==========================================
# 2. DEDICATED DASHBOARD & PAGE VIEWS
# ==========================================

@login_required
def dashboard(request):
    """Page 1: Main Dashboard & Live Customer Reviews Stream."""
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)
    reviews = Review.objects.filter(user=request.user).order_by('-created_at')

    now = timezone.now()
    first_day_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_reviews_this_month = reviews.filter(created_at__gte=first_day_of_month).count()

    avg_rating_result = reviews.aggregate(Avg('rating'))['rating__avg']
    avg_rating = round(avg_rating_result, 1) if avg_rating_result else 0.0

    total_reviews_handled = reviews.count()
    saved_hours = round(total_reviews_handled * 0.1, 1)

    analytics = {
        'total_reviews_this_month': total_reviews_this_month,
        'avg_rating': avg_rating,
        'saved_hours': saved_hours,
        'total_reviews_handled': total_reviews_handled,
    }

    context = {
        'reviews': reviews,
        'profile': profile,
        'analytics': analytics,
        'active_tab': 'dashboard',
    }
    return render(request, 'reviews/dashboard.html', context)


@login_required
def settings_page_view(request):
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)
    return render(request, 'reviews/settings.html', {'profile': profile, 'active_tab': 'ai_settings'})


@login_required
def qr_booster_page_view(request):
    qr_codes = SmartQRCode.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'reviews/qr_booster.html', {'qr_codes': qr_codes, 'active_tab': 'qr_booster'})


@login_required
def integrations_page_view(request):
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)
    return render(request, 'reviews/integrations.html', {'profile': profile, 'active_tab': 'integrations'})


@login_required
def competitors_page_view(request):
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)
    competitors = Competitor.objects.filter(user=request.user).order_by('-avg_rating')

    context = {
        'profile': profile,
        'competitors': competitors,
        'active_tab': 'competitors',
    }
    return render(request, 'reviews/competitors.html', context)


@login_required
def simulator_page_view(request):
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)
    return render(request, 'reviews/simulator.html', {'profile': profile, 'active_tab': 'simulator'})


# ==========================================
# 3. ACTION & FORM HANDLERS
# ==========================================

@login_required
def sync_google_reviews_view(request):
    if request.method == 'POST':
        business_name = request.POST.get('business_name', 'Geneva Bistro')
        place_id = request.POST.get('place_id', '').strip()

        imported_count = fetch_live_google_reviews(
            place_id=place_id,
            user=request.user,
            business_name=business_name
        )

        if imported_count > 0:
            messages.success(request, f"Imported {imported_count} new review{'s' if imported_count != 1 else ''} for {business_name}.")
        else:
            messages.info(request, f"You're all caught up — no new reviews found for {business_name}.")

    return redirect('dashboard')


@login_required
def update_settings_view(request):
    if request.method == 'POST':
        profile, created = BusinessProfile.objects.get_or_create(user=request.user)
        automation_mode = request.POST.get('automation_mode')
        brand_tone = request.POST.get('brand_tone')
        custom_prompt = request.POST.get('custom_prompt', '')
        signature = request.POST.get('signature', '')

        if automation_mode in ['positive_only', 'all', 'manual']:
            profile.automation_mode = automation_mode

        if brand_tone in ['friendly', 'professional', 'casual']:
            profile.brand_tone = brand_tone

        if hasattr(profile, 'custom_prompt'):
            profile.custom_prompt = custom_prompt
        if hasattr(profile, 'signature'):
            profile.signature = signature

        profile.save()

    return redirect('ai_settings')


@login_required
def generate_draft_view(request, review_id):
    """
    Generates AI review draft and routes status based on Auto-Pilot settings.

    Reviews that fail the authenticity check (gibberish/spam) are flagged
    for manual handling instead of getting a confident AI-written reply,
    unless ?force=1 is passed (an explicit "write anyway" override from
    the dashboard).
    """
    review = get_object_or_404(Review, id=review_id, user=request.user)
    profile, created = BusinessProfile.objects.get_or_create(user=request.user)

    force = request.GET.get('force') == '1'

    if not force and not is_authentic_review(review.comment):
        review.status = 'flagged'
        review.ai_draft_reply = ''
        review.save()
        return redirect('dashboard')

    draft_text = generate_review_draft(
        reviewer_name=review.reviewer_name,
        star_rating=review.rating,
        comment=review.comment,
        language=review.detected_language,
        business_name=review.business_name,
        tone=profile.brand_tone
    )

    review.ai_draft_reply = draft_text

    mode = profile.automation_mode
    if mode == 'all':
        review.status = 'approved'
    elif mode == 'positive_only' and review.rating >= 4:
        review.status = 'approved'
    else:
        review.status = 'pending'

    review.save()
    return redirect('dashboard')


@login_required
def approve_review_view(request, review_id):
    if request.method == 'POST':
        review = get_object_or_404(Review, id=review_id, user=request.user)
        edited_text = request.POST.get('ai_draft_reply')

        review.ai_draft_reply = edited_text
        success = post_reply_to_google(review.id, edited_text)

        if success:
            review.status = 'posted'
            messages.success(request, f"Reply to {review.reviewer_name} was posted to Google.")
        else:
            review.status = 'approved'
            messages.warning(request, f"Reply to {review.reviewer_name} was approved but couldn't be posted automatically yet — you can post it manually via Open Google Business.")

        review.save()

    return redirect('dashboard')


@login_required
def add_review_view(request):
    """Processes manual review simulation submission and redirects to dashboard."""
    if request.method == 'POST':
        reviewer_name = request.POST.get('reviewer_name', 'Anonymous')
        rating = int(request.POST.get('rating', 5))
        comment = request.POST.get('comment', '')
        language = request.POST.get('language', 'fr')
        business_name = request.POST.get('business_name', 'Geneva Bistro')

        profile, created = BusinessProfile.objects.get_or_create(user=request.user)

        review = Review.objects.create(
            user=request.user,
            reviewer_name=reviewer_name,
            rating=rating,
            comment=comment,
            detected_language=language,
            business_name=business_name,
            status='pending'
        )

        if not is_authentic_review(comment):
            review.status = 'flagged'
            review.save()
            return redirect('dashboard')

        draft_text = generate_review_draft(
            reviewer_name=reviewer_name,
            star_rating=rating,
            comment=comment,
            language=language,
            business_name=business_name,
            tone=profile.brand_tone
        )
        review.ai_draft_reply = draft_text

        mode = profile.automation_mode
        if mode == 'all':
            review.status = 'approved'
        elif mode == 'positive_only' and rating >= 4:
            review.status = 'approved'
        else:
            review.status = 'pending'

        review.save()

    return redirect('dashboard')


# ==========================================
# 4. COMPETITOR & TEAM ACTIONS
# ==========================================

@login_required
def add_competitor_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        location = request.POST.get('location', 'Geneva')
        avg_rating = float(request.POST.get('avg_rating', 4.0))
        total_reviews = int(request.POST.get('total_reviews', 0))
        google_maps_url = request.POST.get('google_maps_url', '')

        if name:
            Competitor.objects.create(
                user=request.user,
                name=name,
                location=location,
                avg_rating=avg_rating,
                total_reviews=total_reviews,
                google_maps_url=google_maps_url
            )

    return redirect('competitors')


@login_required
def delete_competitor_view(request, competitor_id):
    competitor = get_object_or_404(Competitor, id=competitor_id, user=request.user)
    if request.method == 'POST':
        competitor.delete()
    return redirect('competitors')


# ==========================================
# 5. SMART QR CODE ACTIONS & ROUTER
# ==========================================

def qr_redirect_view(request, slug):
    qr = get_object_or_404(SmartQRCode, slug=slug)

    SmartQRCode.objects.filter(pk=qr.pk).update(total_scans=F('total_scans') + 1)

    rating_param = request.GET.get('rating')
    if rating_param:
        try:
            rating = int(rating_param)
            if rating >= 4 and qr.google_review_url:
                return redirect(qr.google_review_url)
            elif rating < 4 and qr.private_feedback_url:
                return redirect(qr.private_feedback_url)
        except ValueError:
            pass

    return redirect(qr.fallback_url or qr.google_review_url)


@login_required
def create_qr_view(request):
    if request.method == 'POST':
        title = request.POST.get('name', 'Main QR Code')
        google_review_url = request.POST.get('target_url')
        slug_input = request.POST.get('slug')

        if google_review_url:
            qr = SmartQRCode(
                user=request.user,
                title=title,
                google_review_url=google_review_url,
                fallback_url=google_review_url
            )

            if slug_input and slug_input.strip():
                qr.slug = slug_input.strip()

            qr.save()

    return redirect('qr_booster')