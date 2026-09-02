import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


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


    PLAN_CHOICES = [
        ('starter', 'Starter'),
        ('premium', 'Premium'),
        ('founding_partner', 'Founding Partner'),
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
    google_maps_url = models.URLField(blank=True, null=True)
    custom_prompt = models.TextField(
        blank=True, null=True,
        help_text="Extra facts/rules fed into the AI prompt (e.g. parking info, signature dishes)."
    )
    blacklisted_words = models.TextField(
        blank=True, null=True,
        help_text="Comma-separated words/phrases the AI must never use in a reply (e.g. competitor names, sensitive terms)."
    )
    seo_keywords = models.TextField(
        blank=True, null=True,
        help_text="Comma-separated local SEO/GEO phrases (e.g. 'terrace near Lake Geneva, authentic fondue Old Town'). The AI weaves one in ONLY when a review naturally supports it — never forced."
    )
    geo_seo_enabled = models.BooleanField(
        default=True,
        help_text="Pro feature toggle for Local SEO Keyword Reinforcement (GEO/AEO). Placeholder for future plan-based gating — currently on for everyone since billing isn't wired up yet."
    )
    action_link_enabled = models.BooleanField(
        default=False,
        help_text="Pro feature: invite happy reviewers to a booking page, event, or offer. Off by default — this is promotional, so it's an explicit opt-in unlike SEO keywords."
    )
    action_link_url = models.URLField(
        blank=True, null=True,
        help_text="Destination link — booking page, event page, online store, etc."
    )
    action_link_label = models.CharField(
        max_length=200, blank=True, null=True,
        help_text="What the link is, in plain words the AI can reference naturally, e.g. 'our autumn wine tasting event', 'table reservations', 'our online store'."
    )
    action_link_min_rating = models.PositiveSmallIntegerField(
        default=4,
        help_text="Only offered on reviews with this star rating or higher — never shown to unhappy customers."
    )
    learned_patterns = models.TextField(
        blank=True, null=True,
        help_text="AI-generated summary of how this owner edits drafts, auto-injected into future prompts."
    )
    last_training_run = models.DateTimeField(blank=True, null=True)

    ai_daily_limit = models.PositiveIntegerField(
        default=50,
        help_text="Max AI generations (drafts + regenerations) allowed per day for this business. Protects against runaway Gemini API costs."
    )
    ai_generations_today = models.PositiveIntegerField(default=0)
    ai_last_generation_date = models.DateField(blank=True, null=True)

    quiet_hours_enabled = models.BooleanField(
        default=False,
        help_text="Hold negative-review alert emails outside business hours and send them at the next opening time instead."
    )
    business_hours_start = models.TimeField(default='09:00')
    business_hours_end = models.TimeField(default='20:00')
    timezone_name = models.CharField(
        max_length=50, default='Europe/Zurich',
        help_text="IANA timezone, e.g. Europe/Zurich, Europe/Paris."
    )

    RESPONSE_LENGTH_CHOICES = [
        ('short', 'Concise (1-2 sentences)'),
        ('medium', 'Balanced (2-4 sentences)'),
        ('long', 'Detailed (4-6 sentences)'),
    ]
    CREATIVITY_CHOICES = [
        ('low', 'Precise & Consistent'),
        ('medium', 'Balanced'),
        ('high', 'Creative & Varied'),
    ]

    response_length = models.CharField(
        max_length=10, choices=RESPONSE_LENGTH_CHOICES, default='medium',
        help_text="Controls how many sentences the AI writes per reply."
    )
    creativity_level = models.CharField(
        max_length=10, choices=CREATIVITY_CHOICES, default='medium',
        help_text="Controls how much wording varies between similar replies (maps to the model's temperature)."
    )
    signature = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Appended to the end of every generated reply."
    )
    logo = models.ImageField(
        upload_to='business_logos/', blank=True, null=True,
        help_text="Embedded in the center of your QR codes when set."
    )

    google_review_url = models.URLField(blank=True, null=True)
    tripadvisor_url = models.URLField(blank=True, null=True)
    webhook_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    google_business_token = models.JSONField(blank=True, null=True)
    google_business_refresh_token = models.TextField(blank=True, null=True)
    google_business_account_id = models.CharField(max_length=255, blank=True, null=True)
    google_business_location_id = models.CharField(max_length=255, blank=True, null=True)
    last_manual_post_url = models.URLField(blank=True, null=True)

    SYNC_FREQUENCY_CHOICES = [
        ('manual', 'Manual only (click Sync Reviews yourself)'),
        ('daily', 'Daily'),
        ('hourly', 'Hourly'),
        ('realtime', 'Real-time (checks every 5 min)'),
    ]
    sync_frequency = models.CharField(
        max_length=10, choices=SYNC_FREQUENCY_CHOICES, default='manual',
        help_text="How often to automatically pull new Google reviews in the background."
    )
    last_auto_sync = models.DateTimeField(blank=True, null=True)
    trustpilot_waitlist_joined_at = models.DateTimeField(
    blank=True, null=True,
    help_text="Set when the owner asks to be notified once Trustpilot integration opens up."
    )

    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='starter')
    plan_expires_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.business_name} ({self.get_automation_mode_display()})"


