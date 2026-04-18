"""Unit tests for ExploreSearchFilters encoding."""

import json
import urllib.parse

import pytest

from fli.models import (
    Airport,
    ExploreLocation,
    ExploreLocationType,
    ExploreSearchFilters,
    MaxStops,
    SeatType,
    TripType,
)


def decode_payload(filters: ExploreSearchFilters) -> list:
    """Round-trip the encoded body through urllib/json back into the nested list."""
    encoded = filters.encode()
    wrapped = json.loads(urllib.parse.unquote(encoded))
    assert wrapped[0] is None
    return json.loads(wrapped[1])


def test_one_way_airport_origin_shape():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport(Airport.JFK)],
        trip_type=TripType.ONE_WAY,
    )
    payload = decode_payload(filters)

    assert payload[0] == []
    assert payload[1] is None
    assert payload[2] is None
    # One-way -> single outbound segment.
    filter_block = payload[3]
    segments = filter_block[13]
    assert len(segments) == 1
    outbound = segments[0]
    assert outbound[0] == [[["JFK", 0]]]
    assert outbound[1] == []
    assert outbound[2] is None
    assert outbound[3] == MaxStops.ANY.value
    # The constant "1" at position [5] is required for full results.
    assert payload[5] == 1
    assert payload[7] == 0
    assert payload[11] == 2


def test_round_trip_has_return_segment():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.city("/m/05qtj")],
        destination=[ExploreLocation.region("/m/02j71")],
        trip_type=TripType.ROUND_TRIP,
    )
    payload = decode_payload(filters)

    segments = payload[3][13]
    assert len(segments) == 2
    outbound, inbound = segments
    assert outbound[0] == [[["/m/05qtj", ExploreLocationType.CITY.value]]]
    assert outbound[1] == [[["/m/02j71", ExploreLocationType.REGION.value]]]
    # Inbound mirrors: destination -> origin.
    assert inbound[0] == outbound[1]
    assert inbound[1] == outbound[0]


def test_date_range_is_forwarded():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport("JFK")],
        from_date="2026-05-01",
        to_date="2026-05-31",
    )
    assert decode_payload(filters)[2] == ["2026-05-01", "2026-05-31"]


def test_trip_duration_is_encoded_as_min_max_pair():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport("JFK")],
        trip_type=TripType.ROUND_TRIP,
        trip_duration=7,
    )
    # Scalar integers at position [4] crash the endpoint with HTTP 400;
    # the payload must use a [min, max] pair instead.
    assert decode_payload(filters)[4] == [7, 7]


def test_multi_city_trip_is_rejected():
    with pytest.raises(ValueError):
        ExploreSearchFilters(
            origin=[ExploreLocation.airport("JFK")],
            trip_type=TripType.MULTI_CITY,
        )


def test_date_range_requires_both_ends():
    with pytest.raises(ValueError):
        ExploreSearchFilters(
            origin=[ExploreLocation.airport("JFK")],
            from_date="2026-05-01",
        )


def test_passenger_and_seat_type_are_forwarded():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport("JFK")],
        seat_type=SeatType.BUSINESS,
    )
    filter_block = decode_payload(filters)[3]
    assert filter_block[5] == SeatType.BUSINESS.value
    assert filter_block[6] == [1, 0, 0, 0]


def test_time_restrictions_packed_into_segment():
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport("JFK")],
        earliest_departure=8,
        latest_departure=20,
    )
    outbound = decode_payload(filters)[3][13][0]
    assert outbound[2] == [8, 20, None, None]
