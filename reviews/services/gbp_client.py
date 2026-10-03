"""
Google Business Profile client for Mehrly (optional "Connect Google Business Profile" flow).

Talks to Google over plain REST (requests), so no Google SDK is needed.
Refresh tokens are stored encrypted (Fernet, key = settings.TOKEN_ENCRYPTION_KEY).
Access tokens are short-lived and only kept in Django's cache.

Stored on BusinessProfile:
  google_business_refresh_token -> encrypted refresh token
  google_business_account_id    -> e.g. "accounts/1234567890"
  google_business_location_id   -> e.g. "locations/9876543210"
"""
import logging
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.cache import cache
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/business.manage"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/reviews"
TIMEOUT = 20

STAR_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


class GBPError(Exception):
    """Anything that went wrong talking to Google."""


class GBPNotConnected(GBPError):
    """No (valid) Google Business Profile connection - user must connect/reconnect."""


# ---------------------------------------------------------------- encryption

def _fernet():
    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        raise GBPError("TOKEN_ENCRYPTION_KEY is not set.")
    try:
        return Fernet(key.encode())
    except ValueError:
        raise GBPError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.")


def encrypt_token(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_token(encrypted):
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken:
        raise GBPNotConnected("Stored Google token can't be decrypted - please reconnect.")


# ---------------------------------------------------------------- helpers

def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def _error_message(resp):
    err = _json(resp).get("error", {})
    if isinstance(err, dict):
        return err.get("message") or resp.text[:200]
    return str(err) or resp.text[:200]


def redirect_uri():
    """Must be added under 'Authorized redirect URIs' in Google Cloud Console."""
    return f"{settings.SITE_URL.rstrip('/')}/google-business/callback/"


# ---------------------------------------------------------------- OAuth

def build_auth_url(state):
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # gives us a refresh token
        "prompt": "consent",        # forces the refresh token to be returned every time
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code):
    """Swap the ?code= from Google's redirect for tokens. Returns Google's token dict."""
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise GBPError(f"Google sign-in failed: {_error_message(resp)}")
    data = _json(resp)
    if not data.get("refresh_token"):
        raise GBPError("Google did not return a refresh token - please try connecting again.")
    return data


def store_refresh_token(profile, refresh_token):
    profile.google_business_refresh_token = encrypt_token(refresh_token)
    profile.save(update_fields=["google_business_refresh_token"])
    cache.delete(f"gbp_access:{profile.pk}")


def disconnect(profile):
    profile.google_business_refresh_token = None
    profile.google_business_account_id = None
    profile.google_business_location_id = None
    profile.save(update_fields=[
        "google_business_refresh_token",
        "google_business_account_id",
        "google_business_location_id",
    ])
    cache.delete(f"gbp_access:{profile.pk}")


def get_access_token(profile):
    if not profile.google_business_refresh_token:
        raise GBPNotConnected("Google Business Profile is not connected.")

    cache_key = f"gbp_access:{profile.pk}"
    token = cache.get(cache_key)
    if token:
        return token

    resp = requests.post(TOKEN_URL, data={
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": decrypt_token(profile.google_business_refresh_token),
        "grant_type": "refresh_token",
    }, timeout=TIMEOUT)
    data = _json(resp)

    if resp.status_code != 200:
        if data.get("error") == "invalid_grant":
            raise GBPNotConnected("Google access was revoked or expired - please reconnect.")
        raise GBPError(f"Could not refresh Google access: {_error_message(resp)}")

    token = data["access_token"]
    cache.set(cache_key, token, max(int(data.get("expires_in", 3600)) - 120, 60))
    return token


def _api(profile, method, url, **kwargs):
    resp = None
    for attempt in (1, 2):
        token = get_access_token(profile)
        resp = requests.request(
            method, url, headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT, **kwargs,
        )
        if resp.status_code == 401 and attempt == 1:
            cache.delete(f"gbp_access:{profile.pk}")  # stale token, refresh and retry once
            continue
        break

    if resp.status_code == 429:
        raise GBPError("Google rate limit reached - try again in a minute.")
    if not resp.ok:
        raise GBPError(f"Google API {resp.status_code}: {_error_message(resp)}")
    return _json(resp)


# ---------------------------------------------------------------- accounts & locations

def list_accounts(profile):
    data = _api(profile, "GET", ACCOUNTS_URL)
    return [
        {"name": a["name"], "title": a.get("accountName", a["name"])}
        for a in data.get("accounts", [])
    ]


def list_locations(profile, account_name):
    """account_name looks like 'accounts/123'. Returns [{'name': 'locations/456', 'title': ..., 'address': ...}]"""
    url = LOCATIONS_URL.format(account=account_name)
    out, page_token = [], None
    while True:
        params = {"readMask": "name,title,storefrontAddress", "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        data = _api(profile, "GET", url, params=params)
        for loc in data.get("locations", []):
            addr = loc.get("storefrontAddress", {}) or {}
            lines = ", ".join(addr.get("addressLines", []))
            address = ", ".join(p for p in [lines, addr.get("locality", "")] if p)
            out.append({"name": loc["name"], "title": loc.get("title", ""), "address": address})
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def save_location(profile, account_name, location_name, title=None):
    profile.google_business_account_id = account_name
    profile.google_business_location_id = location_name
    fields = ["google_business_account_id", "google_business_location_id"]
    if title:
        profile.business_name = title
        fields.append("business_name")
    profile.save(update_fields=fields)


# ---------------------------------------------------------------- reviews

def _base(profile):
    if not profile.gbp_connected:
        raise GBPNotConnected("No Google Business location selected.")
    return REVIEWS_URL.format(
        account=profile.google_business_account_id,
        location=profile.google_business_location_id,
    )


def fetch_reviews(profile, max_reviews=200):
    """Newest first. Returns normalized dicts ready to turn into Review rows."""
    url = _base(profile)
    reviews, page_token = [], None
    while len(reviews) < max_reviews:
        params = {"pageSize": 50, "orderBy": "updateTime desc"}
        if page_token:
            params["pageToken"] = page_token
        data = _api(profile, "GET", url, params=params)
        for r in data.get("reviews", []):
            reply = r.get("reviewReply") or {}
            reviews.append({
                "external_id": r.get("reviewId"),
                "reviewer_name": (r.get("reviewer") or {}).get("displayName") or "Anonymous",
                "rating": STAR_MAP.get(r.get("starRating"), 0),
                "comment": r.get("comment", ""),
                "created_at": parse_datetime(r["createTime"]) if r.get("createTime") else None,
                "has_reply": bool(reply.get("comment")),
                "reply_text": reply.get("comment", ""),
            })
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return reviews


def post_reply(profile, review_id, text):
    """Publish (or overwrite) the owner's reply on Google. review_id = Google's reviewId."""
    url = f"{_base(profile)}/{review_id}/reply"
    _api(profile, "PUT", url, json={"comment": text})
    return True