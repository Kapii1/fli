"""Flight search implementation.

This module talks to Google Flights' ``GetShoppingResults`` endpoint — the one
that powers the main search results page on https://www.google.com/travel/flights.

Response envelope (top-level ``_.Qz`` fields, decoded from the FlightsFrontendUi
client JS — specifically ``dtf.next`` / ``dtf.error`` / ``dtf.complete``):

    [0]  proto 1  = bool — terminal error flag
    [6]  proto 7  = _.Qz recursive — "best" tab body (carries slice list)
    [7]  proto 8  = repeated _.Or — per-segment result slices
    [11] proto 12 = _.Rz echo (outbound state for inbound continuation)
    [12] proto 13 = _.Qz recursive — "cheapest" tab body
    [13] proto 14 = sort-change / error marker
    [14] proto 15 = _.t9 continue-from-cache state
    [15] proto 16 = _.OW error detail (see ``ShoppingError``)
    [16] proto 17 = error code for cheapest tab

Error codes (``_.OW`` wire values — see ``dtf.error`` + the ``ctf`` mapper):

    0 = ok, 1 = HTTP, 2 = auth, 3 = timeout/network,
    5 = geographic restriction, 6 = invalid date, 7 = invalid airport combo

The per-flight array indices (``data[0][9]`` etc. in ``_parse_flights_data``)
are preserved verbatim — they're empirically derived and the JS chunk that
proves them (a ``F0d``-like decoder) lives in a different module than the one
this endpoint map was derived from. Don't tweak them without a live capture.
"""

import json
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from fli.core import extract_currency_from_price_token
from fli.models import (
    Airline,
    Airport,
    FlightLeg,
    FlightResult,
    FlightSearchFilters,
)
from fli.models.google_flights.base import TripType
from fli.search.client import get_fast_client


# ---------------------------------------------------------------------------
# Response-envelope error shape (top-level fields on `_.Qz`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShoppingError:
    """Decoded top-level error from a ``GetShoppingResults`` response.

    Mirrors the ``_.OW`` submessage at response field 16 (index 15).
    ``code`` semantics come from ``dtf.error`` + the ``ctf`` mapper in the JS.
    """

    code: int
    message: str | None = None

    @property
    def is_fatal(self) -> bool:
        """True for errors that won't retry — auth, geo, invalid inputs."""
        return self.code in (2, 5, 6, 7)


