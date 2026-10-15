"""HTTP client implementation with impersonation, rate limiting and retry functionality.

This module provides a robust HTTP client that handles:

- User agent impersonation (to mimic a browser)
- Rate limiting (10 requests per second, *globally* across threads)
- Automatic retries with exponential backoff
- Thread-safe session management (one ``curl_cffi`` session per worker thread)
- Error handling

Threading model
---------------

``curl_cffi.requests.Session`` wraps a libcurl handle which is not safe
to share across threads. We keep one session per worker thread using
``threading.local``; the rate-limit budget is shared globally via
:class:`~fli.search._concurrency.TokenBucketRateLimiter` so concurrent
callers cooperate cleanly under Google's 10 req/sec ceiling.
"""

from __future__ import annotations

import os
import threading
import time as _time
from queue import Empty as _QueueEmpty
from queue import Queue as _Queue
from threading import Lock as _Lock
from threading import Thread as _Thread
from typing import TYPE_CHECKING, Any

from curl_cffi import CurlHttpVersion, CurlOpt, requests
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from fli.search._concurrency import TokenBucketRateLimiter
from fli.search.exceptions import (
    SearchCertificateError,
    SearchClientError,
    SearchConnectionError,
    SearchHTTPError,
    SearchTimeoutError,
)

# ``curl_cffi`` adds ~100ms to import time on first load — we only need
# it once an HTTP request actually fires, so import lazily on first use.
# ``TYPE_CHECKING`` makes the annotation visible to static checkers
# without paying the import cost at runtime.
if TYPE_CHECKING:
    from curl_cffi import requests as _curl_requests

    Response = _curl_requests.Response
    Session = _curl_requests.Session
else:
    Response = "Any"
    Session = "Any"

# Module-level singleton client + lock guarding its lazy initialisation.
# ``get_client()`` uses double-checked locking so concurrent first callers
# can't each construct an independent ``Client`` (each with its own
# ``TokenBucketRateLimiter`` — that would silently double the global
# request budget).
client: Client | None = None
_client_lock = threading.Lock()

# Google's published ceiling.
DEFAULT_CALLS_PER_SECOND = 10

# Request timeout in seconds.  Override with the FLI_TIMEOUT env var.
DEFAULT_TIMEOUT: float = 60.0
_env_timeout = os.environ.get("FLI_TIMEOUT")
if _env_timeout is not None:
    try:
        REQUEST_TIMEOUT: float = float(_env_timeout)
    except ValueError:
        msg = f"FLI_TIMEOUT must be a number of seconds, got: {_env_timeout!r}"
        raise ValueError(msg) from None
    if REQUEST_TIMEOUT <= 0:
        raise ValueError(f"FLI_TIMEOUT must be a positive number, got: {_env_timeout!r}")
else:
    REQUEST_TIMEOUT = DEFAULT_TIMEOUT

# EU/EEA IPs are redirected to Google's consent interstitial, which serves a
# page with no ds:1 payload — every search then fails to parse. A pre-accepted
# SOCS cookie skips the interstitial (the legacy CONSENT cookie no longer
# works, and a truncated SOCS value is ignored). Override with FLI_SOCS_COOKIE
# if Google rotates the value; set it empty to send no cookie at all.
DEFAULT_SOCS_COOKIE = (
    "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMzE3LjA5X3AwGgJlbiADGgYIgLC_rwY"
)
SOCS_COOKIE = os.environ.get("FLI_SOCS_COOKIE", DEFAULT_SOCS_COOKIE)

