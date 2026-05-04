"""Explore-based flight search implementation.

This module talks to Google Flights' `GetExploreDestinations` endpoint, the one
that powers the destination map on https://www.google.com/travel/explore.

Given an origin, it returns up to a few hundred candidate destinations with the
cheapest dates/prices Google has indexed. It is complementary to
:class:`SearchDates` (which finds the cheapest dates for a fixed route) and
:class:`SearchFlights` (which lists actual itineraries for a specific date).
"""

import json
import urllib.parse

from fli.core.currency import extract_currency_from_price_token
from fli.models.google_flights.explore import (
    ExploreDestination,
    ExploreFlightDetailsFilters,
    ExploreFlightDetailsResult,
    ExploreFlightOffer,
    ExploreSearchFilters,
)
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

    def search(
        self,
        filters: ExploreSearchFilters,
        currency: str | None = None,
        hl: str | None = None,
        gl: str | None = None,
    ) -> list[ExploreDestination] | None:
        """Run an Explore query and return the flat list of destinations.

        Args:
            filters: Configured :class:`ExploreSearchFilters` describing the origin,
                trip type, date range, cabin class, etc.
            currency: Optional 3-letter currency code forwarded as ``?curr=`` so
                Google returns prices in that currency instead of choosing one
                from the egress IP / locale.
            hl: Optional UI language code (e.g. ``"en"``).
            gl: Optional country / region code (e.g. ``"US"``); influences which
                results Google considers for the explore destination set.

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
            url = self.BASE_URL
            params: dict[str, str] = {}
            if currency:
                params["curr"] = currency.upper()
            if hl:
                params["hl"] = hl
            if gl:
                params["gl"] = gl
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            response = self.client.post(
                url=url,
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
            booking_token = r1[1] if len(r1) > 1 and isinstance(r1[1], str) else None
            currency = extract_currency_from_price_token(booking_token)
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
                currency=currency,
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
            booking_token = r1[1] if len(r1) > 1 and isinstance(r1[1], str) else None
            if booking_token and not dest.currency:
                parsed_currency = extract_currency_from_price_token(booking_token)
                if parsed_currency:
                    dest.currency = parsed_currency

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


class SearchExploreDetails:
    """Per-destination flight detail search.

    Wraps Google Flights' ``GetExploreDestinationFlightDetails`` endpoint —
    the request that fires when a user clicks a destination card on the
    explore map and the panel populates with actual airline / time / price
    options.

    Wire shape (reverse-engineered from the FlightsFrontendUi build):

    * Request type ``_.Ied`` exposes one decoded field via ``getConstraints()``
      — the same 18-element ``_.Kr`` filter block used by
      ``GetExploreDestinations``. Outer wire array is ``[null, filter_block]``.
    * Response type ``_.Ced`` (proto tag ``j33LJc``) is a thin wrapper with one
      field reading a ``_.Qr`` payload at field 1; each streamed frame contains
      one such wrapper.

    The endpoint is HTTP server-streaming, but in practice we observe the
    full result delivered in a single ``wrb.fr`` frame followed by the usual
    ``di`` / ``af.httprm`` / ``e`` housekeeping frames.
    """

    BASE_URL = (
        "https://www.google.com/_/FlightsFrontendUi/data/"
        "travel.frontend.flights.FlightsFrontendService/GetExploreDestinationFlightDetails"
    )
    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }

    def __init__(self):
        self.client = get_fast_client()

    def search(
        self,
        filters: ExploreFlightDetailsFilters,
        currency: str | None = None,
        hl: str | None = None,
        gl: str | None = None,
    ) -> ExploreFlightDetailsResult | None:
        """Run a FlightDetails query and return the parsed result."""
        try:
            url = self.BASE_URL
            params: dict[str, str] = {}
            if currency:
                params["curr"] = currency.upper()
            if hl:
                params["hl"] = hl
            if gl:
                params["gl"] = gl
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            response = self.client.post(
                url=url,
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

            result = ExploreFlightDetailsResult()
            for wrb in wrb_entries:
                if len(wrb) < 3 or not wrb[2]:
                    continue
                inner = json.loads(wrb[2])
                self._merge_payload(inner, result)
            return result if result.offers or result.session_id else None

        except Exception as e:
            raise Exception(f"FlightDetails search failed: {str(e)}") from e

    @staticmethod
    def _merge_payload(payload: list, result: ExploreFlightDetailsResult) -> None:
        """Merge one decoded ``_.Qr`` payload into the accumulating result.

        Wire shape (JSPB array index → proto field number − 1):

        * ``[0]``  field 1 — session block: ``[None, [reqId,...], 0, session_id, cursor_token]``
        * ``[1]``  field 2 — repeated FlightOffer
        * ``[2]``  field 3 — date range ``[departure_date, return_date]``
        * ``[4]``  field 5 — price-chart summary; trailing string is the
                              destination city display name
        * ``[10]`` field 11 — chunk metadata
        """
        if not isinstance(payload, list) or not payload:
            return

        session_block = payload[0] if len(payload) > 0 else None
        if isinstance(session_block, list):
            if len(session_block) > 3 and isinstance(session_block[3], str):
                result.session_id = result.session_id or session_block[3]
            if len(session_block) > 4 and isinstance(session_block[4], str):
                result.cursor_token = result.cursor_token or session_block[4]

        offers_block = payload[1] if len(payload) > 1 else None
        if isinstance(offers_block, list):
            for record in offers_block:
                offer = SearchExploreDetails._parse_offer(record)
                if offer is not None:
                    result.offers.append(offer)

        date_range = payload[2] if len(payload) > 2 else None
        if isinstance(date_range, list):
            if len(date_range) > 0 and isinstance(date_range[0], str):
                result.departure_date = date_range[0]
            if len(date_range) > 1 and isinstance(date_range[1], str):
                result.return_date = date_range[1]

        chart = payload[4] if len(payload) > 4 else None
        if isinstance(chart, list):
            prices: list[float | None] = []
            for entry in chart:
                # Each `[null, value]` pair encodes one price-chart point.
                if (
                    isinstance(entry, list)
                    and len(entry) > 1
                    and isinstance(entry[1], (int, float))
                    and not isinstance(entry[1], bool)
                ):
                    prices.append(float(entry[1]))
            if prices:
                result.price_chart = prices
            for entry in reversed(chart):
                if isinstance(entry, str):
                    result.destination_name = entry
                    break

    @staticmethod
    def _parse_offer(record: list) -> ExploreFlightOffer | None:
        """Decode one 20-field FlightOffer record.

        Field map (JSPB array index → meaning), from a captured live response:

        * [0]  ``[[null, price], booking_token]``
        * [1]  airline IATA code
        * [2]  airline display name
        * [3]  stops count
        * [4]  duration minutes
        * [5]  is_best bool (set on the highlighted offer; null otherwise)
        * [6]  departure date (``YYYY-MM-DD``)
        * [7]  origin airport IATA
        * [8]  origin airport display name
        * [9]  destination airport IATA
        * [10] destination airport display name
        * [13] origin city KG id (``/m/...``)
        """
        if not isinstance(record, list) or len(record) < 11:
            return None

        price: float | None = None
        currency: str | None = None
        booking_token: str | None = None
        r0 = record[0]
        if isinstance(r0, list) and r0:
            price_block = r0[0]
            if (
                isinstance(price_block, list)
                and len(price_block) > 1
                and isinstance(price_block[1], (int, float))
                and not isinstance(price_block[1], bool)
            ):
                price = float(price_block[1])
            if len(r0) > 1 and isinstance(r0[1], str):
                booking_token = r0[1]
                currency = extract_currency_from_price_token(booking_token)

        def _str(idx: int) -> str | None:
            return record[idx] if idx < len(record) and isinstance(record[idx], str) else None

        def _int(idx: int) -> int | None:
            v = record[idx] if idx < len(record) else None
            return int(v) if isinstance(v, int) and not isinstance(v, bool) else None

        is_best = record[5] if len(record) > 5 and isinstance(record[5], bool) else None

        return ExploreFlightOffer(
            price=price,
            currency=currency,
            booking_token=booking_token,
            airline_code=_str(1),
            airline_name=_str(2),
            stops=_int(3),
            duration_minutes=_int(4),
            is_best=is_best,
            departure_date=_str(6),
            origin_airport=_str(7),
            origin_airport_name=_str(8),
            destination_airport=_str(9),
            destination_airport_name=_str(10),
            origin_city_kg_id=_str(13),
        )
