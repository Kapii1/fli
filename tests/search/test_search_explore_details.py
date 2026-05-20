"""Parser tests for SearchExploreDetails using a captured live response."""

import json
from pathlib import Path

import pytest

from fli.models import (
    Airport,
    ExploreFlightDetailsFilters,
    ExploreLocation,
    TripType,
)
from fli.search.explore import SearchExploreDetails

SAMPLE = Path(__file__).resolve().parent.parent / "explore_flight_details_sample.txt"


def _wrb_payloads(raw_bytes: bytes) -> list[list]:
    """Walk the streaming body and return decoded ``wrb.fr`` inner payloads.

    Body format: ``)]}'`` anti-XSSI prefix, then alternating ``<byte_size>\\n``
    headers and JSON frames. We don't rely on the size headers (some captures
    contain pretty-printed JSON with embedded newlines, making size-based
    slicing fragile) — instead we strip the prefix and advance through the
    buffer with ``json.JSONDecoder.raw_decode``, which finds the end of each
    JSON value automatically.
    """
    text = raw_bytes.decode("utf-8")
    if text.startswith(")"):
        # Skip the )]}' prefix in any whitespace arrangement.
        text = text.lstrip(")]'}\r\n \t")
    decoder = json.JSONDecoder()
    payloads: list[list] = []
    pos = 0
    while pos < len(text):
        # Skip whitespace and any digit-only "size header" lines between frames.
        while pos < len(text) and not text[pos] in "[{":
            pos += 1
        if pos >= len(text):
            break
        try:
            envelope, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        pos = end
        if isinstance(envelope, list):
            for row in envelope:
                if isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr" and row[2]:
                    payloads.append(json.loads(row[2]))
    return payloads


def test_request_encoding_shape():
    filters = ExploreFlightDetailsFilters(
        origin=[ExploreLocation.airport(Airport.CDG)],
        destination=[ExploreLocation.airport("ELU")],
        from_date="2026-05-18",
        to_date="2026-05-22",
        trip_type=TripType.ROUND_TRIP,
    )
    outer = filters.format()
    assert outer[0] is None
    filter_block = outer[1]
    assert isinstance(filter_block, list)
    assert len(filter_block) == 18
    # Specific-date marker at field 18 (index 17) should be 1 when from_date is set.
    assert filter_block[17] == 1
    segments = filter_block[13]
    assert len(segments) == 2  # round-trip → 2 segments
    outbound = segments[0]
    assert outbound[0] == [[["CDG", 0]]]
    assert outbound[1] == [[["ELU", 0]]]
    assert outbound[6] == "2026-05-18"


def test_parser_decodes_captured_sample():
    """Smoke-test the parser against a captured Paris → Seville response."""
    if not SAMPLE.exists():
        pytest.skip(f"sample fixture not present at {SAMPLE}")
    raw = SAMPLE.read_bytes()
    payloads = _wrb_payloads(raw)
    assert payloads, "expected at least one wrb.fr payload in fixture"

    from fli.models import ExploreFlightDetailsResult

    result = ExploreFlightDetailsResult()
    for payload in payloads:
        SearchExploreDetails._merge_payload(payload, result)

    assert len(result.offers) == 4
    assert result.departure_date == "2026-05-18"
    assert result.return_date == "2026-05-22"
    assert result.session_id == "2Rr5abeXF83ChcIPsovvwQc"
    assert result.cursor_token and result.cursor_token.startswith("HO-deIntSQ9gAA8n8QBG")
    assert result.destination_name == "Séville"

    # Prices and airlines exactly as shown in the Google Flights UI.
    by_airline_price = sorted((o.airline_code, o.price) for o in result.offers)
    assert by_airline_price == [
        ("TO", 144.0),
        ("VY", 140.0),
        ("VY", 147.0),
        ("multi", 336.0),
    ]
    for offer in result.offers:
        assert offer.currency == "EUR"
        assert offer.origin_city_kg_id == "/m/05qtj"

    best = [o for o in result.offers if o.is_best]
    assert len(best) == 1
    best_offer = best[0]
    assert best_offer.airline_code == "VY"
    assert best_offer.airline_name == "Vueling"
    assert best_offer.stops == 0
    assert best_offer.price == 140.0
    assert best_offer.origin_airport == "ORY"
    assert best_offer.destination_airport == "SVQ"

    # Multi-modal offer (Iberia + Renfe train) lands at the Sevilla Santa Justa
    # train station, not the airport.
    multi = next(o for o in result.offers if o.airline_code == "multi")
    assert multi.origin_airport == "CDG"
    assert multi.destination_airport == "XQA"
    assert multi.stops == 1
    assert multi.price == 336.0

    # Price-chart numbers from the summary block.
    assert result.price_chart == [140.0, 105.0, -36.0, 90.0, 150.0]