class AccessCode(models.Model):
    code = models.CharField(max_length=32, unique=True)
    business_name = models.CharField(max_length=255, blank=True, help_text="Who this code was generated for")
    redeemed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='redeemed_access_codes')
    redeemed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    granted_linkedin_recommendation = models.BooleanField(default=False)
    granted_case_study_permission = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} ({self.business_name or 'unassigned'})"

    def is_redeemed(self):
        return self.redeemed_by is not None

    def is_active(self):
        if not self.is_redeemed():
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True


class Review(models.Model):
    LANGUAGE_CHOICES = [
        ('fr', 'French'),
        ('en', 'English'),
        ('de', 'German'),
        ('it', 'Italian'),
        ('gsw', 'Swiss German (Schwiizerdütsch)'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('posted', 'Posted to Google'),
        ('flagged', 'Flagged - Needs Manual Review'),
        ('generation_failed', 'AI Generation Failed'),
    ]

    SOURCE_CHOICES = [
        ('google', 'Google'),
        ('tripadvisor', 'TripAdvisor'),
        ('webhook', 'Custom Webhook'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews', null=True, blank=True)
    business_name = models.CharField(max_length=255, default="Geneva Business")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='google')
    reviewer_name = models.CharField(max_length=255)
    rating = models.IntegerField(help_text="Star rating from 1 to 5")
    comment = models.TextField(help_text="The customer's original review text")

    detected_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='fr')
    sentiment = models.CharField(
        max_length=10, blank=True, null=True,
        choices=[('positive', 'Positive'), ('neutral', 'Neutral'), ('negative', 'Negative')],
        help_text="AI-classified emotional tone of the review, independent of star rating."
    )
    is_likely_spam = models.BooleanField(
        default=False,
        help_text="Flagged by AI as likely fake, bot-generated, or irrelevant content."
    )
    ai_draft_reply = models.TextField(blank=True, null=True, help_text="AI generated draft response")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_simulated = models.BooleanField(default=False, help_text="True if created via Review Simulator, not a real synced review.")
    first_response_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set the moment a reply first became approved or posted — used to compute average response time. Never overwritten after first set, even if the reply is later edited or regenerated."
    )
    seo_keyword_used = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="The local SEO phrase (if any) the AI naturally worked into this reply. Blank means none was used — expected for most replies."
    )
    action_link_shown = models.BooleanField(
        default=False,
        help_text="True if a promotional action link (booking, event, store) was appended to this reply."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.reviewer_name} ({self.rating}★) - {self.status}"


def generate_short_slug():
    return uuid.uuid4().hex[:8]

class SmartQRCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='qr_codes')
    title = models.CharField(max_length=100, help_text="e.g. Table 1 or Front Counter")
    slug = models.SlugField(unique=True, default=generate_short_slug, editable=False)

    google_review_url = models.URLField(help_text="Destination for happy customers (4-5 stars)")
    private_feedback_url = models.URLField(
        blank=True,
        null=True,
        help_text="Internal feedback form for unhappy customers (1-3 stars)"
    )
    fallback_url = models.URLField(help_text="Default URL if no specific rule matches")

    total_scans = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_qr_codes',
        help_text="Teammate responsible for this campaign."
    )
    active_hours_enabled = models.BooleanField(
        default=False,
        help_text="If on, this code only routes customers during the hours below — outside that window it shows a closed notice instead."
    )
    active_hours_start = models.TimeField(null=True, blank=True)
    active_hours_end = models.TimeField(null=True, blank=True)
    expires_at = models.DateField(
        null=True, blank=True,
        help_text="After this date, the code stops routing customers — useful for time-limited promotions."
    )

    def is_currently_active(self):
        from django.utils import timezone
        from zoneinfo import ZoneInfo
        tz_name = getattr(self.user.profile, 'timezone_name', None) or 'Europe/Zurich'
        now = timezone.now().astimezone(ZoneInfo(tz_name))
        if self.expires_at and now.date() > self.expires_at:
            return False
        if self.active_hours_enabled and self.active_hours_start and self.active_hours_end:
            current = now.time()
            if self.active_hours_start <= self.active_hours_end:
                if not (self.active_hours_start <= current <= self.active_hours_end):
                    return False
            else:  # window crosses midnight
                if not (current >= self.active_hours_start or current <= self.active_hours_end):
                    return False
        return True

    def __str__(self):
        return f"{self.title} ({self.total_scans} scans)"

    
class QRScanEvent(models.Model):
    DEVICE_CHOICES = [
        ('mobile', 'Mobile'),
        ('tablet', 'Tablet'),
        ('desktop', 'Desktop'),
        ('other', 'Other'),
    ]
    qr_code = models.ForeignKey(SmartQRCode, on_delete=models.CASCADE, related_name='scan_events')
    scanned_at = models.DateTimeField(auto_now_add=True)
    device_type = models.CharField(max_length=10, choices=DEVICE_CHOICES, default='other')
    resulted_in_rating = models.IntegerField(null=True, blank=True, help_text="Star tapped on the Smart Rating Gate, if any.")

    class Meta:
        ordering = ['-scanned_at']

    def __str__(self):
        return f"Scan of {self.qr_code.title} at {self.scanned_at:%Y-%m-%d %H:%M}"


class Competitor(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='competitors')
    name = models.CharField(max_length=255, help_text="e.g. Cafe De Paris")
    location = models.CharField(max_length=255, default="Geneva", help_text="City or neighborhood")
    avg_rating = models.FloatField(default=4.0, help_text="Rating out of 5.0")
    total_reviews = models.IntegerField(default=0, help_text="Total review count")
    google_maps_url = models.URLField(blank=True, null=True, help_text="Optional link to Google Maps entry")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.avg_rating}★ - {self.total_reviews} reviews)"


class TeamInvite(models.Model):
    ROLE_CHOICES = [
        ('reviewer', 'Review Approver'),
        ('viewer', 'Read-Only Analyst'),
        ('admin', 'Store Admin'),
    ]
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='team_invites')
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='reviewer')
    linked_user = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='team_membership',
        help_text="Set automatically once someone signs up with this invite's email."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"



class UserSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tracked_sessions')
    session_key = models.CharField(max_length=40, unique=True)
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_activity']

    def __str__(self):
        return f"{self.user.username} session ({self.session_key[:8]}…)"



class EditLog(models.Model):
    """Captures the diff between an AI draft and the human-edited final text —
    the raw training signal for the AI Training feature."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='edit_logs')
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name='edit_logs')
    ai_draft = models.TextField()
    final_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Edit log for review #{self.review_id} ({self.created_at:%Y-%m-%d})"


class ActivityLog(models.Model):
    ACTION_CHOICES = [
        ('settings_updated', 'Updated AI Settings'),
        ('review_approved', 'Approved a Review Reply'),
        ('team_invite_sent', 'Sent Team Invite'),
        ('team_invite_revoked', 'Revoked Team Invite'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activity_logs')
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    detail = models.CharField(max_length=255, blank=True, help_text="Short human-readable summary, e.g. review name or setting changed.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.get_action_display()} - {self.created_at}"


class SyncLog(models.Model):
    PLATFORM_CHOICES = [
        ('google', 'Google Business Profile'),
        ('tripadvisor', 'TripAdvisor'),
    ]
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('rate_limited', 'Rate Limited'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sync_logs')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES)
    detail = models.CharField(max_length=255, blank=True, help_text="e.g. '3 new reviews imported' or the error message.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_platform_display()} sync ({self.status}) - {self.created_at:%Y-%m-%d %H:%M}"