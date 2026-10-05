"""
Files for search engines and AI assistants (free GEO/SEO basics):
  /robots.txt   - what crawlers may visit
  /sitemap.xml  - list of public pages
  /llms.txt     - plain-language summary of Mehrly for AI assistants (llmstxt.org)
"""
from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.cache import cache_control

PUBLIC_PAGES = [  # (url name, priority)
    ('home', '1.0'),
    ('getting_started', '0.6'),
    ('request_access_code', '0.5'),
    ('terms_of_service', '0.3'),
    ('privacy_policy', '0.3'),
    ('refund_policy', '0.3'),
    ('security', '0.3'),
]


def _site():
    return getattr(settings, 'SITE_URL', 'https://mehrly.com').rstrip('/')


@cache_control(max_age=86400, public=True)
def robots_txt(request):
    lines = [
        'User-agent: *',
        'Allow: /',
        # Private app areas: nothing useful for search engines there.
        'Disallow: /admin/',
        'Disallow: /accounts/',
        'Disallow: /dashboard/',
        'Disallow: /api/',
        'Disallow: /i18n/',
        '',
        f'Sitemap: {_site()}/sitemap.xml',
        '',
    ]
    return HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')


@cache_control(max_age=86400, public=True)
def sitemap_xml(request):
    urls = ''.join(
        f'  <url><loc>{_site()}{reverse(name)}</loc><priority>{prio}</priority></url>\n'
        for name, prio in PUBLIC_PAGES
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + '</urlset>\n')
    return HttpResponse(xml, content_type='application/xml; charset=utf-8')


LLMS_TXT = """# Mehrly

> Mehrly drafts personal replies to Google reviews for restaurants, cafés and hotels. It detects the guest's language (French, English, German, Italian, Swiss German and more) and writes a reply in the business's own tone. Owners approve each reply; good reviews come pre-approved for one-click copy and paste on Google, and 1-3 star reviews always wait for a human.

Mehrly is a web app (no install) built in Geneva, Switzerland. Prices are in Swiss francs (CHF).

## What it does
- Imports public Google reviews automatically (and optionally Tripadvisor or a custom webhook).
- Writes a draft reply to each new review in the reviewer's language, using the business's tone, signature and details (dishes, parking, policies).
- Learns from the owner's edits so later drafts sound more like them.
- Smart Guardrail: 4-5 star replies are pre-approved; 1-3 star reviews always need human approval.
- Quick Reply: copies the approved text and opens the review on Google, so posting takes a few seconds. Fully automatic posting to Google is coming soon.
- Email alerts for 1-2 star reviews, weekly summary emails, complaint trend analysis, competitor benchmarking.
- Table QR codes that invite every guest to leave a public Google review (Google-policy-safe, no review gating).
- Team roles (owner, admin, reviewer, viewer) so staff can approve replies without sharing passwords.

## Pricing
- Free trial: 14 days of Premium, no credit card.
- Starter: CHF 19/month, or CHF 12.42/month billed yearly. One location, AI drafts, Quick Reply, Guardrail.
- Premium: CHF 39/month. Adds Hands-Free mode, Tripadvisor sync, QR codes, competitor benchmarking, complaint analysis, team seats, instant alerts.
- Founding Partner (Geneva launch): 1 month of Premium free, no card, for a few early restaurants.
- Cancel anytime; payments handled by Polar (Merchant of Record).

## Who it is for
Independent restaurants, cafés, bars, hotels and shops that get Google reviews but have no time to answer them, especially in multilingual places like Switzerland.

## Links
- Website: {site}/
- Getting started: {site}/getting-started/
- Founding Partner access code: {site}/request-access/
- Terms: {site}/terms-of-service/
- Privacy: {site}/privacy-policy/
- Refunds: {site}/refund-policy/
- Security: {site}/security/
- Instagram: https://www.instagram.com/mehrly.app/
- Contact: {email}
"""


@cache_control(max_age=86400, public=True)
def llms_txt(request):
    text = LLMS_TXT.format(site=_site(), email=settings.SUPPORT_EMAIL)
    return HttpResponse(text, content_type='text/plain; charset=utf-8')
