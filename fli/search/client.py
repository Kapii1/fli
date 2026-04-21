"""HTTP client for Google Flights endpoints.

Two client flavors:

- :class:`Client` — general-purpose client for SearchFlights / SearchDates.
  Rate-limited, retried, shared singleton.
- :class:`FastClient` — HTTP/3 + DoH + chrome133a client for the Explore
  endpoint, where a single hot request matters more than pooling. Build a
  fresh instance per logical search: Google slow-serves (~20 s) any session
  that has already issued a POST on this path.

Both share impersonation and header defaults.
"""

from __future__ import annotations

import time as _time
from threading import Lock as _Lock
from typing import Any

from curl_cffi import CurlHttpVersion, CurlOpt, requests
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential

client = None

DEFAULT_HEADERS = {
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
}

# curl_cffi impersonation profile. chrome133a matches the JA3/JA4 Google
# currently expects from Chrome 133; older `chrome` profiles can trigger
# anti-abuse slow-serve.
IMPERSONATE = "chrome133a"


class _GoogleResolver:
    """DoH-based resolver for ``www.google.com``.

    Some networks hijack ``www.google.com`` to the IETF sinkhole
    ``192.0.0.88``, which looks identical to server-side throttling. DoH
    bypasses the local resolver. All DoH endpoints are IP literals so this
    lookup itself never needs DNS.
    """

    _ips: list[str] = []
    _expiry: float = 0.0
    _lock = _Lock()
    _DOH_URLS = (
        "https://1.1.1.1/dns-query?name=www.google.com&type=A",
        "https://8.8.8.8/resolve?name=www.google.com&type=A",
        "https://9.9.9.9/dns-query?name=www.google.com&type=A",
    )

    @classmethod
    def get(cls) -> list[str]:
        now = _time.time()
        if cls._ips and now < cls._expiry:
            return cls._ips
        with cls._lock:
            if cls._ips and now < cls._expiry:
                return cls._ips
            for url in cls._DOH_URLS:
                try:
                    resp = requests.get(
                        url, headers={"accept": "application/dns-json"}, timeout=3
                    )
                    answers = [
                        a
                        for a in resp.json().get("Answer", [])
                        if a.get("type") == 1 and a.get("data")
                    ]
                    if answers:
                        cls._ips = [a["data"] for a in answers]
                        ttl = min((a.get("TTL", 60) for a in answers), default=60)
                        cls._expiry = now + max(ttl, 30)
                        return cls._ips
                except Exception:
                    continue
            raise RuntimeError("DoH resolution of www.google.com failed on all providers")


_attempt_counter = 0
_counter_lock = _Lock()


def _next_ip_index() -> int:
    global _attempt_counter
    with _counter_lock:
        _attempt_counter += 1
        return _attempt_counter


def _make_resolved_session(ip_index: int = 0) -> requests.Session:
    """Build a session with a DoH-resolved ``www.google.com`` → IP binding."""
    ips = _GoogleResolver.get()
    rotated = ips[ip_index % len(ips) :] + ips[: ip_index % len(ips)]
    resolve_entry = f"www.google.com:443:{','.join(rotated)}"
    sess = requests.Session(
        impersonate=IMPERSONATE,
        curl_options={CurlOpt.RESOLVE: [resolve_entry]},
    )
    sess.headers.update(DEFAULT_HEADERS)
    return sess


class Client:
    """Rate-limited HTTP client with impersonation and retries.

    Suitable for SearchFlights and SearchDates. Uses chrome133a impersonation,
    a shared session for connection reuse, and a 10 req/sec cap. Does NOT force
    HTTP/3 — use :class:`FastClient` if you need h3 + DoH (e.g. for Explore).
    """

    DEFAULT_HEADERS = DEFAULT_HEADERS
    REQUEST_TIMEOUT = 10

    def __init__(self):
        """Initialize a new client session with default headers."""
        self._client = requests.Session(impersonate=IMPERSONATE)
        self._client.headers.update(self.DEFAULT_HEADERS)
        try:
            # Full GET (not HEAD) so Google's Set-Cookie headers for NID et al.
            # are returned — HEAD responses sometimes omit them.
            self._client.get(
                "https://www.google.com/travel/flights",
                timeout=5,
                allow_redirects=True,
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
        """Make a rate-limited GET request with automatic retries."""
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
        """Make a rate-limited POST request with automatic retries."""
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        try:
            response = self._client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise Exception(f"POST request failed: {str(e)}") from e


class FastClient:
    """HTTP/3 + DoH + chrome133a client tuned for GetExploreDestinations.

    Use one instance per search — Google slow-serves (~20 s) any session that
    has already POSTed to the Explore endpoint. Drop-in for :class:`Client`:
    exposes ``.post(url, **kwargs)``.

    Not rate-limited or retried the same way — the only retry is a single
    IP rotation on network-level stalls (timeouts, resolution failures).
    Auth / 4xx errors fail fast.
    """

    REQUEST_TIMEOUT = 5

    def __init__(self):
        """Build a fresh DoH-resolved session."""
        self._client = _make_resolved_session(0)

    def __del__(self):
        """Close the session on deletion."""
        if hasattr(self, "_client"):
            self._client.close()

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """POST with HTTP/3, retrying once against a rotated IP on stalls."""
        # chrome133a is already applied at the session level; a per-call
        # impersonate= kwarg overrides it and breaks H3 + keep-alive reuse.
        kwargs.pop("impersonate", None)
        kwargs.setdefault("http_version", CurlHttpVersion.V3)
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)

        last_exc: Exception | None = None
        for attempt in range(2):
            sess = self._client if attempt == 0 else _make_resolved_session(_next_ip_index())
            try:
                response = sess.post(url, **kwargs)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                retriable = (
                    "timed out" in err or "timeout" in err or "could not resolve" in err
                )
                if attempt == 0 and retriable:
                    continue
                raise Exception(f"POST request failed: {exc}") from exc
        raise Exception(f"POST request failed after retry: {last_exc}")


def get_client() -> Client:
    """Get or create the shared :class:`Client` singleton."""
    global client
    if not client:
        client = Client()
    return client


def get_fast_client() -> FastClient:
    """Build a fresh :class:`FastClient`.

    Not a singleton by design — Explore requires a per-search session.
    """
    return FastClient()
