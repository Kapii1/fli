"""Unit tests for the explore_destinations MCP tool (no network)."""

import pytest

import fli.mcp.server as server
from fli.mcp.server import CONFIG, ExploreParams
from fli.mcp.server import _explore_from_params as explore_fn
from fli.models import ExploreDestination, TripType


class _FakeSearchExplore:
    """Stand-in for SearchExplore that records the filters it was given."""

    captured: dict = {}
    results: list[ExploreDestination] | None = []

    def search(self, filters, currency=None, hl=None, gl=None):
        _FakeSearchExplore.captured = {"filters": filters, "currency": currency}
        return _FakeSearchExplore.results


@pytest.fixture(autouse=True)
def fake_search(monkeypatch):
    _FakeSearchExplore.captured = {}
    _FakeSearchExplore.results = [
        ExploreDestination(kg_id="/m/04jpl", name="London", airport="LHR", price=120.0),
        ExploreDestination(kg_id="/m/06c62", name="Rome", airport="FCO", price=80.0),
        ExploreDestination(kg_id="/m/05qtj", name="Paris", airport="CDG", price=None),
    ]
    monkeypatch.setattr(server, "SearchExplore", _FakeSearchExplore)
    return _FakeSearchExplore


def test_specific_round_trip_dates_build_round_trip_filters():
    result = explore_fn(
        ExploreParams(
            origin="JFK",
            departure_date="2026-09-26",
            return_date="2026-10-03",
            currency="EUR",
        )
    )

    assert result["success"] is True
    assert result["trip_type"] == "ROUND_TRIP"
    filters = _FakeSearchExplore.captured["filters"]
    assert filters.trip_type == TripType.ROUND_TRIP
    assert filters.from_date == "2026-09-26"
    assert filters.to_date == "2026-10-03"
    assert filters.trip_duration == 7  # derived from the dates
    assert _FakeSearchExplore.captured["currency"] == "EUR"


def test_single_date_searches_one_way_with_mirrored_to_date():
    result = explore_fn(ExploreParams(origin="JFK", departure_date="2026-09-26"))

    assert result["success"] is True
    assert result["trip_type"] == "ONE_WAY"
    filters = _FakeSearchExplore.captured["filters"]
    assert filters.trip_type == TripType.ONE_WAY
    # The wire format requires both dates; one-way mirrors the departure date.
    assert filters.from_date == filters.to_date == "2026-09-26"


def test_flexible_trip_duration_implies_round_trip():
    result = explore_fn(ExploreParams(origin="JFK", trip_duration=5))

    assert result["success"] is True
    filters = _FakeSearchExplore.captured["filters"]
    assert filters.trip_type == TripType.ROUND_TRIP
    assert filters.from_date is None and filters.to_date is None
    assert filters.trip_duration == 5


def test_no_dates_defaults_to_flexible_one_way():
    explore_fn(ExploreParams(origin="JFK"))
    filters = _FakeSearchExplore.captured["filters"]
    assert filters.trip_type == TripType.ONE_WAY
    assert filters.from_date is None and filters.to_date is None


def test_return_date_without_departure_date_fails():
    result = explore_fn(ExploreParams(origin="JFK", return_date="2026-10-03"))
    assert result["success"] is False
    assert "departure_date" in result["error"]


def test_invalid_airport_fails_cleanly():
    result = explore_fn(ExploreParams(origin="NOPE"))
    assert result["success"] is False
    assert "airport" in result["error"].lower()


def test_sort_by_price_puts_unpriced_destinations_last():
    result = explore_fn(ExploreParams(origin="JFK", sort_by_price=True))

    names = [d["name"] for d in result["destinations"]]
    assert names == ["Rome", "London", "Paris"]


def test_serialization_includes_fallback_currency():
    result = explore_fn(ExploreParams(origin="JFK"))

    assert result["count"] == 3
    rome = next(d for d in result["destinations"] if d["name"] == "Rome")
    assert rome["airport"] == "FCO"
    assert rome["price"] == 80.0
    assert rome["currency"] == CONFIG.default_currency


def test_empty_results_return_success_with_zero_count():
    _FakeSearchExplore.results = None
    result = explore_fn(ExploreParams(origin="JFK"))
    assert result["success"] is True
    assert result["destinations"] == []
    assert result["count"] == 0
