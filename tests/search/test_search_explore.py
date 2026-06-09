"""Unit tests for SearchExplore response parsing (no network)."""

import json

from fli.models import ExploreDestination, ExploreLocation, ExploreSearchFilters
from fli.search.explore import SearchExplore

# ---------------------------------------------------------------------------
# Record builders matching the documented wire formats
# ---------------------------------------------------------------------------


def make_cheapest_date_record(**overrides) -> list:
    """Build a 29-element cheapest-date record (see _parse_destination)."""
    record = [None] * 29
    record[0] = "/m/04jpl"  # kg_id
    record[1] = [51.5074, -0.1278]  # [lat, lng]
    record[2] = "London"  # display name
    record[3] = "https://example.com/photo.jpg"  # thumbnail
    record[4] = "United Kingdom"  # country (legacy)
    record[6] = 2  # connected enum (2 => true)
    record[11] = "2026-09-01"  # departure date (legacy)
    record[12] = "2026-09-08"  # return date (legacy)
    record[15] = "LHR"  # airport (legacy)
    record[16] = 123.0  # price
    record[17] = 95.0  # duration minutes
    record[20] = True  # noteworthy
    record[26] = "England"  # subtitle
    for idx, value in overrides.items():
        record[int(idx)] = value
    return record


def make_specific_date_record(
    kg_id: str = "/m/06c62",
    price: float = 199.0,
    airport: str = "FCO",
    name: str = "Rome",
) -> list:
    """Build a 16-element specific-date record."""
    record = [None] * 16
    record[0] = kg_id
    record[1] = [[None, price], "booking-token"]
    dest_info = [None] * 8
    dest_info[5] = airport
    dest_info[7] = name
    record[6] = dest_info
    record[9] = False  # is_domestic
    return record


def make_update_record(
    kg_id: str = "/m/06c62",
    price: float = 250.0,
    airline_code: str = "AZ",
    airline_name: str = "ITA Airways",
    duration: float = 135.0,
    airport: str = "FCO",
) -> list:
    """Build a y0d streaming-update record (see _apply_update)."""
    record = [None] * 17
    record[0] = kg_id
    record[1] = [[None, price], "booking-token"]
    record[2] = True  # noteworthy
    detail = [None] * 9
    detail[0] = airline_code
    detail[1] = airline_name
    detail[3] = duration
    detail[5] = airport
    record[6] = detail
    record[10] = 1  # connected enum (1 => true)
    return record


# ---------------------------------------------------------------------------
# _parse_destination
# ---------------------------------------------------------------------------


def test_parse_cheapest_date_record():
    dest = SearchExplore._parse_destination(make_cheapest_date_record())
    assert dest is not None
    assert dest.kg_id == "/m/04jpl"
    assert dest.name == "London"
    assert dest.latitude == 51.5074
    assert dest.longitude == -0.1278
    assert dest.price == 123.0
    assert dest.duration_minutes == 95.0
    assert dest.noteworthy is True
    assert dest.connected is True
    assert dest.subtitle == "England"
    assert dest.thumbnail_url == "https://example.com/photo.jpg"
    # [20] is noteworthy, not is_domestic — domesticity is unknown here.
    assert dest.is_domestic is None


def test_parse_cheapest_date_record_with_non_numeric_coords():
    record = make_cheapest_date_record(**{"1": ["not-a-float", None]})
    dest = SearchExplore._parse_destination(record)
    assert dest is not None
    assert dest.latitude is None
    assert dest.longitude is None


def test_parse_specific_date_record():
    dest = SearchExplore._parse_destination(
        make_specific_date_record(), departure_date="2026-09-26", return_date="2026-10-03"
    )
    assert dest is not None
    assert dest.kg_id == "/m/06c62"
    assert dest.name == "Rome"
    assert dest.airport == "FCO"
    assert dest.price == 199.0
    assert dest.is_domestic is False
    # Dates fall back to the requested range in specific-date mode.
    assert dest.departure_date == "2026-09-26"
    assert dest.return_date == "2026-10-03"


def test_parse_specific_date_record_ignores_bool_price():
    record = make_specific_date_record()
    record[1] = [[None, True], "booking-token"]
    dest = SearchExplore._parse_destination(record)
    assert dest is not None
    assert dest.price is None


def test_parse_rejects_malformed_records():
    assert SearchExplore._parse_destination(None) is None
    assert SearchExplore._parse_destination([]) is None
    assert SearchExplore._parse_destination([123, None, None, None, None, None, None]) is None
    # Too short for the cheapest-date format.
    assert SearchExplore._parse_destination(["/m/x", None, "Name"] + [None] * 4) is None


# ---------------------------------------------------------------------------
# _apply_update
# ---------------------------------------------------------------------------


def test_apply_update_enriches_destination():
    dest = ExploreDestination(kg_id="/m/06c62", name="Rome")
    seen = {"/m/06c62": dest}

    SearchExplore._apply_update(make_update_record(), seen)

    assert dest.price == 250.0
    assert dest.noteworthy is True
    assert dest.connected is True
    assert dest.airport == "FCO"
    assert dest.airline_code == "AZ"
    assert dest.airline_name == "ITA Airways"
    assert dest.duration_minutes == 135.0


