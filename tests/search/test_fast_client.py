"""Tests for the hedged-request behavior of :class:`FastClient`.

All network access is stubbed: fake sessions simulate fast responses,
slow-serves (sleeps) and transport errors, so these tests assert the racing
logic itself — which attempt wins, how many sessions get created, and how
errors propagate — without touching Google.
"""

import threading
import time

import pytest
from curl_cffi import CurlHttpVersion

from fli.search import client as client_mod
from fli.search.client import FastClient


@pytest.fixture(autouse=True)
def reset_h3_flag():
    """Isolate the class-level h3-availability flag between tests."""
    FastClient._h3_unavailable = False
    yield
    FastClient._h3_unavailable = False


class FakeResponse:
    """Minimal response stub carrying a tag identifying which session made it."""

    def __init__(self, tag: str, status_code: int = 200):
        """Store the originating session tag and HTTP status."""
        self.tag = tag
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP Error {self.status_code}")


class FakeSession:
    """Scripted session: sleeps then returns a response or raises."""

    def __init__(self, tag: str, delay: float = 0.0, error: Exception | None = None):
        """Configure the scripted delay and optional error for this session."""
        self.tag = tag
        self.delay = delay
        self.error = error
        self.closed = False
        self.posted = threading.Event()

    def post(self, url, **kwargs):
        self.posted.set()
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.tag)

    def close(self):
        self.closed = True


@pytest.fixture
def fast_client(monkeypatch):
    """FastClient whose primary session and replacements are scripted.

    Returns (client, replacements) where `replacements` is the list of fake
    sessions handed out by the patched _make_resolved_session, in order.
    Configure them via .script_replacements(...) before calling post().
    """
    monkeypatch.setattr(client_mod, "_make_resolved_session", lambda *a, **k: FakeSession("unused"))
    fc = FastClient.__new__(FastClient)

    handed_out: list[FakeSession] = []
    pending: list[FakeSession] = []

    def fake_factory(*args, **kwargs):
        sess = pending.pop(0) if pending else FakeSession("extra", error=Exception("timed out"))
        handed_out.append(sess)
        return sess

    monkeypatch.setattr(client_mod, "_make_resolved_session", fake_factory)
    monkeypatch.setattr(client_mod, "_next_ip_index", lambda: 1)
    fc.handed_out = handed_out
    fc.pending = pending
    return fc


URL = "https://www.google.com/fake"


def test_fast_primary_response_returns_without_hedging(fast_client):
    fast_client._client = FakeSession("primary", delay=0.0)
    fast_client.HEDGE_DELAY = 0.2

    response = fast_client.post(URL)

    assert response.tag == "primary"
    assert fast_client.handed_out == []  # no hedge session was ever built


def test_slow_primary_loses_race_to_hedge(fast_client):
    fast_client._client = FakeSession("primary", delay=5.0)
    hedge = FakeSession("hedge", delay=0.05)
    fast_client.pending.append(hedge)
    fast_client.HEDGE_DELAY = 0.1

    start = time.perf_counter()
    response = fast_client.post(URL)
    elapsed = time.perf_counter() - start

    assert response.tag == "hedge"
    assert elapsed < 1.0  # did not wait out the slow primary
    hedge.posted.wait(1.0)


def test_primary_wins_when_faster_than_hedge(fast_client):
    fast_client._client = FakeSession("primary", delay=0.3)
    hedge = FakeSession("hedge", delay=5.0)
    fast_client.pending.append(hedge)
    fast_client.HEDGE_DELAY = 0.1

    response = fast_client.post(URL)

    assert response.tag == "primary"


def test_second_hedge_fires_when_first_two_attempts_both_stall(fast_client):
    fast_client._client = FakeSession("primary", delay=5.0)
    fast_client.pending.append(FakeSession("hedge1", delay=5.0))
    hedge2 = FakeSession("hedge2", delay=0.05)
    fast_client.pending.append(hedge2)
    fast_client.HEDGE_DELAY = 0.1

    start = time.perf_counter()
    response = fast_client.post(URL)
    elapsed = time.perf_counter() - start

    assert response.tag == "hedge2"
    assert elapsed < 1.0
    # MAX_ATTEMPTS = 3 → no fourth session even though all timers expired
    assert [s.tag for s in fast_client.handed_out] == ["hedge1", "hedge2"]


def test_retriable_failure_triggers_immediate_replacement(fast_client):
    fast_client._client = FakeSession(
        "primary", error=Exception("curl: (28) QUIC needs at least ...")
    )
    retry = FakeSession("retry", delay=0.05)
    fast_client.pending.append(retry)
    fast_client.HEDGE_DELAY = 10.0  # hedge timer must not be what saves us

    start = time.perf_counter()
    response = fast_client.post(URL)
    elapsed = time.perf_counter() - start

    assert response.tag == "retry"
    assert elapsed < 1.0


def test_non_retriable_failure_raises_immediately(fast_client):
    fast_client._client = FakeSession("primary", error=Exception("HTTP Error 400"))
    fast_client.HEDGE_DELAY = 10.0

    with pytest.raises(Exception, match="POST request failed"):
        fast_client.post(URL)

    assert fast_client.handed_out == []  # no replacement for a 4xx


def test_all_attempts_failing_raises_last_error(fast_client):
    fast_client._client = FakeSession("primary", error=Exception("timed out"))
    fast_client.pending.append(FakeSession("retry1", error=Exception("timed out")))
    fast_client.pending.append(FakeSession("retry2", error=Exception("connection reset")))
    fast_client.HEDGE_DELAY = 10.0

    with pytest.raises(Exception, match="POST request failed"):
        fast_client.post(URL)

    # MAX_ATTEMPTS = 3 → exactly two replacement sessions
    assert [s.tag for s in fast_client.handed_out] == ["retry1", "retry2"]


