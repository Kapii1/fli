"""Filters and entities for the Google Flights Explore endpoint.

The Explore endpoint (`GetExploreDestinations`) powers the "map of destinations"
view on https://www.google.com/travel/explore. Given an origin, it returns a set
of destinations with the cheapest round-trip or one-way prices Google has seen.
"""

import json
import urllib.parse
from enum import Enum

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, model_validator

from fli.models.airport import Airport
from fli.models.google_flights.base import (
    MaxStops,
    PassengerInfo,
    SeatType,
    TripType,
)


class ExploreLocationType(Enum):
    """Type tag attached to a location in the explore payload.

    Google uses a small integer to disambiguate IATA codes from Knowledge Graph
    IDs. Airports (IATA) use 0, cities use 5, and regions/countries use 6.
    """

    AIRPORT = 0
    CITY = 5
    REGION = 6


class ExploreLocation(BaseModel):
    """A location used to build an Explore request.

    The Explore endpoint accepts either an IATA airport code (e.g. ``"JFK"``) or
    a Google Knowledge Graph ID (e.g. ``"/m/05qtj"`` for Paris, ``"/m/02j71"``
    for New York). Pass the corresponding :class:`ExploreLocationType`.
    """

    code: str
    type: ExploreLocationType = ExploreLocationType.AIRPORT

    @classmethod
    def airport(cls, airport: Airport | str) -> "ExploreLocation":
        """Build a location from an airport enum or IATA code."""
        code = airport.name if isinstance(airport, Airport) else airport
        return cls(code=code, type=ExploreLocationType.AIRPORT)

    @classmethod
    def city(cls, kg_id: str) -> "ExploreLocation":
        """Build a location from a Google Knowledge Graph city id (``/m/...``)."""
        return cls(code=kg_id, type=ExploreLocationType.CITY)

    @classmethod
    def region(cls, kg_id: str) -> "ExploreLocation":
        """Build a location from a Google Knowledge Graph region id (``/m/...``)."""
        return cls(code=kg_id, type=ExploreLocationType.REGION)

    def to_payload(self) -> list:
        """Return the ``[code, type_int]`` pair used in the wire format."""
        return [self.code, self.type.value]


class ExploreSearchFilters(BaseModel):
    """Filters for the Google Flights Explore (GetExploreDestinations) endpoint."""

    origin: list[ExploreLocation]
    destination: list[ExploreLocation] | None = None
    trip_type: TripType = TripType.ONE_WAY
    passenger_info: PassengerInfo = Field(default_factory=PassengerInfo)
    stops: MaxStops = MaxStops.ANY
    seat_type: SeatType = SeatType.ECONOMY
    from_date: str | None = None
    to_date: str | None = None
    trip_duration: PositiveInt | None = None
    earliest_departure: NonNegativeInt | None = None
    latest_departure: PositiveInt | None = None
    earliest_arrival: NonNegativeInt | None = None
    latest_arrival: PositiveInt | None = None

    @model_validator(mode="after")
    def _validate(self) -> "ExploreSearchFilters":
        if not self.origin:
            raise ValueError("At least one origin location is required")
        if self.trip_type == TripType.MULTI_CITY:
            raise ValueError("Explore does not support multi-city trips")
        if (self.from_date is None) != (self.to_date is None):
            raise ValueError("from_date and to_date must be set together")
        return self

    @staticmethod
    def _loc_list(locs: list[ExploreLocation] | None) -> list:
        """Format a location list as ``[[[code, type], ...]]`` or ``[]``."""
        if not locs:
            return []
        return [[loc.to_payload() for loc in locs]]

    def _time_restrictions(self) -> list | None:
        """Pack departure/arrival hour bounds into the 4-tuple the API expects."""
        if all(
            v is None
            for v in (
                self.earliest_departure,
                self.latest_departure,
                self.earliest_arrival,
                self.latest_arrival,
            )
        ):
            return None
        return [
            self.earliest_departure,
            self.latest_departure,
            self.earliest_arrival,
            self.latest_arrival,
        ]

    def _segments(self) -> list:
        """Build the segment array (1 element for one-way, 2 for round-trip)."""
        origin = self._loc_list(self.origin)
        dest = self._loc_list(self.destination)
        time_filter = self._time_restrictions()
        stops = self.stops.value
        outbound = [origin, dest, time_filter, stops]
        if self.trip_type == TripType.ROUND_TRIP:
            return [outbound, [dest, origin, time_filter, stops]]
        return [outbound]

    def _filter_block(self) -> list:
        """Build the 18-element ``filters`` block at payload index [3]."""
        return [
            None,
            None,
            self.trip_type.value,
            None,
            [],
            self.seat_type.value,
            [
                self.passenger_info.adults,
                self.passenger_info.children,
                self.passenger_info.infants_on_lap,
                self.passenger_info.infants_in_seat,
            ],
            None,
            None,
            None,
            None,
            None,
            None,
            self._segments(),
            None,
            None,
            None,
            0,
        ]

    def format(self) -> list:
        """Serialize into the nested list structure sent to Google's API."""
        date_range = (
            [self.from_date, self.to_date] if self.from_date and self.to_date else None
        )
        return [
            [],  # [0] known destinations filter (empty = any)
            None,  # [1] map viewport [[lat_max, lng_max], [lat_min, lng_min]]
            date_range,  # [2] date range [from, to] or null
            self._filter_block(),  # [3] flight filters
            # [4] desired trip length; scalar integers 400 — must be a [min, max]
            # range (or None to let Google pick the best duration per destination)
            [self.trip_duration, self.trip_duration] if self.trip_duration else None,
            1,  # [5] observed constant; setting to trip_type.value starves results
            None,  # [6] unknown
            0,  # [7] sort mode (0 = default/cheapest)
            None,  # [8] unknown
            1,  # [9] observed constant
            [844, 820],  # [10] map viewport pixel size
            2,  # [11] request variant
        ]

    def encode(self) -> str:
        """URL-encode the formatted filters into an ``f.req`` body value."""
        inner = json.dumps(self.format(), separators=(",", ":"))
        return urllib.parse.quote(json.dumps([None, inner], separators=(",", ":")))


class ExploreDestination(BaseModel):
    """A single destination returned by the Explore endpoint."""

    kg_id: str
    name: str
    country: str | None = None
    airport: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    departure_date: str | None = None
    return_date: str | None = None
    price: float | None = None
    thumbnail_url: str | None = None
    is_domestic: bool | None = None
