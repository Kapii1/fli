"""HTTP client implementation with impersonation, rate limiting and retry functionality.

This module provides a robust HTTP client that handles:
- User agent impersonation (to mimic a browser)
- Rate limiting (10 requests per second)
- Automatic retries with exponential backoff
- Session management
- Error handling
"""

from typing import Any

from curl_cffi import requests
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential

client = None


class Client:
    """HTTP client with built-in rate limiting, retry and user agent impersonation functionality."""

    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }
    # Google throttles cold sessions with ~22 s delayed responses; a tight
    # per-request timeout lets tenacity retry fast instead of hanging.
    REQUEST_TIMEOUT = 10

    def __init__(self):
        """Initialize a new client session with default headers."""
        # Set impersonate at session level so curl pools connections with a
        # consistent JA3 fingerprint across all requests (per-call impersonate
        # can break keep-alive reuse).
        self._client = requests.Session(impersonate="chrome")
        self._client.headers.update(self.DEFAULT_HEADERS)
        try:
            self._client.head(
                "https://www.google.com/travel/flights",
                timeout=5,
                allow_redirects=False,
            )
        except Exception:
            pass

    def __del__(self):
        """Clean up client session on deletion."""
        if hasattr(self, "_client"):
            self._client.close()

    @sleep_and_retry
    @limits(calls=10, period=1)
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, max=4), reraise=True)
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a rate-limited GET request with automatic retries.

        Args:
            url: Target URL for the request
            **kwargs: Additional arguments passed to requests.get()

        Returns:
            Response object from the server

        Raises:
            Exception: If request fails after all retries

        """
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        try:
            response = self._client.get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"GET request failed: {str(e)}") from e

    @sleep_and_retry
    @limits(calls=10, period=1)
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, max=4), reraise=True)
    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a rate-limited POST request with automatic retries.

        Args:
            url: Target URL for the request
            **kwargs: Additional arguments passed to requests.post()

        Returns:
            Response object from the server

        Raises:
            Exception: If request fails after all retries

        """
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        try:
            response = self._client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"POST request failed: {str(e)}") from e


def get_client() -> Client:
    """Get or create a shared HTTP client instance.

    Returns:
        Singleton instance of the HTTP client

    """
    global client
    if not client:
        client = Client()
    return client
