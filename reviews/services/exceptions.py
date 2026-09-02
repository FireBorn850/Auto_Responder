class RateLimitError(Exception):
    """Raised when SerpAPI reports we've hit a rate limit or run out of searches."""
    pass