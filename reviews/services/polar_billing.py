"""
Polar (polar.sh) — the Merchant of Record that takes payments.

Polar is the legal seller: it charges the card, handles VAT/sales tax and
invoices, and pays us out. This module only:
  1. creates a checkout link for a plan            (create_checkout_url)
  2. opens Polar's customer portal                  (customer_portal_url)
     where customers change card / plan / cancel
  3. verifies and applies webhooks                  (verify_webhook, apply_event)

Everything provider-specific lives here, so switching to Paddle later means
replacing this one file and the env settings.

Settings (environment variables):
  POLAR_SERVER                 'sandbox' (testing) or 'production'
  POLAR_ACCESS_TOKEN           Organization Access Token
  POLAR_WEBHOOK_SECRET         whsec_... from the webhook endpoint
  POLAR_PRODUCT_STARTER_MONTHLY, POLAR_PRODUCT_STARTER_YEARLY, POLAR_PRODUCT_PREMIUM_MONTHLY
"""
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from reviews.models import BusinessProfile

logger = logging.getLogger(__name__)

TIMEOUT = 20
WEBHOOK_TOLERANCE_SECONDS = 5 * 60


class PolarError(Exception):
    pass


def _api_base():
    if getattr(settings, 'POLAR_SERVER', 'sandbox') == 'production':
        return 'https://api.polar.sh/v1'
    return 'https://sandbox-api.polar.sh/v1'


def _headers():
    token = getattr(settings, 'POLAR_ACCESS_TOKEN', '')
    if not token:
        raise PolarError("Payments aren't configured yet (POLAR_ACCESS_TOKEN missing).")
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def product_map():
    """{product_id: (plan, interval)} from settings."""
    pairs = {
        ('starter', 'month'): getattr(settings, 'POLAR_PRODUCT_STARTER_MONTHLY', ''),
        ('starter', 'year'): getattr(settings, 'POLAR_PRODUCT_STARTER_YEARLY', ''),
        ('premium', 'month'): getattr(settings, 'POLAR_PRODUCT_PREMIUM_MONTHLY', ''),
    }
    return {pid: key for key, pid in pairs.items() if pid}


def product_id_for(plan, interval):
    for pid, key in product_map().items():
        if key == (plan, interval):
            return pid
    raise PolarError(f"No Polar product configured for {plan} / {interval}.")


# ---------------------------------------------------------------- checkout & portal

def create_checkout_url(profile, plan, interval, success_url, return_url):
    body = {
        'products': [product_id_for(plan, interval)],
        'external_customer_id': str(profile.user_id),   # how webhooks find this account again
        'customer_email': profile.user.email or None,
        'success_url': success_url,
        'return_url': return_url,
        'metadata': {'user_id': str(profile.user_id), 'plan': plan, 'interval': interval},
        'allow_trial': False,   # the 14-day trial is handled by the app, without a card
    }
    resp = requests.post(f"{_api_base()}/checkouts/", json=body, headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 300:
        logger.error("Polar checkout failed %s: %s", resp.status_code, resp.text[:300])
        raise PolarError("Couldn't open the payment page — please try again in a minute.")
    return resp.json()['url']


def customer_portal_url(profile, return_url):
    resp = requests.post(
        f"{_api_base()}/customer-sessions/",
        json={'external_customer_id': str(profile.user_id), 'return_url': return_url},
        headers=_headers(), timeout=TIMEOUT,
    )
    if resp.status_code >= 300:
        logger.error("Polar portal failed %s: %s", resp.status_code, resp.text[:300])
        raise PolarError("Couldn't open the billing portal — please try again in a minute.")
    return resp.json()['customer_portal_url']


# ---------------------------------------------------------------- webhooks

def _signing_keys(secret):
    """
    Standard Webhooks secrets ('whsec_' + base64 key) are base64-decoded.
    Older Polar secrets use the raw secret bytes. Accept either.
    """
    keys = [secret.encode()]
    raw = secret[len('whsec_'):] if secret.startswith('whsec_') else secret
    try:
        keys.insert(0, base64.b64decode(raw))
    except Exception:
        pass
    return keys


def verify_webhook(body: bytes, headers) -> bool:
    """Standard Webhooks signature check: HMAC-SHA256 of '{id}.{timestamp}.{body}'."""
    secret = getattr(settings, 'POLAR_WEBHOOK_SECRET', '')
    msg_id = headers.get('webhook-id')
    timestamp = headers.get('webhook-timestamp')
    signatures = headers.get('webhook-signature', '')
    if not (secret and msg_id and timestamp and signatures):
        return False
    try:
        if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
            return False   # replayed / stale
    except ValueError:
        return False

    signed = f"{msg_id}.{timestamp}.".encode() + body
    for key in _signing_keys(secret):
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        for part in signatures.split():
            version, _, sig = part.partition(',')
            if version == 'v1' and hmac.compare_digest(sig, expected):
                return True
    return False


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return parse_datetime(value)


def _find_profile(sub):
    customer = sub.get('customer') or {}
    candidates = [customer.get('external_id'), (sub.get('metadata') or {}).get('user_id')]
    for user_id in candidates:
        if user_id and str(user_id).isdigit():
            profile = BusinessProfile.objects.filter(user_id=int(user_id)).first()
            if profile:
                return profile
    if sub.get('id'):
        return BusinessProfile.objects.filter(billing_subscription_id=sub['id']).first()
    return None


SUBSCRIPTION_EVENTS = {
    'subscription.created', 'subscription.updated', 'subscription.active',
    'subscription.canceled', 'subscription.uncanceled', 'subscription.revoked',
    'subscription.past_due',
}


def apply_event(event: dict):
    """Updates the account from a verified webhook. Returns the profile or None."""
    if event.get('type') not in SUBSCRIPTION_EVENTS:
        return None
    sub = event.get('data') or {}
    profile = _find_profile(sub)
    if profile is None:
        logger.warning("Polar webhook for unknown customer: %s", sub.get('id'))
        return None

    plan_interval = product_map().get(sub.get('product_id'))
    if plan_interval:
        profile.plan, profile.billing_interval = plan_interval

    status = sub.get('status') or profile.subscription_status
    if event['type'] == 'subscription.revoked':
        status = 'revoked'   # access ends now (e.g. unpaid after retries, or refunded)

    profile.subscription_status = status
    profile.billing_provider = 'polar'
    profile.billing_subscription_id = sub.get('id') or profile.billing_subscription_id
    profile.billing_customer_id = (sub.get('customer') or {}).get('id') or profile.billing_customer_id
    profile.current_period_end = _dt(sub.get('current_period_end')) or profile.current_period_end

    if status == 'past_due':
        profile.past_due_since = profile.past_due_since or timezone.now()
    else:
        profile.past_due_since = None

    profile.save(update_fields=[
        'plan', 'billing_interval', 'subscription_status', 'billing_provider',
        'billing_subscription_id', 'billing_customer_id', 'current_period_end', 'past_due_since',
    ])
    return profile
