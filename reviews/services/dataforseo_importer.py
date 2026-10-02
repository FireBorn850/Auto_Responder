import re
import time
import logging
import requests
from django.conf import settings
from .exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Codes that mean "still working", not "failed" (as far as I know from the docs).
_STILL_WORKING = (20000, 40601, 40602)


def _auth():
    return (settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD)


def _base():
    return getattr(settings, 'DATAFORSEO_BASE_URL', 'https://api.dataforseo.com/v3').rstrip('/')


def place_id_from_url(url):
    """Pulls the Google place_id out of a '...writereview?placeid=XXXX' link."""
    match = re.search(r'placeid=([A-Za-z0-9_-]+)', url or '')
    return match.group(1) if match else None


def fetch_reviews(business_name, place_id=None, depth=10, wait_seconds=90, priority=2):
    """
    Posts a DataForSEO reviews task, waits for it, returns (items, info).
    Uses place_id when known (precise), otherwise searches by business name.
    """
    task = {
        'location_name': 'Switzerland',
        'language_code': 'en',
        'depth': depth,
        'sort_by': 'newest',
        'priority': priority,
    }
    if place_id:
        task['place_id'] = place_id
    else:
        task['keyword'] = business_name

    resp = requests.post(
        f"{_base()}/business_data/google/reviews/task_post",
        json=[task], auth=_auth(), timeout=30,
    )
    if resp.status_code == 429:
        raise RateLimitError("DataForSEO rate limit hit.")
    body = resp.json()
    if body.get('status_code') != 20000:
        raise Exception(f"DataForSEO {body.get('status_code')}: {body.get('status_message')}")

    created = body['tasks'][0]
    if created['status_code'] != 20100:
        raise Exception(f"DataForSEO {created['status_code']}: {created['status_message']}")

    task_id = created['id']
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        got = requests.get(
            f"{_base()}/business_data/google/reviews/task_get/{task_id}",
            auth=_auth(), timeout=30,
        ).json()['tasks'][0]

        if got.get('result'):
            result = got['result'][0]
            return result.get('items') or [], {
                'place_id': result.get('place_id'),
                'cid': result.get('cid'),
            }
        if got['status_code'] not in _STILL_WORKING:
            raise Exception(f"DataForSEO {got['status_code']}: {got['status_message']}")

    raise TimeoutError(f"DataForSEO task {task_id} not ready after {wait_seconds}s.")