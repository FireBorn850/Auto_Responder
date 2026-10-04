"""
Who may use what — the single source of truth for plans.

Plans (matches the pricing page):
  starter          CHF 19/mo, or CHF 149/yr (≈12.42/mo) — sync, AI drafts, 4–5★ auto-post (Smart Guardrail)
  premium          CHF 39/mo                              — everything (see PREMIUM_FEATURES)
  founding_partner free for 1 month via access code — Premium

Access levels:
  trial      first 14 days after sign-up, no card       -> Premium
  founding   redeemed Founding Partner code, 30 days    -> Premium
  paid       active Polar subscription                  -> its plan
  grace      payment failed, < 7 days ago               -> its plan, with a warning
  read_only  nothing above: reviews stay visible and can be copied/approved,
             but nothing that costs money runs (sync, AI, auto-post, alerts)
"""
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

TRIAL_DAYS = 14
GRACE_DAYS = 7
FOUNDING_DAYS = 30

# Not listed here = included in every active plan (Starter too), e.g.
# 'auto_post': Smart Guardrail, 4–5★ replies post to Google by themselves.
PREMIUM_FEATURES = {
    'hands_free',    # Hands-Free mode: 1–3★ drafts also come pre-approved (still never auto-posted)
    'tripadvisor',
    'qr',            # QR Code Booster + Smart Rating Gate
    'competitors',
    'insights',      # AI complaint trend analysis
    'team',          # team seats & staff access
    'alerts',        # instant negative-review alerts
}

PRICES = {  # shown in the app; the real charge is configured in Polar
    ('starter', 'month'): 19,
    ('starter', 'year'): 149,   # the only yearly option: ~35% cheaper than monthly
    ('premium', 'month'): 39,
}

PAID_STATUSES = ('active', 'trialing')


@dataclass
class Access:
    level: str            # trial | founding | paid | grace | read_only
    plan: str             # starter | premium | none
    ends_at: object = None

    @property
    def active(self):
        return self.level != 'read_only'

    @property
    def is_premium(self):
        return self.active and self.plan == 'premium'

    @property
    def days_left(self):
        if not self.ends_at:
            return None
        return max(0, (self.ends_at - timezone.now()).days)

    def can(self, feature):
        if not self.active:
            return False
        if feature in PREMIUM_FEATURES:
            return self.plan == 'premium'
        return True


def trial_end_for_new_profile():
    return timezone.now() + timedelta(days=TRIAL_DAYS)


def get_access(profile, now=None):
    if profile is None:
        return Access('read_only', 'none')
    now = now or timezone.now()

    status = profile.subscription_status
    paid_plan = profile.plan if profile.plan in ('starter', 'premium') else None

    if paid_plan and status in PAID_STATUSES:
        return Access('paid', paid_plan, profile.current_period_end)
    if paid_plan and status == 'canceled' and profile.current_period_end and profile.current_period_end > now:
        # Cancelled, but the month/year they paid for isn't over yet.
        return Access('paid', paid_plan, profile.current_period_end)
    if paid_plan and status == 'past_due':
        since = profile.past_due_since or now
        if now - since < timedelta(days=GRACE_DAYS):
            return Access('grace', paid_plan, since + timedelta(days=GRACE_DAYS))

    if profile.plan == 'founding_partner' and profile.plan_expires_at and profile.plan_expires_at > now:
        return Access('founding', 'premium', profile.plan_expires_at)

    if profile.trial_ends_at and profile.trial_ends_at > now:
        return Access('trial', 'premium', profile.trial_ends_at)

    return Access('read_only', 'none')


def can(profile, feature):
    return get_access(profile).can(feature)


def is_active(profile):
    return get_access(profile).active


READ_ONLY_MESSAGE = "Your plan has ended — choose a plan on the Billing page to keep syncing and drafting replies."
PREMIUM_MESSAGE = "This is a Premium feature — upgrade on the Billing page to unlock it."


def denial_message(profile, feature=None):
    access = get_access(profile)
    if not access.active:
        return READ_ONLY_MESSAGE
    return PREMIUM_MESSAGE
