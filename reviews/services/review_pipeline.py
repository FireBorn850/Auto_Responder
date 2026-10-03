"""
One place that turns a review into a reply.

Used by: the dashboard "Generate" button, the Review Simulator, the webhook,
and the automatic drafting that runs after every sync. Before this module
existed the same steps were copy-pasted in four places and had drifted apart.

Steps:
  1. gibberish check          -> flagged
  2. app daily AI quota       -> stop (nothing changes)
  3. Gemini sentiment + spam  -> flagged if spam
  4. Gemini draft             -> generation_failed if it fails
  5. route by automation mode -> pending / approved
  6. auto-post to Google      -> posted (only when it is safe, see can_auto_post)
"""
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from reviews.models import Review
from reviews.permissions import check_ai_quota
from reviews.services import billing, gbp_client
from reviews.services.ai_responder import (
    QuotaExceededError,
    analyze_review_sentiment,
    append_action_link,
    detect_seo_keyword_used,
    generate_review_draft,
    is_authentic_review,
)

logger = logging.getLogger(__name__)

AUTO_POST_MIN_RATING = 4  # 1-3★ replies never post without a human, in any mode


@dataclass
class DraftResult:
    ok: bool
    code: str            # drafted | not_authentic | quota | spam | gemini_quota | failed
    reason: str = ''
    posted: bool = False


def effective_automation_mode(profile):
    """Starter (and read-only) accounts get manual approval, whatever was saved."""
    return profile.automation_mode if billing.can(profile, 'auto_post') else 'manual'


def route_status(review, profile):
    """Which status a freshly drafted reply gets, based on the owner's automation mode."""
    mode = effective_automation_mode(profile)
    happy = review.rating >= AUTO_POST_MIN_RATING and review.sentiment != 'negative'
    if mode == 'all':
        return 'approved'                     # 1-3★ pre-approved, but still never auto-posted
    if mode == 'positive_only' and happy:
        return 'approved'
    return 'pending'


def can_auto_post(review, profile):
    """
    Auto-posting is deliberately strict. A reply is published without a human
    only when ALL of these hold:
      - the owner chose an automatic mode (Smart Guardrail or Hands-Free)
      - the review is 4-5★ and its text isn't actually negative (sarcasm guard)
      - it isn't spam and isn't a simulator test review
      - Google Business Profile is connected and this review came from it
    """
    return bool(
        review.ai_draft_reply
        and review.status == 'approved'
        and effective_automation_mode(profile) in ('positive_only', 'all')
        and review.rating >= AUTO_POST_MIN_RATING
        and review.sentiment != 'negative'
        and not review.is_likely_spam
        and not review.is_simulated
        and profile.gbp_connected
        and (review.external_id or '').startswith('gbp:')
    )


def _mark_responded(review):
    if review.first_response_at is None:
        review.first_response_at = timezone.now()


def try_auto_post(review, profile):
    """Publishes the reply to Google if allowed. Returns True if it went live."""
    if not can_auto_post(review, profile):
        return False
    try:
        gbp_client.post_reply(profile, review.external_id[4:], review.ai_draft_reply.strip())
    except gbp_client.GBPError as e:
        logger.warning("Auto-post failed for review %s: %s", review.id, e)
        return False
    review.status = 'posted'
    _mark_responded(review)
    review.save(update_fields=['status', 'first_response_at', 'updated_at'])
    return True


def draft_reply(review, profile, *, force=False, is_regeneration=False, auto_post=True):
    """Runs the full pipeline on one review. Always leaves the review saved."""
    if not billing.is_active(profile):
        # Read-only accounts cost nothing: no Gemini calls at all.
        return DraftResult(False, 'no_plan', billing.READ_ONLY_MESSAGE)
    if not force and not is_authentic_review(review.comment):
        review.status = 'flagged'
        review.ai_draft_reply = ''
        review.save()
        return DraftResult(False, 'not_authentic', "Skipped — this doesn't look like a genuine review (failed authenticity check).")

    if not check_ai_quota(profile):
        return DraftResult(False, 'quota', f"Daily AI generation limit reached ({profile.ai_daily_limit}/day) — try again tomorrow.")

    analysis = analyze_review_sentiment(review.comment, review.rating)
    review.sentiment = analysis['sentiment']
    review.is_likely_spam = analysis['is_likely_spam']

    if not force and review.is_likely_spam:
        review.status = 'flagged'
        review.save()
        return DraftResult(False, 'spam', "Skipped — flagged as likely spam.")

    active_seo_keywords = profile.seo_keywords if (profile.geo_seo_enabled and profile.seo_keywords) else ''
    offer_qualifies = bool(
        profile.action_link_enabled and profile.action_link_url and profile.action_link_label
        and review.rating >= profile.action_link_min_rating
    )

    try:
        draft_text = generate_review_draft(
            reviewer_name=review.reviewer_name,
            star_rating=review.rating,
            comment=review.comment,
            language=review.detected_language,
            business_name=profile.business_name,
            tone=profile.brand_tone,
            custom_prompt=profile.custom_prompt or '',
            signature=profile.signature or '',
            response_length=profile.response_length,
            creativity=profile.creativity_level,
            blacklisted_words=profile.blacklisted_words or '',
            learned_patterns=profile.learned_patterns or '',
            seo_keywords=active_seo_keywords,
            action_offer_label=profile.action_link_label if offer_qualifies else '',
            is_regeneration=is_regeneration,
            contact_email=profile.user.email,
        )
    except QuotaExceededError:
        review.save()
        return DraftResult(False, 'gemini_quota', "Gemini's daily quota is exhausted for now — try again later.")

    if not draft_text or not draft_text.strip():
        review.status = 'generation_failed'
        review.ai_draft_reply = ''
        review.save()
        return DraftResult(False, 'failed', "AI draft generation failed — please try again.")

    if offer_qualifies:
        draft_text = append_action_link(draft_text, profile.action_link_url, profile.action_link_label)
        review.action_link_shown = True

    review.ai_draft_reply = draft_text
    review.seo_keyword_used = detect_seo_keyword_used(draft_text, active_seo_keywords)
    review.status = route_status(review, profile)
    if review.status == 'approved':
        _mark_responded(review)
    review.save()

    posted = try_auto_post(review, profile) if auto_post else False
    return DraftResult(True, 'drafted', posted=posted)


def auto_draft_new_reviews(review_ids):
    """
    Called by the importers after a sync. Drafts only the newest few new
    reviews (AUTO_DRAFT_MAX_PER_SYNC, default 5) so a big first sync can't
    burn the whole Gemini quota or time out; older ones keep the Generate
    button. Each one runs as a Celery task so it moves to the background as
    soon as a real worker is configured.
    """
    if not review_ids:
        return 0
    from reviews.tasks import auto_draft_review

    limit = getattr(settings, 'AUTO_DRAFT_MAX_PER_SYNC', 5)
    newest = (
        Review.objects.filter(id__in=review_ids, status='pending', is_simulated=False)
        .filter(Q(ai_draft_reply__isnull=True) | Q(ai_draft_reply=''))
        .order_by('-id').values_list('id', flat=True)[:limit]
    )

    count = 0
    for rid in newest:
        auto_draft_review.delay(rid)
        count += 1
    return count