def test_apply_update_does_not_overwrite_existing_fields():
    dest = ExploreDestination(
        kg_id="/m/06c62",
        name="Rome",
        airport="CIA",
        airline_code="FR",
        airline_name="Ryanair",
        duration_minutes=120.0,
    )
    seen = {"/m/06c62": dest}

    SearchExplore._apply_update(make_update_record(), seen)

    assert dest.airport == "CIA"
    assert dest.airline_code == "FR"
    assert dest.airline_name == "Ryanair"
    assert dest.duration_minutes == 120.0
    # Price-block prices DO refresh — streaming updates carry newer totals.
    assert dest.price == 250.0


def test_apply_update_ground_route_uses_display_price_pair():
    """Ground ("connected") destinations have no booking price-block.

    Their price arrives as a [[null, price]] pair at [15], while [16] holds a
    small enum (observed live: 3) that must never be mistaken for a price.
    """
    dest = ExploreDestination(kg_id="/m/04g61", name="Luxembourg")
    seen = {"/m/04g61": dest}

    record = make_update_record(kg_id="/m/04g61", airline_code="multi", airline_name="")
    record[1] = None  # no booking price-block
    record[6][3] = 0  # ground route: no flight duration
    record[15] = [[None, 121.0]]
    record[16] = 3  # enum, NOT a price

    SearchExplore._apply_update(record, seen)

    assert dest.price == 121.0
    assert dest.duration_minutes is None  # zero duration ignored
    assert dest.airline_name is None  # empty string ignored
    assert dest.airline_code == "multi"


def test_apply_update_never_reads_record_16_as_price():
    dest = ExploreDestination(kg_id="/m/04g61", name="Luxembourg")
    seen = {"/m/04g61": dest}

    record = make_update_record(kg_id="/m/04g61")
    record[1] = None  # no booking price-block
    record[15] = None  # no display-price pair either
    record[16] = 3

    SearchExplore._apply_update(record, seen)

    assert dest.price is None


def test_apply_update_unknown_kg_id_is_noop():
    seen: dict[str, ExploreDestination] = {}
    SearchExplore._apply_update(make_update_record(kg_id="/m/unknown"), seen)
    assert seen == {}


def test_apply_update_ignores_malformed_records():
    dest = ExploreDestination(kg_id="/m/06c62", name="Rome")
    seen = {"/m/06c62": dest}
    SearchExplore._apply_update(None, seen)
    SearchExplore._apply_update([], seen)
    SearchExplore._apply_update([42], seen)
    assert dest.price is None


# ---------------------------------------------------------------------------
# _merge
# ---------------------------------------------------------------------------


def test_merge_fills_only_empty_fields():
    into = ExploreDestination(kg_id="/m/x", name="Paris", price=100.0)
    other = ExploreDestination(
        kg_id="/m/x", name="Paris (alt)", price=200.0, airport="CDG", country="France"
    )

    SearchExplore._merge(into, other)

    assert into.price == 100.0  # kept
    assert into.name == "Paris"  # kept
    assert into.airport == "CDG"  # filled
    assert into.country == "France"  # filled


# ---------------------------------------------------------------------------
# Full search() flow against a stubbed HTTP client
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _StubClient:
    def __init__(self, content: bytes):
        self._content = content

    def post(self, url: str, **kwargs):
        return _StubResponse(self._content)


def _build_envelope(primary_block: list, updates_block: list | None = None) -> bytes:
    inner = [None, None, None, primary_block, updates_block]
    envelope = [["wrb.fr", None, json.dumps(inner)]]
    return b")]}'" + json.dumps(envelope).encode()


def test_search_parses_and_merges_blocks():
    primary = [
        [
            make_cheapest_date_record(),
            make_specific_date_record(),
            ["bad-record-too-short"],  # silently skipped
        ]
    ]
    updates = [[make_update_record()]]

    search = SearchExplore.__new__(SearchExplore)  # skip network client init
    search.client = _StubClient(_build_envelope(primary, updates))

    results = search.search(ExploreSearchFilters(origin=[ExploreLocation.airport("JFK")]))

    assert results is not None
    assert {d.kg_id for d in results} == {"/m/04jpl", "/m/06c62"}
    rome = next(d for d in results if d.kg_id == "/m/06c62")
    assert rome.price == 250.0  # streaming update applied
    assert rome.airline_code == "AZ"


def test_search_returns_none_when_no_destinations():
    search = SearchExplore.__new__(SearchExplore)
    search.client = _StubClient(_build_envelope([]))
    assert search.search(ExploreSearchFilters(origin=[ExploreLocation.airport("JFK")])) is None


def test_search_survives_records_with_bad_coords():
    # A record whose coords are strings used to raise a pydantic
    # ValidationError and abort the whole search; it now parses with
    # coords left unset and the rest of the results intact.
    bad = make_cheapest_date_record(**{"1": ["51.5", "-0.12"]})
    good = make_specific_date_record()

    search = SearchExplore.__new__(SearchExplore)
    search.client = _StubClient(_build_envelope([[bad, good]]))

    results = search.search(ExploreSearchFilters(origin=[ExploreLocation.airport("JFK")]))
    assert results is not None
    assert {d.kg_id for d in results} == {"/m/04jpl", "/m/06c62"}
    london = next(d for d in results if d.kg_id == "/m/04jpl")
    assert london.latitude is None and london.longitude is None
