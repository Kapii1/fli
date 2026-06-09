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
from queue import Empty as _QueueEmpty
from queue import Queue as _Queue
from threading import Lock as _Lock
from threading import Thread as _Thread
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
                    resp = requests.get(url, headers={"accept": "application/dns-json"}, timeout=3)
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

    Not rate-limited or retried the same way. Instead of waiting out the full
    request timeout before retrying, :meth:`post` *hedges*: if no response has
    arrived after :attr:`HEDGE_DELAY` seconds (or the in-flight attempt failed
    on a network-level error), a duplicate request is fired on a fresh
    DoH-resolved session bound to a rotated server IP, and the first response
    to complete wins. Slow-serves and QUIC stalls are per-connection, so the
    hedge typically answers in normal time (~1 s) instead of the 20 s the
    stalled attempt would have burned. Auth / 4xx errors still fail fast.
    """

    # Google actively slow-serves throttled origins with ~22 s responses.
    # 20 s matches what _make_search_explore() used to set per-call;
    # setting it here covers SearchFlights and SearchExploreDetails too.
    REQUEST_TIMEOUT = 20

    # Seconds without a completed response before a duplicate request is
    # launched on a fresh session; re-arms every HEDGE_DELAY until
    # MAX_ATTEMPTS is reached (hedges at ~3 s and ~6 s), since both in-flight
    # connections occasionally get slow-served together. Healthy responses
    # complete in 0.5–2 s, so only the slow tail ever hedges. Set to 0/None
    # to disable hedging (degrades to fail-then-retry).
    HEDGE_DELAY: float | None = 3.0

    # Hard cap on sessions used per logical request (initial + replacements).
    MAX_ATTEMPTS = 3

    # Substrings of curl/curl_cffi error text that indicate a transport-level
    # stall worth retrying on a fresh session. "quic" covers handshake stalls
    # (curl 28 "QUIC needs at least ..."), which the old timeout-only match
    # missed entirely.
    _RETRIABLE_ERRORS = (
        "timed out",
        "timeout",
        "could not resolve",
        "connect",
        "quic",
        "reset",
        "curl: (28)",
        "curl: (55)",
        "curl: (56)",
    )

    def __init__(self):
        """Build a fresh DoH-resolved session."""
        self._client = _make_resolved_session(0)

    def __del__(self):
        """Close the session on deletion."""
        if hasattr(self, "_client"):
            self._client.close()

    @classmethod
    def _is_retriable(cls, exc: Exception) -> bool:
        err = str(exc).lower()
        return any(needle in err for needle in cls._RETRIABLE_ERRORS)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """POST with HTTP/3, hedging onto a fresh session if the first stalls.

        At most :attr:`MAX_ATTEMPTS` sessions are used and no new attempt is
        launched once :attr:`REQUEST_TIMEOUT` has elapsed, so the worst case
        is ``HEDGE_DELAY + REQUEST_TIMEOUT`` instead of the serial
        ``2 × REQUEST_TIMEOUT`` of a fail-then-retry strategy.
        """
        # chrome133a is already applied at the session level; a per-call
        # impersonate= kwarg overrides it and breaks H3 + keep-alive reuse.
        kwargs.pop("impersonate", None)
        kwargs.setdefault("http_version", CurlHttpVersion.V3)
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)

        results: _Queue = _Queue()

        def _run(sess: requests.Session, own_session: bool) -> None:
            try:
                response = sess.post(url, **kwargs)
                response.raise_for_status()
                results.put(("ok", response))
            except Exception as exc:
                results.put(("err", exc))
            finally:
                # Replacement sessions are created per-attempt; the response
                # body is fully buffered (stream=False), so closing here is
                # safe even when this attempt won the race.
                if own_session:
                    sess.close()

        def _spawn(sess: requests.Session, own_session: bool) -> None:
            _Thread(target=_run, args=(sess, own_session), daemon=True).start()

        started_at = _time.monotonic()

        def _may_spawn(attempts: int) -> bool:
            # Never extend the tail: once the original timeout budget is
            # spent, stop launching replacements and drain what's in flight.
            return (
                attempts < self.MAX_ATTEMPTS and _time.monotonic() - started_at < kwargs["timeout"]
            )

        _spawn(self._client, False)
        attempts = 1
        in_flight = 1
        last_exc: Exception | None = None

        while True:
            # Hedge timer: arm only while every attempt so far is still
            # silently in flight (a failure switches us to the replacement
            # logic below) and the attempt/time budgets allow another one.
            may_hedge = bool(self.HEDGE_DELAY) and last_exc is None and _may_spawn(attempts)
            try:
                kind, payload = results.get(timeout=self.HEDGE_DELAY if may_hedge else None)
            except _QueueEmpty:
                # No response within HEDGE_DELAY: race another duplicate on a
                # fresh session against the (probably slow-served) attempts.
                # Both in-flight connections being slow-served at once does
                # happen, so the timer keeps re-arming until MAX_ATTEMPTS.
                _spawn(_make_resolved_session(_next_ip_index()), True)
                attempts += 1
                in_flight += 1
                continue

            if kind == "ok":
                return payload

            in_flight -= 1
            last_exc = payload
            if in_flight == 0:
                if self._is_retriable(payload) and _may_spawn(attempts):
                    _spawn(_make_resolved_session(_next_ip_index()), True)
                    attempts += 1
                    in_flight += 1
                    continue
                raise Exception(f"POST request failed: {last_exc}") from last_exc
            # Another attempt is still in flight — wait for it instead of
            # piling on more connections.


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