class SearchFlights:
    """Flight search implementation using Google Flights' API.

    This class handles searching for specific flights with detailed filters,
    parsing the results into structured data models.
    """

    BASE_URL = (
        "https://www.google.com/_/FlightsFrontendUi/data/"
        "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
    )
    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }

    # Top-level response field → decoded-array index (see module docstring).
    _IDX_BEST_TAB = 6
    _IDX_CHEAPEST_TAB = 12
    _IDX_ERROR_DETAIL = 15

    def __init__(self):
        """Initialize the search client for flight searches.

        Uses :class:`FastClient` (HTTP/3 + DoH + chrome133a) per-search —
        ``GetShoppingResults`` slow-serves reused sessions the same way the
        Explore endpoint does. Override with ``inst.client = ...`` for tests
        or a custom transport.
        """
        self.client = get_fast_client()

    def search(
        self, filters: FlightSearchFilters, top_n: int = 5
    ) -> list[FlightResult | tuple[FlightResult, ...]] | None:
        """Search for flights using the given FlightSearchFilters.

        Args:
            filters: Full flight search object including airports, dates, and preferences
            top_n: Number of flights to limit the return flight search to

        Returns:
            List of FlightResult objects (one-way), tuples of FlightResult (round-trip
            or multi-city), or None if no results

        Raises:
            Exception: If the search fails or returns invalid data. For known
                server-side errors (auth, geographic restriction, invalid
                dates/airports), the exception message includes the decoded
                ``ShoppingError.code`` for programmatic handling.

        Note:
            Multi-city searches (TripType.MULTI_CITY) with distinct city pairs may
            time out due to limitations of the Google Flights API endpoint.  The
            endpoint reliably supports one-way and round-trip searches.

        """
        encoded_filters = filters.encode()

        try:
            # Don't pass `impersonate=` — the session is already impersonated
            # as chrome133a at construction time, and a per-call override
            # forces curl_cffi to tear down + rebuild the connection, which
            # breaks HTTP/3 and keep-alive reuse.
            response = self.client.post(
                url=self.BASE_URL,
                data=f"f.req={encoded_filters}",
                allow_redirects=False,
            )
            response.raise_for_status()

            flights = self._parse_response(response.text)
            if not flights:
                return None

            if filters.trip_type == TripType.ONE_WAY:
                return flights

            # For round-trip and multi-city, iteratively select each leg
            # and fetch the next leg's options with combined pricing.
            num_segments = len(filters.flight_segments)
            selected_count = sum(
                1 for s in filters.flight_segments if s.selected_flight is not None
            )

            # If all previous segments are selected, we're on the last leg
            if selected_count >= num_segments - 1:
                return flights

            # Select each flight option and fetch the next leg
            flight_combos = []
            for selected_flight in flights[:top_n]:
                next_filters = deepcopy(filters)
                next_filters.flight_segments[selected_count].selected_flight = selected_flight
                next_results = self.search(next_filters, top_n=top_n)
                if next_results is not None:
                    for next_result in next_results:
                        if isinstance(next_result, tuple):
                            flight_combos.append((selected_flight,) + next_result)
                        else:
                            flight_combos.append((selected_flight, next_result))

            return flight_combos

        except Exception as e:
            raise Exception(f"Search failed: {str(e)}") from e

    # ------------------------------------------------------------------
    # Response envelope walk
    # ------------------------------------------------------------------

    def _parse_response(self, text: str) -> list[FlightResult]:
        """Walk every ``wrb.fr`` chunk in a streaming response and collect flights.

        The Explore endpoint returns a single chunk in practice; shopping is
        genuinely streaming — the cheapest-tab body often arrives in a later
        chunk than the best-tab one, and round-trip responses push an inbound
        update after the outbound. Reading only ``[0]`` silently drops results.
        """
        # NOTE: we previously inspected ``inner[15]`` as a decoded ``_.OW``
        # error submessage, but that index carries unrelated metadata on
        # successful responses (observed ``[2, ['SunExpress']]`` alongside
        # 5 real flight rows on DUB→CDG). Until the real error layout is
        # pinned down, trust the flight rows and let callers treat an
        # empty result list as "no flights found".
        seen_keys: set[tuple] = set()
        flights: list[FlightResult] = []

        for inner in self._iter_wrb_payloads(text):
            for row in self._iter_flight_rows(inner):
                parsed = self._parse_flights_data(row)
                # Dedup across chunks: the same itinerary can surface in both
                # best and cheapest tabs. Key on the legs' signature + price.
                key = self._dedup_key(parsed)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                flights.append(parsed)

        return flights

    @staticmethod
    def _iter_wrb_payloads(text: str) -> Iterable[list]:
        """Yield the decoded inner arrays from every ``wrb.fr`` entry.

        Handles both compact (``rt=c``) responses — one outer array — and the
        line-delimited streaming format where each line carries one chunk.
        """
        body = text.lstrip(")]}'").strip()
        try:
            envelope = json.loads(body)
            entries = envelope
        except json.JSONDecodeError:
            entries = []
            for raw in body.splitlines():
                raw = raw.strip()
                if not raw or raw[0] not in "[{":
                    continue
                try:
                    entries.extend(json.loads(raw))
                except json.JSONDecodeError:
                    continue

        for entry in entries:
            if not (isinstance(entry, list) and entry and entry[0] == "wrb.fr"):
                continue
            payload = entry[2] if len(entry) > 2 else None
            if not payload:
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue

    @classmethod
    def _iter_flight_rows(cls, inner: list) -> Iterable[list]:
        """Yield raw flight rows from both tabs of a response chunk.

        Historically this parser read ``inner[2]`` and ``inner[3]`` — those
        are per-segment result groups inside an ``_.Or`` message, which is
        what the server returns for single-segment/single-chunk responses.
        For streaming responses we also check the top-level "best" and
        "cheapest" tab recursions (``_.Qz`` at fields 7 and 13) in case the
        outer envelope is nested.
        """
        # Historical path: slice groups at inner[2]/inner[3]. Each group is
        # a list of result rows.
        for i in (2, 3):
            if len(inner) > i and isinstance(inner[i], list) and inner[i]:
                first = inner[i][0] if isinstance(inner[i][0], list) else None
                if first:
                    yield from first

        # Streaming path: best/cheapest tabs carry a recursive _.Qz. Peek one
        # level deeper if the historical path came up empty for that tab.
        for idx in (cls._IDX_BEST_TAB, cls._IDX_CHEAPEST_TAB):
            nested = inner[idx] if len(inner) > idx else None
            if not isinstance(nested, list):
                continue
            for i in (2, 3):
                if len(nested) > i and isinstance(nested[i], list) and nested[i]:
                    first = nested[i][0] if isinstance(nested[i][0], list) else None
                    if first:
                        yield from first

    @classmethod
    def _extract_error(cls, inner: list) -> ShoppingError | None:
        """Pull a typed error off the response envelope if one is present.

        Looks at the terminal flag at index 0 and the ``_.OW`` error detail
        at index 15. Returns ``None`` if no error is surfaced in this chunk.
        """
        terminal = inner[0] if inner else None
        detail = inner[cls._IDX_ERROR_DETAIL] if len(inner) > cls._IDX_ERROR_DETAIL else None

        if not terminal and not isinstance(detail, list):
            return None

        code = 0
        message: str | None = None
        if isinstance(detail, list) and detail:
            # _.OW layout (0-indexed):
            #   [0] variant tag: 1=http, 2=auth, 3=other; [1] payload w/ message
            tag = detail[0] if detail else None
            if isinstance(tag, int):
                code = tag
            payload = detail[1] if len(detail) > 1 and isinstance(detail[1], list) else None
            if payload:
                for item in payload:
                    if isinstance(item, str) and item:
                        message = item
                        break

        if code == 0 and terminal:
            # Terminal flag set with no _.OW detail — generic failure.
            code = 1
        return ShoppingError(code=code, message=message)

    @staticmethod
    def _dedup_key(flight: FlightResult) -> tuple:
        """Stable key for a parsed flight, used to dedup across stream chunks.

        Prefer the booking token — it's a unique id per offer. Fall back to
        leg signature + price for rows that don't carry one.
        """
        if flight.booking_token:
            return ("tok", flight.booking_token)
        return (
            "legs",
            tuple(
                (
                    leg.airline.name,
                    leg.flight_number,
                    leg.departure_airport.name,
                    leg.arrival_airport.name,
                    leg.departure_datetime.isoformat(),
                )
                for leg in flight.legs
            ),
            round(flight.price, 2),
            flight.currency or "",
        )

    # ------------------------------------------------------------------
    # Per-row decoder (empirically-verified indices — do not edit without
    # a live capture to diff against).
    # ------------------------------------------------------------------

    # Direct booking deep-link base. The `tfs` query param accepts the
    # per-flight booking token from `data[1][1]` verbatim — the token
    # already encodes the flight id, fare class, and currency.
    _BOOKING_URL_BASE = "https://www.google.com/travel/flights/booking"

    @staticmethod
    def _parse_flights_data(data: list) -> FlightResult:
        """Parse raw flight data into a structured FlightResult.

        Args:
            data: Raw flight data from the API response

        Returns:
            Structured FlightResult object with all flight details

        """
        price, currency = SearchFlights._parse_price_info(data)
        token = SearchFlights._parse_booking_token(data)
        booking_url = SearchFlights.build_booking_url(token, currency) if token else None
        flight = FlightResult(
            price=price,
            currency=currency,
            duration=data[0][9],
            stops=len(data[0][2]) - 1,
            booking_token=token,
            booking_url=booking_url,
            legs=[
                FlightLeg(
                    airline=SearchFlights._parse_airline(fl[22][0]),
                    flight_number=fl[22][1],
                    departure_airport=SearchFlights._parse_airport(fl[3]),
                    arrival_airport=SearchFlights._parse_airport(fl[6]),
                    departure_datetime=SearchFlights._parse_datetime(fl[20], fl[8]),
                    arrival_datetime=SearchFlights._parse_datetime(fl[21], fl[10]),
                    duration=fl[11],
                )
                for fl in data[0][2]
            ],
        )
        return flight

    @staticmethod
    def _parse_booking_token(data: list) -> str | None:
        """Return the raw booking token at ``data[1][1]``, if present.

        The token is the second element of the price block — a base64-encoded
        protobuf carrying the flight id, fare details, and currency. Feeding
        it back as the ``tfs`` query param resolves the final booking page.
        """
        price_block = SearchFlights._get_price_block(data)
        if not price_block or len(price_block) < 2:
            return None
        token = price_block[1]
        return token if isinstance(token, str) and token else None

    @classmethod
    def build_booking_url(
        cls,
        token: str,
        currency: str | None = None,
        hl: str = "en",
    ) -> str:
        """Build a deep-link to Google Flights' booking page for ``token``.

        Args:
            token: The per-flight booking token from ``FlightResult.booking_token``.
            currency: Optional 3-letter currency code to force — otherwise
                Google picks from the token / IP. Passing the same currency
                as the response keeps displayed prices consistent.
            hl: UI language code.

        Returns:
            A full ``https://www.google.com/travel/flights/booking?...`` URL.

        Note:
            Tokens are short-lived (~15 min). The URL must be opened soon
            after the search; stale tokens land on a "session expired" page.
        """
        params: dict[str, str] = {"tfs": token, "hl": hl}
        if currency:
            params["curr"] = currency
        return f"{cls._BOOKING_URL_BASE}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def _parse_price_info(data: list) -> tuple[float, str | None]:
        """Extract the numeric price and returned currency from raw flight data."""
        price_block = SearchFlights._get_price_block(data)
        price = 0.0
        currency = None
        try:
            if price_block and price_block[0]:
                price = float(price_block[0][-1])
        except (IndexError, TypeError):
            pass
        try:
            if price_block and len(price_block) > 1:
                currency = extract_currency_from_price_token(price_block[1])
        except (IndexError, TypeError):
            pass
        return price, currency

    @staticmethod
    def _parse_currency(data: list) -> str | None:
        """Extract the returned currency code from raw flight data."""
        try:
            price_block = SearchFlights._get_price_block(data)
            if price_block and len(price_block) > 1:
                return extract_currency_from_price_token(price_block[1])
        except (IndexError, TypeError):
            pass
        return None

    @staticmethod
    def _get_price_block(data: list) -> list | None:
        """Return the raw price block attached to a flight row."""
        try:
            if len(data) > 1 and isinstance(data[1], list):
                return data[1]
        except TypeError:
            pass
        return None

    @staticmethod
    def _parse_datetime(date_arr: list[int], time_arr: list[int]) -> datetime:
        """Convert date and time arrays to datetime.

        Args:
            date_arr: List of integers [year, month, day]
            time_arr: List of integers [hour, minute]

        Returns:
            Parsed datetime object

        Raises:
            ValueError: If arrays contain only None values

        """
        if not any(x is not None for x in date_arr) or not any(x is not None for x in time_arr):
            raise ValueError("Date and time arrays must contain at least one non-None value")

        return datetime(*(x or 0 for x in date_arr), *(x or 0 for x in time_arr))

    @staticmethod
    def _parse_airline(airline_code: str) -> Airline:
        """Convert airline code to Airline enum.

        Args:
            airline_code: Raw airline code from API

        Returns:
            Corresponding Airline enum value

        """
        if airline_code[0].isdigit():
            airline_code = f"_{airline_code}"
        return getattr(Airline, airline_code)

    @staticmethod
    def _parse_airport(airport_code: str) -> Airport:
        """Convert airport code to Airport enum.

        Args:
            airport_code: Raw airport code from API

        Returns:
            Corresponding Airport enum value

        """
        return getattr(Airport, airport_code)