# A TLS-intercepting corporate proxy re-signs Google's certificate with a
# private CA that curl's bundled trust store doesn't know about, so every
# request fails verification. Checked in this order: FLI_CA_BUNDLE (this
# client's own override) first, then CURL_CA_BUNDLE and REQUESTS_CA_BUNDLE —
# the conventions curl and python-requests already use — so a developer who
# has one of those set for other tools does not have to duplicate it, while
# an explicit FLI_-prefixed value always wins.
_CA_BUNDLE_ENV_VARS = ("FLI_CA_BUNDLE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def _ca_bundle_from_env() -> str | None:
    """Return the first configured CA bundle path from the supported env vars.

    A configured path that is not a readable file raises
    :class:`SearchCertificateError` naming both the variable and the path —
    almost always a typo or a bundle that hasn't been mounted into a
    container yet, and worth failing on immediately with a clear message
    rather than surfacing later as a confusing curl error.

    Called once per thread, from ``Client._session()`` when that thread's
    session is first created — see its docstring for the caching
    consequence (changing the env var afterwards does not affect an
    already-created session).
    """
    for name in _CA_BUNDLE_ENV_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        if not os.path.isfile(value) or not os.access(value, os.R_OK):
            raise SearchCertificateError(
                f"{name} points to a CA bundle path that does not exist or "
                f"is not readable: {value!r}"
            )
        return value
    return None


class Client:
    """HTTP client with built-in rate limiting, retry and user agent impersonation functionality.

    Sessions are kept per-thread because ``curl_cffi.requests.Session`` is
    not thread-safe — concurrent ``post``/``get`` calls from different
    threads each get their own libcurl handle. The shared
    :class:`TokenBucketRateLimiter` enforces the global 10 req/sec budget
    across all of them.
    """

    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }

    def __init__(
        self,
        calls_per_second: int = DEFAULT_CALLS_PER_SECOND,
    ):
        """Initialise the shared rate limiter and per-thread session storage."""
        self._sessions = threading.local()
        self._rate_limiter = TokenBucketRateLimiter(calls=calls_per_second, period=1.0)

    def _session(self) -> Session:
        """Return this thread's ``Session``, creating it on first use.

        The CA-bundle env vars (see ``_ca_bundle_from_env``) and
        ``SOCS_COOKIE`` are only read the first time *this thread* needs a
        session — once created, a thread's session is cached for its
        lifetime (and the module-level singleton in ``get_client()``
        extends that across the whole process). Changing ``FLI_CA_BUNDLE``
        (or the other two) after a session already exists has no effect on
        it; only a new thread, or a fresh ``Client()``, picks up the
        change.
        """
        session = getattr(self._sessions, "session", None)
        if session is None:
            # Deferred import: ``curl_cffi`` is heavy (~100ms cold) and
            # not needed for CLI flows that never hit the network, so
            # only pull it in on the first real request.
            from curl_cffi import requests as _requests

            session = _requests.Session()
            session.headers.update(self.DEFAULT_HEADERS)
            if SOCS_COOKIE:
                session.cookies.set("SOCS", SOCS_COOKIE, domain=".google.com")
            ca_bundle = _ca_bundle_from_env()
            if ca_bundle:
                session.verify = ca_bundle
            self._sessions.session = session
        return session

    def __del__(self):
        """Best-effort cleanup of the main-thread session (others die with their thread)."""
        session = getattr(self._sessions, "session", None) if hasattr(self, "_sessions") else None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 — destruction-time best effort
                pass

    # ------------------------------------------------------------------
    # Request entry points
    # ------------------------------------------------------------------

    # A bad CA bundle path or an untrusted certificate is deterministic for
    # a fixed environment — retrying the identical request three times with
    # backoff before the user ever sees the actionable message just wastes
    # their time, so SearchCertificateError is the one failure mode this
    # decorator does not retry. retry_if_not_exception_type() sees the
    # *wrapped* type here, not the raw curl exception: `except Exception`
    # below always re-raises via `_wrap_request_error(...)` before this
    # decorator's predicate ever runs.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(),
        retry=retry_if_not_exception_type(SearchCertificateError),
        reraise=True,
    )
    def get(self, url: str, **kwargs: Any) -> Response:
        """Make a rate-limited GET request with automatic retries."""
        self._rate_limiter.acquire()
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = self._session().get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise _wrap_request_error("GET", url, e) from e

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(),
        retry=retry_if_not_exception_type(SearchCertificateError),
        reraise=True,
    )
    def post(self, url: str, **kwargs: Any) -> Response:
        """Make a rate-limited POST request with automatic retries."""
        self._rate_limiter.acquire()
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = self._session().post(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise _wrap_request_error("POST", url, e) from e


# libcurl's CURLE_PEER_FAILED_VERIFICATION code. curl_cffi's own
# ``code2error`` dispatch maps this 1:1 to ``CertificateVerifyError`` on
# both 0.15.0 and 0.16.3 (exceptions.py's CODE2ERROR table has no other
# entry for it in either version), so the isinstance() check below is
# normally enough on its own — see is_untyped_certificate_error in
# _wrap_request_error for what this constant backs and why it's kept
# despite being unreachable on those two versions.
_CURLE_PEER_FAILED_VERIFICATION = 60


def _wrap_request_error(method: str, url: str, exc: BaseException) -> SearchClientError:
    """Map curl-cffi / network errors into our typed ``SearchClientError`` family.

    The CLI surfaces these as short user-facing messages and writes the
    underlying traceback to a log file, so the message here should read
    well on its own.
    """
    # If a typed error somehow escapes the request body (e.g. a future
    # change in ``_session()``), keep its original type instead of
    # downgrading it to the generic fallback below.
    if isinstance(exc, SearchClientError):
        return exc

    # Imported lazily — ``curl_cffi.requests.exceptions`` triggers the
    # full curl-cffi load, which we otherwise defer until first request.
    from curl_cffi.requests import exceptions as curl_exc

    host = _host_from_url(url)

    # ``CertificateVerifyError`` is itself an ``SSLError`` -> ``ConnectionError``
    # subclass, so this must run before the generic ``curl_exc.ConnectionError``
    # branch below or a bad certificate would silently read as a vague "check
    # your connection" message instead of naming the fix. Two independent
    # forward-compat guards, kept as separate named conditions since they
    # guard against different future breakages:
    #
    # - is_typed_certificate_error: the normal case today. ``getattr(...,
    #   ())`` keeps this safe if a future curl_cffi release ever drops the
    #   class entirely — ``isinstance(exc, ())`` is simply always False.
    # - is_untyped_certificate_error: belt-and-suspenders for a future
    #   release that raises the plain ``SSLError``/``ConnectionError`` base
    #   class with the same code instead of the ``CertificateVerifyError``
    #   subclass (see ``_CURLE_PEER_FAILED_VERIFICATION`` above — confirmed
    #   unreachable on curl_cffi 0.15.0/0.16.3, kept anyway).
    certificate_error_cls = getattr(curl_exc, "CertificateVerifyError", ())
    is_typed_certificate_error = certificate_error_cls and isinstance(exc, certificate_error_cls)
    is_untyped_certificate_error = (
        isinstance(exc, curl_exc.ConnectionError)
        and getattr(exc, "code", None) == _CURLE_PEER_FAILED_VERIFICATION
    )
    if is_typed_certificate_error or is_untyped_certificate_error:
        env_vars = ", ".join(_CA_BUNDLE_ENV_VARS)
        return SearchCertificateError(
            f"TLS certificate verification failed for Google Flights ({host}). "
            f"If your network uses a custom certificate authority (e.g. a "
            f"TLS-intercepting corporate proxy), set one of {env_vars} to a "
            f"CA bundle path and try again."
        )
    if isinstance(exc, curl_exc.Timeout):
        return SearchTimeoutError(
            f"Timed out talking to Google Flights ({host}). "
            "The service may be slow or unreachable from your network — "
            "check your connection and try again."
        )
    if isinstance(exc, curl_exc.ConnectionError):
        return SearchConnectionError(
            f"Could not reach Google Flights ({host}). "
            "Check your internet connection or DNS and try again."
        )
    if isinstance(exc, curl_exc.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" (HTTP {status})" if status else ""
        return SearchHTTPError(
            f"Google Flights returned an error response{suffix}. "
            "The request may be malformed, rate-limited, or blocked.",
            status_code=status,
        )
    # Anything else (including bare CurlError) — keep a clean message but
    # preserve the original via ``__cause__`` so logs still show details.
    return SearchClientError(
        f"{method} request to Google Flights ({host}) failed: {exc.__class__.__name__}"
    )


def _host_from_url(url: str) -> str:
    """Best-effort host extraction for error messages."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or url
    except Exception:  # noqa: BLE001 — never let logging fail the request path
        return url


# --- FastClient support (HTTP/3 + DoH + chrome133a), used by the Explore path ---
IMPERSONATE = "chrome133a"
FAST_DEFAULT_HEADERS = {
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
}


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
    if SOCS_COOKIE:
        sess.cookies.set("SOCS", SOCS_COOKIE, domain=".google.com")
    sess.headers.update(FAST_DEFAULT_HEADERS)
    return sess


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

    # Set after a fast QUIC/TLS failure: some hosts cannot speak HTTP/3 at
    # all (UDP 443 blocked, curl built without TLS 1.3, corporate proxies).
    # Class-level so every per-search instance skips the doomed h3 attempt
    # once one has failed, instead of re-failing on each search. Only
    # failures faster than _H3_FAIL_FAST_S set it — a 20 s stall whose error
    # text happens to mention QUIC is a transient throttle, not proof that
    # the environment lacks HTTP/3.
    _h3_unavailable = False
    _H3_FAIL_FAST_S = 5.0

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

        Falls back to HTTP/2 — and remembers it class-wide — when the
        environment cannot complete a QUIC handshake at all. At most
        :attr:`MAX_ATTEMPTS` sessions are used and no new attempt is
        launched once :attr:`REQUEST_TIMEOUT` has elapsed, so the worst case
        is ``HEDGE_DELAY + REQUEST_TIMEOUT`` instead of the serial
        ``2 × REQUEST_TIMEOUT`` of a fail-then-retry strategy.
        """
        # chrome133a is already applied at the session level; a per-call
        # impersonate= kwarg overrides it and breaks H3 + keep-alive reuse.
        kwargs.pop("impersonate", None)
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        forced_version = kwargs.pop("http_version", None)

        results: _Queue = _Queue()

        def _version() -> int:
            if forced_version is not None:
                return forced_version
            if FastClient._h3_unavailable:
                return CurlHttpVersion.V2TLS
            return CurlHttpVersion.V3

        def _run(sess: requests.Session, own_session: bool) -> None:
            version = _version()
            attempt_started = _time.monotonic()
            try:
                response = sess.post(url, http_version=version, **kwargs)
                response.raise_for_status()
                results.put(("ok", response))
            except Exception as exc:
                err = str(exc).lower()
                if (
                    version == CurlHttpVersion.V3
                    and forced_version is None
                    and _time.monotonic() - attempt_started < self._H3_FAIL_FAST_S
                    and ("quic" in err or "tls" in err)
                ):
                    # Fast QUIC/TLS failure: this environment cannot do
                    # HTTP/3. Remember it so the replacement this error is
                    # about to trigger (and every later search) goes straight
                    # to HTTP/2.
                    FastClient._h3_unavailable = True
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
    """Get or create a shared HTTP client instance.

    Returns:
        Singleton instance of the HTTP client

    """
    # Double-checked locking: the fast path is a single read (no lock
    # taken once the client is initialised). Only the first concurrent
    # callers serialise through ``_client_lock`` to ensure exactly one
    # ``Client`` (and therefore one ``TokenBucketRateLimiter``) ever
    # exists per process.
    global client
    if client is None:
        with _client_lock:
            if client is None:
                client = Client()
    return client


def get_fast_client() -> FastClient:
    """Build a fresh :class:`FastClient`.

    Not a singleton by design: Explore requires a per-search session.
    """
    return FastClient()
