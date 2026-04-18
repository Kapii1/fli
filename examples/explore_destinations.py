#!/usr/bin/env python3
"""Explore destinations example.

Given an origin, ask Google Flights for a map of destinations with their
cheapest dates and prices — the same data that powers
https://www.google.com/travel/explore.
"""

from fli.models import (
    Airport,
    ExploreLocation,
    ExploreSearchFilters,
    MaxStops,
    SeatType,
    TripType,
)
from fli.search import SearchExplore


def main():
    # One-way from JFK to anywhere.
    filters = ExploreSearchFilters(
        origin=[ExploreLocation.airport(Airport.JFK)],
        trip_type=TripType.ONE_WAY,
        seat_type=SeatType.ECONOMY,
        stops=MaxStops.ANY,
    )

    search = SearchExplore()
    results = search.search(filters) or []

    # Only destinations with prices are actionable; sort cheapest first.
    priced = [d for d in results if d.price is not None]
    priced.sort(key=lambda d: d.price)

    print(f"Got {len(results)} destinations ({len(priced)} with prices).")
    for d in priced[:20]:
        print(f"  {d.name:<24} ({d.airport or '?':<3})  ${d.price:>6.0f}  dep {d.departure_date}")


if __name__ == "__main__":
    main()
