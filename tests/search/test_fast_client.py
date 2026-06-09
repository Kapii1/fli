"""Unit tests for FastClient's HTTP/3 → HTTP/2 fallback (no network)."""

import pytest
from curl_cffi import CurlHttpVersion

from fli.search.client import FastClient


class _FakeResponse:
    def raise_for_status(self):
        pass


class _QuicBrokenSession:
    """Fails HTTP/3 posts the way curl does on hosts without QUIC support."""

    def __init__(self):
        self.versions_seen: list[int] = []

    def post(self, url, http_version=None, **kwargs):
        self.versions_seen.append(http_version)
        if http_version == CurlHttpVersion.V3:
            raise Exception("Failed to perform, curl: (28) QUIC needs at least TLS version 1.3.")
        return _FakeResponse()


@pytest.fixture(autouse=True)
def reset_h3_flag():
    FastClient._h3_unavailable = False
    yield
    FastClient._h3_unavailable = False


def _make_client(session) -> FastClient:
    client = FastClient.__new__(FastClient)  # skip DoH resolution
    client._client = session
    return client


def test_falls_back_to_http2_when_quic_unavailable():
    session = _QuicBrokenSession()
    client = _make_client(session)

    response = client.post("https://www.google.com/test")

    assert isinstance(response, _FakeResponse)
    assert session.versions_seen == [CurlHttpVersion.V3, CurlHttpVersion.V2TLS]
    assert FastClient._h3_unavailable is True


def test_remembers_h3_unavailability_across_instances():
    first = _make_client(_QuicBrokenSession())
    first.post("https://www.google.com/test")

    session = _QuicBrokenSession()
    second = _make_client(session)
    second.post("https://www.google.com/test")

    # The second client must skip the doomed HTTP/3 attempt entirely.
    assert session.versions_seen == [CurlHttpVersion.V2TLS]


def test_explicit_http_version_is_respected_and_not_downgraded():
    session = _QuicBrokenSession()
    client = _make_client(session)

    with pytest.raises(Exception, match="QUIC"):
        client.post("https://www.google.com/test", http_version=CurlHttpVersion.V3)

    assert session.versions_seen == [CurlHttpVersion.V3]
    assert FastClient._h3_unavailable is False


def test_non_retriable_errors_fail_fast():
    class _AuthFailSession:
        def __init__(self):
            self.calls = 0

        def post(self, url, http_version=None, **kwargs):
            self.calls += 1
            raise Exception("HTTP Error 403: Forbidden")

    session = _AuthFailSession()
    client = _make_client(session)

    with pytest.raises(Exception, match="403"):
        client.post("https://www.google.com/test")

    assert session.calls == 1
