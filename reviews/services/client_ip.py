"""
The visitor's real IP address, in a way a visitor can't fake.

On Render, a proxy sits in front of the app. It ADDS the real client IP as
the LAST entry of the X-Forwarded-For header. Anything before that was sent
by the browser itself and can be any text — the old code trusted the FIRST
entry, so anyone could pick a fresh "IP" per request and dodge every limit.

TRUSTED_PROXY_COUNT (setting / env var) = how many proxies we trust.
Render: 1 (the default in production). Local development: 0.
"""
import ipaddress

from django.conf import settings


def get_client_ip(request):
    trusted = getattr(settings, 'TRUSTED_PROXY_COUNT', 0)
    remote = request.META.get('REMOTE_ADDR') or ''
    if trusted > 0:
        hops = [h.strip() for h in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if h.strip()]
        if len(hops) >= trusted:
            candidate = hops[-trusted]
            if _valid(candidate):
                return candidate
    return remote if _valid(remote) else 'unknown'


def _valid(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
