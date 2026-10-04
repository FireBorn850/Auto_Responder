class RateLimitError(Exception):
    """Raised when the review data provider (DataForSEO) reports we've hit a rate limit or run out of searches."""
    pass