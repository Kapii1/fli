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
from fli.search.client import get_fast_client


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
        """Initialize the HTTP client used for explore requests.

        Uses :class:`FastClient` (HTTP/3 + DoH + chrome133a) by default — the
        Explore endpoint is extremely sensitive to session reuse and transport,
        and the fast client measures an order of magnitude faster than the
        default :class:`Client` on this path. Callers can still override with
        ``inst.client = ...`` for testing or custom transports.
        """
        self.client = get_fast_client()

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
                allow_redirects=False,
            )
            response.raise_for_status()

            envelope = json.loads(response.content.lstrip(b")]}'"))
            wrb_entries = [
                row for row in envelope if isinstance(row, list) and row and row[0] == "wrb.fr"
            ]
            if not wrb_entries:
                return None

            seen: dict[str, ExploreDestination] = {}
            for wrb in wrb_entries:
                if len(wrb) < 3 or not wrb[2]:
                    continue
                inner = json.loads(wrb[2])

                # inner[3] → response field 4 (`_.Ws`): primary destination list.
                # inner[4] → response field 5 (`y0d`): streaming price/offer updates
                #           keyed on the same kg_id. Parse these last so they
                #           enrich already-seen records.
                primary_block = inner[3] if len(inner) > 3 and isinstance(inner[3], list) else None
                if primary_block:
                    for group in primary_block:
                        if not isinstance(group, list):
                            continue
                        for record in group:
                            parsed = self._parse_destination(
                                record, filters.from_date, filters.to_date
                            )
                            if parsed is None:
                                continue
                            existing = seen.get(parsed.kg_id)
                            if existing is None:
                                seen[parsed.kg_id] = parsed
                            else:
                                self._merge(existing, parsed)

                updates_block = inner[4] if len(inner) > 4 and isinstance(inner[4], list) else None
                if updates_block:
                    for group in updates_block:
                        if not isinstance(group, list):
                            continue
                        for record in group:
                            self._apply_update(record, seen)

            destinations = list(seen.values())

            return destinations or None

        except Exception as e:
            raise Exception(f"Explore search failed: {str(e)}") from e

    @staticmethod
    def _parse_destination(
        record: list,
        departure_date: str | None = None,
        return_date: str | None = None,
    ) -> ExploreDestination | None:
        """Convert one raw destination record into a model.

        Handles two wire formats emitted by GetExploreDestinations:
        - Cheapest-date mode (29-element): record[1]=[lat,lon], record[2]=name,
          record[17]=price
        - Specific-date mode (16-element): record[1]=[[null,price],token],
          record[6]=[..., airport, kg, name, ...]
        """
        if not isinstance(record, list) or len(record) < 7:
            return None
        kg_id = record[0]
        if not isinstance(kg_id, str):
            return None

        r1 = record[1]
        # Specific-date format: record[1] is [[null, price], booking_token]
        if isinstance(r1, list) and r1 and isinstance(r1[0], list):
            price_block = r1[0]
            price = price_block[1] if len(price_block) > 1 and isinstance(price_block[1], (int, float)) else None
            dest_info = record[6] if len(record) > 6 and isinstance(record[6], list) else []
            airport = dest_info[5] if len(dest_info) > 5 and isinstance(dest_info[5], str) else None
            name = dest_info[7] if len(dest_info) > 7 and isinstance(dest_info[7], str) else airport
            if not isinstance(name, str):
                return None
            is_domestic = record[9] if len(record) > 9 and isinstance(record[9], bool) else None
            return ExploreDestination(
                kg_id=kg_id,
                name=name,
                country=None,
                airport=airport,
                latitude=None,
                longitude=None,
                departure_date=departure_date,
                return_date=return_date,
                price=float(price) if price is not None else None,
                thumbnail_url=None,
                is_domestic=is_domestic,
            )

        # Cheapest-date format. Indices below are proto_field_number - 1
        # (JSPB lays out field N at array index N-1). Cross-references are to
        # the FlightsFrontendUi `F0d` decoder, which is the canonical proof of
        # wire shape. Only fields F0d actually reads are decoded here.
        #
        #   [0]  proto 1  = kg_id                       (_.Qj)
        #   [1]  proto 2  = [lat, lng]                  (_.w _.fn)
        #   [2]  display name (from getName() accessor; not in F0d field list)
        #   [3]  proto 4  = primary photo url           (_.nl)
        #   [6]  proto 7  = connected enum (==2 ⇒ true) (_.Gl)
        #   [8]  proto 9  = string                       (_.nl, out 48)
        #   [14] proto 15 = has_full_trip_quote bool     (_.ll, out 35)
        #   [15] proto 16 = string                       (_.nl, out 30)
        #   [16] proto 17 = price float                  (_.ml, out 34)
        #   [17] proto 18 = duration minutes float       (_.ml, out 37)
        #   [19] proto 20 = string                       (_.nl, out 40)
        #   [20] proto 21 = noteworthy bool              (_.ll, out 46)
        #   [21] proto 22 = region subtitle string       (_.nl → lr.4)
        #   [26] proto 27 = secondary subtitle string    (_.nl → lr.5)
        #   [27] proto 28 = string                       (_.nl, out 49)
        #
        # Left as speculative (legacy, not proven by F0d): country at [4],
        # airport at [15], departure_date at [11], return_date at [12],
        # is_domestic at [20]. These are what the existing parser assumed;
        # preserved for backward compatibility but may be wrong on new
        # response shapes.
        if len(record) < 16:
            return None
        name = record[2]
        if not isinstance(name, str):
            return None
        coords = r1 if isinstance(r1, list) and len(r1) >= 2 else (None, None)
        return ExploreDestination(
            kg_id=kg_id,
            name=name,
            country=record[4] if len(record) > 4 and isinstance(record[4], str) else None,
            airport=record[15] if len(record) > 15 and isinstance(record[15], str) else None,
            latitude=coords[0],
            longitude=coords[1],
            departure_date=record[11] if len(record) > 11 and isinstance(record[11], str) else None,
            return_date=record[12] if len(record) > 12 and isinstance(record[12], str) else None,
            price=SearchExplore._num(record, 16),
            duration_minutes=SearchExplore._num(record, 17),
            thumbnail_url=record[3] if len(record) > 3 and isinstance(record[3], str) else None,
            is_domestic=record[20] if len(record) > 20 and isinstance(record[20], bool) else None,
            noteworthy=record[20] if len(record) > 20 and isinstance(record[20], bool) else None,
            connected=(record[6] == 2) if len(record) > 6 else None,
            subtitle=record[26] if len(record) > 26 and isinstance(record[26], str) else None,
        )

    @staticmethod
    def _num(record: list, idx: int) -> float | None:
        """Return ``record[idx]`` as a float if it's numeric, else ``None``."""
        if idx >= len(record):
            return None
        v = record[idx]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return None

    @staticmethod
    def _merge(into: ExploreDestination, other: ExploreDestination) -> None:
        """Fill empty fields on ``into`` from ``other`` (non-destructive)."""
        for field_name in other.model_fields:
            if getattr(into, field_name) in (None, "") and getattr(other, field_name) not in (
                None,
                "",
            ):
                setattr(into, field_name, getattr(other, field_name))

    @staticmethod
    def _apply_update(record: list, seen: dict[str, ExploreDestination]) -> None:
        """Merge a `y0d → w0d` streaming update into the matching destination.

        Empirically verified wire format (reverse-engineered):

        record layout:
            [0]  kg_id  (string, matches primary-block kg_id)
            [1]  [[null, round_trip_price], booking_token]  — price-block format
            [2]  noteworthy bool
            [6]  trip_detail list (v0d):
                     [0]  airline IATA code (e.g. 'AZ')
                     [1]  airline name (e.g. 'ITA')
                     [3]  flight duration minutes (outbound)
                     [5]  destination airport IATA  ← NOT currency (doc was wrong)
                     [6]  origin city/region KG id
                     [8]  one-way outbound price (NOT the round-trip total)
            [10] connected_enum (==1 → connected)
            [16] display price (float, sometimes set, sometimes absent)

        The correct round-trip price is at record[1][0][1] (price-block),
        identical to the format _parse_destination uses for specific-date records.
        record[6][8] is a one-way or partial price — do NOT use it as the total.
        """
        if not isinstance(record, list) or not record:
            return
        kg_id = record[0]
        if not isinstance(kg_id, str):
            return
        dest = seen.get(kg_id)
        if dest is None:
            return

        noteworthy = record[2] if len(record) > 2 else None
        if isinstance(noteworthy, bool) and noteworthy:
            dest.noteworthy = True

        if len(record) > 10 and record[10] == 1:
            dest.connected = True

        # Primary price source: record[1] = [[null, price], booking_token]
        # This is the same price-block format used by _parse_destination for
        # specific-date records, and contains the correct round-trip total.
        r1 = record[1] if len(record) > 1 else None
        if isinstance(r1, list) and r1 and isinstance(r1[0], list):
            price_block = r1[0]
            pb_price = (
                float(price_block[1])
                if len(price_block) > 1 and isinstance(price_block[1], (int, float))
                   and not isinstance(price_block[1], bool)
                   and price_block[1] > 0
                else None
            )
            if pb_price is not None:
                dest.price = pb_price

        # Fallback: record[16] display price (present in some response shapes).
        display_price = SearchExplore._num(record, 16)
        if display_price is not None and display_price > 0:
            # Only use display price if we didn't get a price-block price,
            # or if it's higher (price-block tends to be more accurate).
            if dest.price is None:
                dest.price = display_price

        # record[6] is the trip_detail block.  We extract the destination
        # airport from v0d[5] (NOT the currency — field [5] is the IATA code
        # of the destination airport).  We deliberately skip v0d[8] because
        # it is a one-way segment price, not the round-trip total.
        detail = record[6] if len(record) > 6 and isinstance(record[6], list) else None
        if detail:
            # v0d[5] = destination airport IATA (e.g. 'FCO')
            dest_iata = detail[5] if len(detail) > 5 and isinstance(detail[5], str) else None
            if dest_iata and not dest.airport:
                dest.airport = dest_iata
