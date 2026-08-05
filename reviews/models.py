import uuid
from django.db import models
from django.contrib.auth.models import User


class BusinessProfile(models.Model):
    """Stores business owner settings like Auto-Pilot mode and Brand Voice."""
    AUTOMATION_CHOICES = [
        ('positive_only', 'Auto-publish 4-5 stars only (Approval for 1-3 stars)'),
        ('all', 'Auto-publish all responses'),
        ('manual', 'Manual approval for all responses'),
    ]

    TONE_CHOICES = [
        ('friendly', '😊 Friendly & Warm (Best for Cafes, Restaurants)'),
        ('professional', '💼 Professional & Formal (Best for Clinics, Law/Consulting)'),
        ('casual', '⚡ Casual & Playful (Best for Fitness, Trendy Shops)'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    business_name = models.CharField(max_length=255, default="My Business")
    automation_mode = models.CharField(
        max_length=20,
        choices=AUTOMATION_CHOICES,
        default='positive_only'
    )
    brand_tone = models.CharField(
        max_length=20,
        choices=TONE_CHOICES,
        default='friendly'
    )

    def __str__(self):
        return f"{self.business_name} ({self.get_automation_mode_display()})"


class Review(models.Model):
    LANGUAGE_CHOICES = [
        ('fr', 'French'),
        ('en', 'English'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('posted', 'Posted to Google'),
        ('flagged', 'Flagged - Needs Manual Review'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews', null=True, blank=True)
    business_name = models.CharField(max_length=255, default="Geneva Business")
    reviewer_name = models.CharField(max_length=255)
    rating = models.IntegerField(help_text="Star rating from 1 to 5")
    comment = models.TextField(help_text="The customer's original review text")

    detected_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='fr')
    ai_draft_reply = models.TextField(blank=True, null=True, help_text="AI generated draft response")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.reviewer_name} ({self.rating}★) - {self.status}"

    def generate_ai_reply(self):
        """Generates a reply draft using Gemini and saves it to the model."""
        from .services.ai_responder import generate_review_draft

        tone = 'friendly'
        if self.user and hasattr(self.user, 'profile'):
            tone = self.user.profile.brand_tone

        reply = generate_review_draft(
            reviewer_name=self.reviewer_name,
            star_rating=self.rating,
            comment=self.comment,
            language=self.detected_language,
            business_name=self.business_name,
            tone=tone
        )
        self.ai_draft_reply = reply
        self.save()
        return reply


class SmartQRCode(models.Model):
    """Stores smart QR code settings, redirection links, and scan analytics."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='qr_codes')
    title = models.CharField(max_length=100, help_text="e.g. Table 1 or Front Counter")
    slug = models.SlugField(unique=True, default=uuid.uuid4, editable=False)

    google_review_url = models.URLField(help_text="Destination for happy customers (4-5 stars)")
    private_feedback_url = models.URLField(
        blank=True,
        null=True,
        help_text="Internal feedback form for unhappy customers (1-3 stars)"
    )
    fallback_url = models.URLField(help_text="Default URL if no specific rule matches")

    total_scans = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.total_scans} scans)"


class Competitor(models.Model):
    """Stores local business competitors to track and compare ratings side-by-side."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='competitors')
    name = models.CharField(max_length=255, help_text="e.g. Cafe De Paris")
    location = models.CharField(max_length=255, default="Geneva", help_text="City or neighborhood")
    avg_rating = models.FloatField(default=4.0, help_text="Rating out of 5.0")
    total_reviews = models.IntegerField(default=0, help_text="Total review count")
    google_maps_url = models.URLField(blank=True, null=True, help_text="Optional link to Google Maps entry")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.avg_rating}★ - {self.total_reviews} reviews)"