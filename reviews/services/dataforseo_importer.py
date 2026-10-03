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


GOOGLE = 'business_data/google/reviews'
TRIPADVISOR = 'business_data/tripadvisor/reviews'


def post_task(endpoint, task):
    """Submits one DataForSEO task and returns its id. Fast (one HTTP call)."""
    resp = requests.post(f"{_base()}/{endpoint}/task_post", json=[task], auth=_auth(), timeout=30)
    if resp.status_code == 429:
        raise RateLimitError("DataForSEO rate limit hit.")
    body = resp.json()
    if body.get('status_code') != 20000:
        raise Exception(f"DataForSEO {body.get('status_code')}: {body.get('status_message')}")
    created = body['tasks'][0]
    if created['status_code'] != 20100:
        raise Exception(f"DataForSEO {created['status_code']}: {created['status_message']}")
    return created['id']


def get_task_result(endpoint, task_id):
    """
    Asks once whether a task is finished. Returns the result dict, or None if
    DataForSEO is still working on it. Raises if the task failed. Fast.
    Fetching a finished task again is free, so asking repeatedly costs nothing.
    """
    got = requests.get(f"{_base()}/{endpoint}/task_get/{task_id}", auth=_auth(), timeout=30).json()['tasks'][0]
    if got.get('result'):
        return got['result'][0]
    if got['status_code'] not in _STILL_WORKING:
        raise Exception(f"DataForSEO {got['status_code']}: {got['status_message']}")
    return None


def wait_for_result(endpoint, task_id, wait_seconds):
    """Blocking wait — only for the background daily sync, never a web request."""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(3)
        result = get_task_result(endpoint, task_id)
        if result is not None:
            return result
    raise TimeoutError(f"DataForSEO task {task_id} not ready after {wait_seconds}s.")


def google_task(business_name, place_id=None, depth=10, priority=1):
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
    return task


def google_items(result):
    """Splits a finished Google task result into (items, info)."""
    return result.get('items') or [], {'place_id': result.get('place_id'), 'cid': result.get('cid')}


def fetch_reviews(business_name, place_id=None, depth=10, wait_seconds=90, priority=2):
    """
    Posts a DataForSEO reviews task, waits for it, returns (items, info).
    Uses place_id when known (precise), otherwise searches by business name.
    Blocking — used by the background daily sync only.
    """
    task_id = post_task(GOOGLE, google_task(business_name, place_id, depth, priority))
    return google_items(wait_for_result(GOOGLE, task_id, wait_seconds))