def test_hedge_failure_still_waits_for_primary(fast_client):
    fast_client._client = FakeSession("primary", delay=0.4)
    fast_client.pending.append(FakeSession("hedge", error=Exception("could not resolve host")))
    fast_client.HEDGE_DELAY = 0.1

    response = fast_client.post(URL)

    assert response.tag == "primary"


def test_replacement_sessions_are_closed(fast_client):
    fast_client._client = FakeSession("primary", delay=5.0)
    hedge = FakeSession("hedge", delay=0.05)
    fast_client.pending.append(hedge)
    fast_client.HEDGE_DELAY = 0.1

    response = fast_client.post(URL)

    assert response.tag == "hedge"
    deadline = time.time() + 1.0
    while not hedge.closed and time.time() < deadline:
        time.sleep(0.01)
    assert hedge.closed


def test_hedging_disabled_falls_back_to_serial_retry(fast_client):
    fast_client._client = FakeSession("primary", error=Exception("timed out"))
    retry = FakeSession("retry", delay=0.0)
    fast_client.pending.append(retry)
    fast_client.HEDGE_DELAY = None

    response = fast_client.post(URL)

    assert response.tag == "retry"


def test_no_new_attempts_after_timeout_budget_spent(fast_client, monkeypatch):
    fast_client._client = FakeSession("primary", delay=0.3, error=Exception("timed out"))
    fast_client.pending.append(FakeSession("retry1", error=Exception("timed out")))
    fast_client.HEDGE_DELAY = 10.0
    monkeypatch.setattr(FastClient, "REQUEST_TIMEOUT", 0.2)

    with pytest.raises(Exception, match="POST request failed"):
        fast_client.post(URL, timeout=0.2)

    # Budget (0.2 s) was already spent when the primary failed at 0.3 s,
    # so no replacement session may be launched.
    assert fast_client.handed_out == []


# ── HTTP/3 → HTTP/2 environment fallback ─────────────────────────────────────


class QuicBrokenSession(FakeSession):
    """Records http_version per post; fails HTTP/3 the way a QUIC-less host does."""

    def __init__(self, tag: str):
        """Start with no versions seen."""
        super().__init__(tag)
        self.versions_seen: list[int] = []

    def post(self, url, http_version=None, **kwargs):
        self.versions_seen.append(http_version)
        if http_version == CurlHttpVersion.V3:
            raise Exception("Failed to perform, curl: (28) QUIC needs at least TLS version 1.3.")
        return FakeResponse(self.tag)


def test_falls_back_to_http2_when_quic_unavailable(fast_client):
    primary = QuicBrokenSession("primary")
    replacement = QuicBrokenSession("replacement")
    fast_client._client = primary
    fast_client.pending.append(replacement)
    fast_client.HEDGE_DELAY = 10.0

    response = fast_client.post(URL)

    # Primary burned the doomed h3 attempt; the immediate replacement was
    # already downgraded to h2 by the class-level flag.
    assert response.tag == "replacement"
    assert primary.versions_seen == [CurlHttpVersion.V3]
    assert replacement.versions_seen == [CurlHttpVersion.V2TLS]
    assert FastClient._h3_unavailable is True


def test_remembers_h3_unavailability_across_instances(fast_client):
    fast_client._client = QuicBrokenSession("primary")
    fast_client.pending.append(QuicBrokenSession("replacement"))
    fast_client.HEDGE_DELAY = 10.0
    fast_client.post(URL)

    second = QuicBrokenSession("second-primary")
    fast_client._client = second
    response = fast_client.post(URL)

    # The second request must skip the doomed HTTP/3 attempt entirely.
    assert response.tag == "second-primary"
    assert second.versions_seen == [CurlHttpVersion.V2TLS]


def test_explicit_http_version_is_respected_and_not_downgraded(fast_client):
    primary = QuicBrokenSession("primary")
    fast_client._client = primary
    fast_client.pending.append(QuicBrokenSession("retry1"))
    fast_client.pending.append(QuicBrokenSession("retry2"))
    fast_client.HEDGE_DELAY = 10.0

    with pytest.raises(Exception, match="QUIC"):
        fast_client.post(URL, http_version=CurlHttpVersion.V3)

    # A forced version is honored on every attempt and never flips the flag.
    assert primary.versions_seen == [CurlHttpVersion.V3]
    assert all(s.versions_seen == [CurlHttpVersion.V3] for s in fast_client.handed_out)
    assert FastClient._h3_unavailable is False


def test_slow_quic_stall_does_not_disable_h3(fast_client, monkeypatch):
    monkeypatch.setattr(FastClient, "_H3_FAIL_FAST_S", 0.05)
    fast_client._client = FakeSession(
        "primary", delay=0.2, error=Exception("curl: (28) QUIC needs at least ...")
    )
    fast_client.pending.append(FakeSession("retry", delay=0.0))
    fast_client.HEDGE_DELAY = 10.0

    response = fast_client.post(URL)

    # The stalled-then-failed h3 attempt was rescued by a replacement, but a
    # slow stall is throttling, not a missing-QUIC environment: keep h3 on.
    assert response.tag == "retry"
    assert FastClient._h3_unavailable is False


def test_non_retriable_errors_fail_fast(fast_client):
    fast_client._client = FakeSession("primary", error=Exception("HTTP Error 403: Forbidden"))
    fast_client.HEDGE_DELAY = 10.0

    with pytest.raises(Exception, match="403"):
        fast_client.post(URL)

    assert fast_client.handed_out == []
