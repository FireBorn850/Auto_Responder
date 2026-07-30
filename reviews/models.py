from django.db import models
from django.contrib.auth.models import User  # <--- Added User import
from .services.ai_responder import generate_review_draft

class Review(models.Model):
    LANGUAGE_CHOICES = [
        ('fr', 'French'),
        ('en', 'English'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('posted', 'Posted to Google'),
    ]

    # Link each review to a specific business owner account
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews', null=True, blank=True)

    # Business & Customer info
    business_name = models.CharField(max_length=255, default="Geneva Business")
    reviewer_name = models.CharField(max_length=255)
    rating = models.IntegerField(help_text="Star rating from 1 to 5")
    comment = models.TextField(help_text="The customer's original review text")
    
    # AI Response processing
    detected_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='fr')
    ai_draft_reply = models.TextField(blank=True, null=True, help_text="AI generated draft response")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.reviewer_name} ({self.rating}★) - {self.status}"

    def generate_ai_reply(self):
        """Generates a reply draft using Gemini and saves it to the model."""
        reply = generate_review_draft(
            reviewer_name=self.reviewer_name,
            star_rating=self.rating,
            comment=self.comment,
            language=self.detected_language,
            business_name=self.business_name
        )
        self.ai_draft_reply = reply
        self.save()
        return reply