"""Explore-based flight search implementation.

This module talks to Google Flights' `GetExploreDestinations` endpoint, the one
that powers the destination map on https://www.google.com/travel/explore.

Given an origin, it returns up to a few hundred candidate destinations with the
cheapest dates/prices Google has indexed. It is complementary to
:class:`SearchDates` (which finds the cheapest dates for a fixed route) and
:class:`SearchFlights` (which lists actual itineraries for a specific date).
"""

import json

from fli.models.google_flights.explore import ExploreDestination, ExploreSearchFilters
from fli.search.client import get_client


class SearchExplore:
    """Search for cheap destinations from one or more origins."""

    BASE_URL = (
        "https://www.google.com/_/FlightsFrontendUi/data/"
        "travel.frontend.flights.FlightsFrontendService/GetExploreDestinations"
    )
    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }

    def __init__(self):
        """Initialize the HTTP client used for explore requests."""
        self.client = get_client()

    def search(self, filters: ExploreSearchFilters) -> list[ExploreDestination] | None:
        """Run an Explore query and return the flat list of destinations.

        Args:
            filters: Configured :class:`ExploreSearchFilters` describing the origin,
                trip type, date range, cabin class, etc.

        Returns:
            A list of :class:`ExploreDestination` objects in the order Google
            returned them (typically cheapest/most relevant first), or ``None``
            if the endpoint returned no destinations.

        Raises:
            Exception: If the HTTP request or response parsing fails.

        Notes:
            This endpoint is designed for "destinations from X", not "price for
            a specific X to Y route" — pinning a single arrival airport will
            usually return an empty result. Use :class:`SearchFlights` or
            :class:`SearchDates` when the destination is already known.

        """
        try:
            response = self.client.post(
                url=self.BASE_URL,
                data=f"f.req={filters.encode()}",
                impersonate="chrome",
                allow_redirects=True,
            )
            response.raise_for_status()

            envelope = json.loads(response.text.lstrip(")]}'"))
            wrb = next(
                (row for row in envelope if isinstance(row, list) and row and row[0] == "wrb.fr"),
                None,
            )
            if not wrb or len(wrb) < 3 or not wrb[2]:
                return None

            inner = json.loads(wrb[2])
            groups = inner[3] if len(inner) > 3 and isinstance(inner[3], list) else []

            destinations: list[ExploreDestination] = []
            seen: set[str] = set()
            for group in groups:
                if not isinstance(group, list):
                    continue
                for record in group:
                    parsed = self._parse_destination(record)
                    if parsed is None or parsed.kg_id in seen:
                        continue
                    seen.add(parsed.kg_id)
                    destinations.append(parsed)

            return destinations or None

        except Exception as e:
            raise Exception(f"Explore search failed: {str(e)}") from e

    @staticmethod
    def _parse_destination(record: list) -> ExploreDestination | None:
        """Convert one raw destination record (29-element list) into a model."""
        if not isinstance(record, list) or len(record) < 16:
            return None
        kg_id = record[0]
        name = record[2]
        if not isinstance(kg_id, str) or not isinstance(name, str):
            return None

        coords = record[1] if isinstance(record[1], list) and len(record[1]) >= 2 else (None, None)
        price = record[17] if len(record) > 17 and isinstance(record[17], int | float) else None

        return ExploreDestination(
            kg_id=kg_id,
            name=name,
            country=record[4] if isinstance(record[4], str) else None,
            airport=record[15] if len(record) > 15 and isinstance(record[15], str) else None,
            latitude=coords[0],
            longitude=coords[1],
            departure_date=record[11] if len(record) > 11 and isinstance(record[11], str) else None,
            return_date=record[12] if len(record) > 12 and isinstance(record[12], str) else None,
            price=float(price) if price is not None else None,
            thumbnail_url=record[3] if isinstance(record[3], str) else None,
            is_domestic=record[20] if len(record) > 20 and isinstance(record[20], bool) else None,
        )
