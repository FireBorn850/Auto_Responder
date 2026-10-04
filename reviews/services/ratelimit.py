"""
Tiny rate limiter on top of Django's cache.

The cache is the database (CACHES in settings), so every server process and
every restart sees the same counters — unlike the old per-process memory
cache, which each worker kept separately. Free: it's a table in Neon.
"""
from django.core.cache import cache


def too_many(key, limit, window_seconds):
    """True if `key` was already hit `limit` times in the current window."""
    return cache.get(f"rl:{key}", 0) >= limit


def hit(key, window_seconds):
    """Counts one attempt. The window starts at the first attempt."""
    full = f"rl:{key}"
    if cache.add(full, 1, timeout=window_seconds):
        return 1
    try:
        return cache.incr(full)
    except ValueError:   # expired between add and incr
        cache.set(full, 1, timeout=window_seconds)
        return 1


def reset(key):
    cache.delete(f"rl:{key}")
