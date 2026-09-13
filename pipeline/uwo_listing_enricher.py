"""
uwo_listing_enricher.py  —  Stage 1 of the UWO housing pipeline.
Scrapes listing detail pages and applies deterministic rule extraction for every
field that can be resolved from structured amenities or clear description phrases.
Output feeds directly into ai_enricher.py.

Rule extraction covers:
  - fallback address recovery from explicit civic-address prose
  - price periods tied to the advertised amount
  - parking (available + count, with amenity-token adjacency fix)
  - utilities_included_rule + utilities_status + utilities_status_source
  - furnished_rule (from structured amenity tokens first, then description phrases)
  - air_conditioning_rule, laundry_rule, dishwasher_rule (from amenity tokens)
  - bathroom_type_rule (from "Private Bathroom" / "Shared Bathroom" tokens)
  - bathrooms_rule (from explicit whole-property/unit count wording)
  - is_sublet (from strong sublet phrases in title/description)
  - lease_term_months_rule (handles "12", "12.0", "4 (Negotiable)", "12-month")
  - lease_type_rule (sublet > standard > fixed_term > short_term > None)

Changelog vs previous version:
  - _parse_lease_term_months_rule: handles float strings ("12.0") and the site's
    "N (Negotiable)" pattern via LEASE_FLOAT_RE
  - ListingRecord gains utilities_status_source so ai_enricher.py can skip
    consensus on utility fields already resolved from amenity tokens
  - UTILITIES_PARTIAL_RE tightened: requires named utility directly before
    "included", blocking false positives like "snow removal included"
"""
import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from pipeline.run_context import RunContext
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext


# ─── HTTP ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://offcampus.uwo.ca/",
}


# ─── Regex patterns ───────────────────────────────────────────────────────────

DETAIL_URL_RE  = re.compile(r"/Listings/Details/(\d+)")
PRICE_RE       = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
PRICE_TEXT_RE  = re.compile(
    r"\$\s*[\d,]+(?:\.\d{1,2})?"
    r"(?:\s*(?:(?:per|/)\s*(?:bdrm|bedroom|month|week|day)"
    r"|monthly|weekly|daily))?",
    re.I,
)
INT_RE         = re.compile(r"(\d+)")

DESCRIPTION_ADDRESS_RE = re.compile(
    r"\b(?:welcome\s+to|located\s+at|address\s+is|"
    r"(?:property|home|house|apartment|unit)\s+(?:is\s+)?(?:located\s+)?at)\s+"
    r"(?P<address>(?:[A-Z]-)?\d{1,5}(?:-\d{1,5})?\s+"
    r"[A-Z0-9][A-Z0-9' .-]{0,60}?\s+"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|court|crt|ct|crescent|cres|"
    r"boulevard|blvd|lane|ln|place|pl|circle|way|terrace|trail|gate))\b",
    re.I,
)

_NUMBER_WORDS = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
}
_COUNT_TOKEN = r"(?:\d+(?:\.5)?|one|two|three|four|five|six|seven|eight)"
_BATHROOM_TOTAL_PATTERNS = (
    re.compile(
        rf"\b(?:there\s+(?:are|is)|(?:home|house|unit|apartment|property|rental|suite|mansion)\s+"
        rf"(?:has|have|includes?|features?|offers?|boasts?))\s+"
        rf"(?P<count>{_COUNT_TOKEN})[\s-]+(?:full[\s-]+)?(?:bath|bathroom|washroom)s?\b",
        re.I,
    ),
    re.compile(
        rf"\b{_COUNT_TOKEN}[\s-]+bed(?:room)?s?\s*(?:,|/|&|and)?\s*"
        rf"(?P<count>{_COUNT_TOKEN})[\s-]+(?:full[\s-]+)?(?:bath|bathroom|washroom)s?\b",
        re.I,
    ),
    re.compile(
        rf"\b(?:includes?|features?|offers?|boasts?)\s+"
        rf"(?P<count>{_COUNT_TOKEN})[\s-]+(?:full[\s-]+)?(?:bath|bathroom|washroom)s?\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<count>(?:[2-9](?:\.5)?|two|three|four|five|six|seven|eight))"
        rf"[\s-]+full[\s-]+(?:bath|bathroom|washroom)s?\b",
        re.I,
    ),
)

WEEKS_PER_YEAR = 52
DAYS_PER_YEAR = 365
MONTHS_PER_YEAR = 12

# Parking: site tokenises "Parking (1)" as separate tokens ["Parking", "(1)"]
PARKING_TOKEN_RE       = re.compile(r"^\s*parking\s*$", re.I)
PARKING_COUNT_TOKEN_RE = re.compile(r"^\s*\((\d+)\)\s*$")

# Sublet: strong deterministic phrases only; conservative by design
SUBLET_RE = re.compile(
    r"\b(?:"
    r"sublet(?:ting)?|subleas(?:e|ing)|sub-?let(?:ting)?|sub-?leas(?:e|ing)"
    r"|take\s+over\s+(?:my\s+)?lease"
    r"|lease\s+take[\s-]?over"
    r"|lease\s+transfer"
    r"|lease\s+assumption"
    r"|taking\s+over\s+(?:the\s+)?lease"
    r"|immediate\s+lease\s+transfer"
    r")\b",
    re.I,
)
# Furnished: check UNFURNISHED first to avoid substring collision
UNFURNISHED_RE = re.compile(r"\b(?:unfurnished|not\s+furnished)\b", re.I)
FURNISHED_RE   = re.compile(r"\b(?:fully\s+)?furnished\b", re.I)
FURNISHINGS_NOT_INCLUDED_RE = re.compile(
    r"\bfurnishings?\s+(?:are\s+)?not\s+included\b", re.I
)

FEMALE_ONLY_RE = re.compile(r"\b(?:female|females|women|girls)\s+only\b", re.I)
MALE_ONLY_RE = re.compile(r"\b(?:male|males|men|boys)\s+only\b", re.I)
FEMALE_PREFERRED_RE = re.compile(
    r"\b(?:female|females|women)\s+(?:preferred|preference)\b"
    r"|\bprefer(?:red|ence)?\s+(?:female|females|women)\b",
    re.I,
)
MALE_PREFERRED_RE = re.compile(
    r"\b(?:male|males|men)\s+(?:preferred|preference)\b"
    r"|\bprefer(?:red|ence)?\s+(?:male|males|men)\b",
    re.I,
)
ANY_GENDER_RE = re.compile(
    r"\b(?:any\s+gender|all\s+genders|no\s+gender\s+preference|co-?ed)\b",
    re.I,
)

SUMMER_AVAILABILITY_PATTERNS = (
    re.compile(r"\bsummer\s+(?:rent(?:al)?|lease|sublease|availability)\b", re.I),
    re.compile(
        r"\bavailable\b.{0,35}\b(?:for|during|over|in)?\s*(?:the\s+)?summer\b",
        re.I,
    ),
    re.compile(r"\b(?:earlier\s+)?summer\s+move[\s-]?in\b", re.I),
    re.compile(
        r"\b(?:rent(?:al)?|lease|available)\b.{0,80}\bmay\b.{0,45}\baug(?:ust)?\b"
        r"|\bmay\b.{0,45}\baug(?:ust)?\b.{0,80}\b(?:rent(?:al)?|lease|available)\b",
        re.I,
    ),
)

MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MONTH_TOKEN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
AVAILABILITY_RANGE_RE = re.compile(
    rf"\b(?:from\s+)?(?P<start_month>{MONTH_TOKEN})\.?"
    rf"(?:\s+(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"(?:,\s*|\s+)?(?P<start_year>\d{{4}})?\s*"
    rf"(?:to|through|thru|[-\u2013\u2014])\s*"
    rf"(?P<end_month>{MONTH_TOKEN})\.?"
    rf"(?:\s+(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"(?:,\s*|\s+)?(?P<end_year>\d{{4}})?\b",
    re.I,
)
SUMMER_MONTHS = frozenset({5, 6, 7, 8})
AVAILABILITY_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"available|availability|sublet|sublease|subletting|assign(?:ed|ing|ment)?|"
    r"lease|leasing|rental\s+term|tenancy|occupancy|possession|"
    r"move[\s-]?in|move[\s-]?out|room\s+available|unit\s+available|"
    r"bedroom\s+available|school\s+year|summer\s+rent(?:al)?|term|"
    r"\d+\s+(?:furnished\s+)?bedrooms?"
    r")\b",
    re.I,
)
PRICING_CONTEXT_RE = re.compile(
    r"(?:\$\s*\d|\bCAD\b|\b(?:"
    r"rent|rate|price|pricing|discount(?:ed)?|promotion(?:al)?|special|"
    r"per\s+month|monthly|reduced|save|first\s+month\s+free"
    r")\b|/\s*month\b)",
    re.I,
)
CONTEXT_BOUNDARY_RE = re.compile(r"[.!?;:\r\n]")

# Lease term — handles "12", "12.0", "4 (Negotiable)", "12-month"
# LEASE_FLOAT_RE: optional decimal, optional trailing parenthetical
LEASE_FLOAT_RE       = re.compile(r"^\s*(\d{1,2})(?:\.\d+)?\s*(?:\(.*\))?\s*$")
LEASE_MONTHS_TEXT_RE = re.compile(r"(\d{1,2})\s*-?\s*month", re.I)
MONTH_TO_MONTH_RE    = re.compile(r"\bmonth[\s-]to[\s-]month\b|\bm2m\b", re.I)

# Utilities — amenity tokens
UTILITIES_INCL_TOKEN_RE = re.compile(r"^utilities\s+incl(?:uded)?$", re.I)
INTERNET_INCL_TOKEN_RE  = re.compile(r"^internet\s+incl(?:uded)?$", re.I)
CABLE_INCL_TOKEN_RE     = re.compile(r"^cable\s+incl(?:uded)?$", re.I)

# Utilities — free text
UTILITIES_ALL_RE = re.compile(
    r"\b(?:"
    r"all[\s-]inclusive"
    r"|utilities\s+included(?!\s*:)"
    r"|all\s+utilities\s+(?:are\s+)?included"
    r"|includes?\s+all\s+utilities"
    r")\b",
    re.I,
)
UTILITIES_NONE_RE = re.compile(
    r"\b(?:"
    r"utilities\s+not\s+included"
    r"|utilities\s+(?:are\s+)?extra"
    r"|plus\s+utilities"
    r"|\+\s*utilities"
    r"|(?:hydro|electricity|heat|water)\s+(?:not\s+included|is\s+extra|extra)"
    r")\b",
    re.I,
)
# Tight match: only named utility keywords directly before "included"
# "snow removal included" / "lawn care included" will NOT match
UTILITIES_PARTIAL_RE = re.compile(
    r"\b(?:gas|water|hydro|electricity|heat|internet|wi-?fi|cable)\s+(?:is\s+)?included\b",
    re.I,
)

BUS_ROUTE_RE = re.compile(r"\b(?:0?\d{1,3}|N\d{1,2})\b")


# ─── Data record ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AvailabilityClassification:
    category: Optional[str] = None
    source: Optional[str] = None
    evidence: Optional[str] = None
    start_month: Optional[int] = None
    end_month: Optional[int] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    conflict: bool = False


@dataclass
class ListingRecord:
    listing_id: Optional[str]
    source_url: str

    title:       Optional[str] = None
    address_raw: Optional[str] = None
    address:     Optional[str] = None

    price_text:    Optional[str]   = None
    price_numeric: Optional[float] = None
    price_period:  Optional[str]   = None
    price_monthly: Optional[float] = None

    housing_type_raw: Optional[str] = None
    housing_type:     Optional[str] = None

    bedrooms_raw: Optional[str] = None
    bedrooms:     Optional[int] = None

    # ── Utilities ──────────────────────────────────────────────────────────
    utilities_raw:           Optional[str]  = None
    utilities_included_rule: Optional[bool] = None  # True = ALL included
    utilities_status:        Optional[str]  = None  # all_included / partially_included / not_included
    utilities_status_source: Optional[str]  = None  # "rule" when set from amenity tokens/text

    # ── Availability ───────────────────────────────────────────────────────
    date_available_raw:  Optional[str]  = None
    date_available:      Optional[str]  = None
    availability_text:   Optional[str]  = None
    available_now_rule:  Optional[bool] = None

    lease_term_raw:         Optional[str] = None
    lease_term_months_rule: Optional[int] = None
    lease_type_rule:        Optional[str] = None

    # ── Location ───────────────────────────────────────────────────────────
    location_area_raw:     Optional[str]   = None
    location_area:         Optional[str]   = None
    distance_to_campus_raw: Optional[str]  = None
    distance_to_campus_km:  Optional[float] = None

    # ── Preferences ────────────────────────────────────────────────────────
    preferred_gender_raw:  Optional[str]  = None
    preferred_gender_rule: str            = "not_specified"
    smoking_raw:           Optional[str]  = None
    smoking_allowed_rule:  Optional[bool] = None
    tenant_type_raw:       Optional[str]  = None
    tenant_type_rule:      str            = "not_specified"

    # ── Free text ──────────────────────────────────────────────────────────
    description:    Optional[str] = None
    amenities:      Optional[str] = None
    amenities_list: Optional[str] = None

    # ── Parking ────────────────────────────────────────────────────────────
    parking_available_rule: Optional[bool] = None
    parking_spaces_rule:    Optional[int]  = None

    # ── Amenity-derived rule fields ─────────────────────────────────────────
    air_conditioning_rule: Optional[bool] = None
    laundry_rule:          Optional[bool] = None
    dishwasher_rule:       Optional[bool] = None
    furnished_rule:        Optional[bool] = None
    bathrooms_rule:        Optional[float] = None
    bathroom_type_rule:    Optional[str]  = None

    # ── Sublet ─────────────────────────────────────────────────────────────
    is_sublet: Optional[bool] = None
    is_sublet_source: Optional[str] = None

    # ── Downstream AI placeholders ─────────────────────────────────────────
    available_from:        Optional[str] = None
    available_to:          Optional[str] = None
    availability_category: Optional[str] = None
    availability_category_source: Optional[str] = None
    availability_category_evidence: Optional[str] = None
    availability_category_conflict: Optional[bool] = None
    rental_arrangement:    Optional[str] = None
    sublet_type:           Optional[str] = None

    # ── Contact / transit ──────────────────────────────────────────────────
    landlord_name:  Optional[str] = None
    landlord_phone: Optional[str] = None
    transit_routes: Optional[str] = None

    scraped_ok:   bool          = False
    scrape_error: Optional[str] = None


def listing_id_from_url(url: str) -> Optional[str]:
    match = DETAIL_URL_RE.search(url)
    return match.group(1) if match else None


class UWOListingScraper:
    def __init__(self, delay_min: float = 0.3, delay_max: float = 0.8, timeout: int = 20):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout   = timeout
        self.session   = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def polite_pause(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def scrape_listing(self, url: str) -> ListingRecord:
        try:
            html = self.fetch_html(url)
            return self.parse(url, html)
        except Exception as exc:
            return ListingRecord(
                listing_id=listing_id_from_url(url),
                source_url=url,
                scraped_ok=False,
                scrape_error=f"{type(exc).__name__}: {exc}",
            )

    def parse(self, url: str, html: str) -> ListingRecord:
        soup   = BeautifulSoup(html, "html.parser")
        record = ListingRecord(listing_id=listing_id_from_url(url), source_url=url)

        title, address_raw, price_text, price_numeric = self._parse_title_block(soup)
        record.title         = title
        record.address_raw   = address_raw
        record.address       = self._clean_address(address_raw)
        record.price_text    = price_text
        record.price_numeric = price_numeric

        detail_map         = self._extract_detail_pairs(soup)
        record.description = self._extract_description(soup)
        if record.address is None:
            record.address = self._recover_address_from_description(record.description)

        amenities_list        = self._extract_amenities(soup)
        record.amenities_list = json.dumps(amenities_list, ensure_ascii=False)
        record.amenities      = ", ".join(amenities_list) if amenities_list else None

        record.housing_type_raw = detail_map.get("Housing Type")
        record.housing_type     = self._normalize_housing_type(record.housing_type_raw)
        record.bedrooms_raw     = detail_map.get("Bedroom(s)")
        record.bedrooms         = self._to_int(record.bedrooms_raw)

        record.utilities_raw    = detail_map.get("Utilities")
        record.utilities_status = self._parse_utilities_status(
            amenities_list, record.utilities_raw, record.description
        )
        record.utilities_status_source  = "rule" if record.utilities_status is not None else None
        record.utilities_included_rule  = self._utilities_included_from_status(record.utilities_status)

        record.date_available_raw = detail_map.get("Date Available")
        record.date_available     = self._clean_text(record.date_available_raw) if record.date_available_raw else None
        record.availability_text  = record.date_available
        record.available_now_rule = self._parse_available_now(record.date_available, record.description)

        record.lease_term_raw = detail_map.get("Lease Term")
        record.is_sublet = self._detect_sublet(
            record.title,
            record.description,
            record.lease_term_raw,
            record.housing_type_raw,
        )
        record.is_sublet_source = (
            "deterministic_rule" if record.is_sublet is True else None
        )
        record.lease_term_months_rule = self._parse_lease_term_months_rule(record.lease_term_raw)
        record.lease_type_rule        = self._parse_lease_type_rule(
            record.is_sublet, record.lease_term_months_rule, record.description
        )

        record.location_area_raw      = detail_map.get("Location")
        record.location_area          = self._clean_text(record.location_area_raw) if record.location_area_raw else None
        record.distance_to_campus_raw = detail_map.get("Distance")
        record.distance_to_campus_km  = self._parse_distance_km(record.distance_to_campus_raw)

        record.preferred_gender_raw  = detail_map.get("Prefered Gender") or detail_map.get("Preferred Gender")
        record.preferred_gender_rule = self._normalize_gender(record.preferred_gender_raw, record.description)
        record.smoking_raw           = detail_map.get("Smoking")
        record.smoking_allowed_rule  = self._normalize_smoking(record.smoking_raw, record.description)
        record.tenant_type_raw       = detail_map.get("Tenant Type")
        record.tenant_type_rule      = self._normalize_tenant_type(record.tenant_type_raw, record.description)

        record.parking_available_rule, record.parking_spaces_rule = self._parse_parking_fields(
            amenities_list, record.description
        )

        record.air_conditioning_rule = self._parse_amenity_bool(
            amenities_list, {"a/c", "air conditioning", "central air", "air-conditioning"}
        )
        record.laundry_rule = self._parse_amenity_bool(
            amenities_list,
            {"laundry", "in-suite laundry", "ensuite laundry", "in suite laundry", "washer", "washer/dryer"},
        )
        record.dishwasher_rule    = self._parse_amenity_bool(amenities_list, {"dishwasher"})
        record.bathrooms_rule     = self._parse_bathrooms_rule(record.description)
        record.bathroom_type_rule = self._parse_bathroom_type_rule(amenities_list)
        record.furnished_rule     = self._parse_furnished_rule(amenities_list, record.description)

        record.landlord_name, record.landlord_phone = self._extract_contact(soup)
        record.transit_routes = self._extract_transit_routes(record.description)
        record.price_period = self._infer_price_period(
            record.title, record.description, record.price_numeric
        )
        record.price_monthly = self._normalize_monthly_price(
            record.price_numeric, record.price_period
        )
        availability = self._parse_availability_evidence(
            record.title, record.description, record.date_available
        )
        record.availability_category = availability.category
        record.availability_category_source = availability.source
        record.availability_category_evidence = availability.evidence
        record.availability_category_conflict = availability.conflict or None

        record.scraped_ok = True
        return record

    # ── Parse helpers ─────────────────────────────────────────────────────────

    def _parse_title_block(
        self, soup: BeautifulSoup
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
        h2 = soup.find("h2")
        if not h2:
            return None, None, None, None
        text        = " ".join(h2.stripped_strings)
        price_text_match = PRICE_TEXT_RE.search(text)
        price_text = price_text_match.group(0) if price_text_match else None
        price_match = PRICE_RE.search(price_text or "")
        price_numeric = float(price_match.group(1).replace(",", "")) if price_match else None
        address = text
        if price_text:
            address = text.replace(price_text, "").strip(" -–|")
        return text, address or None, price_text, price_numeric

    def _extract_detail_pairs(self, soup: BeautifulSoup) -> Dict[str, str]:
        detail_map:  Dict[str, str] = {}
        stop_labels = {"Amenities", "Contact Landlord"}
        h2   = soup.find("h2")
        node = h2.find_next() if h2 else soup.body
        current_label = None
        while node:
            if getattr(node, "name", None) == "h2":
                break
            if getattr(node, "name", None) == "h3":
                label = self._clean_text(node.get_text(" ", strip=True))
                if label in stop_labels:
                    break
                current_label = label
            elif getattr(node, "name", None) in {"p", "div", "span"} and current_label:
                value = self._clean_text(node.get_text(" ", strip=True))
                if value and value not in detail_map and value != current_label:
                    detail_map[current_label] = value
                    current_label = None
            node = node.find_next()
        if not detail_map:
            text_nodes = [self._clean_text(x) for x in soup.stripped_strings]
            wanted = {
                "Housing Type", "Bedroom(s)", "Utilities", "Date Available",
                "Lease Term", "Location", "Distance",
                "Prefered Gender", "Preferred Gender", "Smoking", "Tenant Type",
            }
            for i, v in enumerate(text_nodes[:-1]):
                if v in wanted and i + 1 < len(text_nodes):
                    detail_map[v] = text_nodes[i + 1]
        return detail_map

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        text_nodes = [self._clean_text(x) for x in soup.stripped_strings]
        if "Tenant Type" in text_nodes:
            idx = text_nodes.index("Tenant Type")
            chunk = text_nodes[idx + 2:]
            parts: List[str] = []
            for item in chunk:
                if item in {"Amenities", "Contact Landlord", "Your contact is"}:
                    break
                parts.append(item)
            desc = " ".join(parts).strip()
            return desc or None
        return None

    def _extract_amenities(self, soup: BeautifulSoup) -> List[str]:
        text_nodes = [self._clean_text(x) for x in soup.stripped_strings]
        amenities: List[str] = []
        if "Amenities" not in text_nodes:
            return amenities
        idx = text_nodes.index("Amenities")
        for item in text_nodes[idx + 1:]:
            if item == "Contact Landlord":
                break
            if item and item not in amenities:
                amenities.append(item)
        return amenities

    def _extract_contact(self, soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
        text_nodes     = [self._clean_text(x) for x in soup.stripped_strings]
        landlord_name  = None
        landlord_phone = None
        for i, item in enumerate(text_nodes):
            if item == "Your contact is" and i + 1 < len(text_nodes):
                landlord_name = text_nodes[i + 1]
            if item.startswith("Call:"):
                landlord_phone = item.replace("Call:", "").strip()
        return landlord_name, landlord_phone

    # ── Static normalizers ────────────────────────────────────────────────────

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _clean_address(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = re.sub(r"\bView Map\b",    "", value, flags=re.I)
        text = re.sub(r"\bper bdrm\b",    "", text,  flags=re.I)
        text = re.sub(r"\bper bedroom\b", "", text,  flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" -–|,")
        return text or None

    @staticmethod
    def _to_int(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        match = INT_RE.search(value)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_distance_km(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*Km", value, flags=re.I)
        return float(match.group(1)) if match else None

    @staticmethod
    def _normalize_housing_type(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = re.sub(r"\s+", " ", value.strip().lower())
        source_categories = {
            "house to share": "house_to_share",
            "apt to share": "apartment_to_share",
            "apartment to share": "apartment_to_share",
            "bachelor apt": "bachelor_apartment",
            "bachelor apartment": "bachelor_apartment",
            "sublet": "sublet",
            "sublets": "sublet",
            "rooms": "room",
        }
        if text in source_categories:
            return source_categories[text]
        if "town"      in text: return "townhouse"
        if "house"     in text: return "house"
        if "apartment" in text or re.search(r"\bapt\b", text): return "apartment"
        if "condo"     in text: return "condo"
        if "room"      in text: return "room"
        if "duplex"    in text: return "duplex"
        return re.sub(r"\s+", "_", text)

    @staticmethod
    def _normalize_gender(value: Optional[str], description: Optional[str] = None) -> str:
        structured = re.sub(r"\s+", " ", (value or "").strip().casefold())
        text = description or ""
        if structured in {"female", "women", "woman"}:
            return "female_only" if FEMALE_ONLY_RE.search(text) else "female_preferred"
        if structured in {"male", "men", "man"}:
            return "male_only" if MALE_ONLY_RE.search(text) else "male_preferred"
        if structured in {"any", "any gender", "all genders", "no preference"}:
            return "any"
        if FEMALE_ONLY_RE.search(text): return "female_only"
        if MALE_ONLY_RE.search(text): return "male_only"
        if FEMALE_PREFERRED_RE.search(text): return "female_preferred"
        if MALE_PREFERRED_RE.search(text): return "male_preferred"
        if ANY_GENDER_RE.search(text): return "any"
        return "not_specified"

    @staticmethod
    def _normalize_smoking(value: Optional[str], description: Optional[str] = None) -> Optional[bool]:
        text = f"{value or ''} {description or ''}".lower()
        if not text.strip(): return None
        if any(x in text for x in ["non-smoking", "no smoking", "smoke free", "smokefree", "not allowed"]): return False
        if any(x in text for x in ["smoking allowed", "smokers ok", "smoker friendly"]): return True
        return None

    @staticmethod
    def _normalize_tenant_type(value: Optional[str], description: Optional[str] = None) -> str:
        text = f"{value or ''} {description or ''}".lower()
        if not text.strip(): return "not_specified"
        if "student"    in text: return "student"
        if "professional" in text: return "professional"
        if "family"     in text: return "family"
        if "staff"      in text or "faculty" in text: return "staff_faculty"
        return "not_specified"

    @staticmethod
    def _extract_transit_routes(description: Optional[str]) -> Optional[str]:
        if not description: return None
        lower = description.lower()
        if "bus" not in lower and "route" not in lower and "transit" not in lower: return None
        routes = []
        for route in BUS_ROUTE_RE.findall(description):
            if route not in routes:
                routes.append(route)
        return ", ".join(routes) if routes else None

    @staticmethod
    def _infer_price_period(
        price_context: Optional[str],
        description: Optional[str],
        price_numeric: Optional[float] = None,
    ) -> Optional[str]:
        """Return a period only when it is attached to the advertised amount.

        A bare ``monthly`` elsewhere in the description may describe cleaning,
        utilities, or another price.  Description evidence is therefore used
        only when the same numeric amount appears close to the period wording.
        """

        period_patterns = (
            ("week", re.compile(r"(?:per|/)\s*week\b|\ba\s+week\b|\bweekly\b", re.I)),
            ("day", re.compile(r"(?:per|/)\s*day\b|\ba\s+day\b|\bdaily\b", re.I)),
            (
                "month_per_bedroom",
                re.compile(r"(?:per|/)\s*(?:bdrm|bedroom)\b", re.I),
            ),
            (
                "month",
                re.compile(
                    r"(?:per|/)\s*(?:mo(?:nth)?s?)\b|\ba\s+month\b|\bmonthly\b",
                    re.I,
                ),
            ),
        )

        context = price_context or ""
        for period, pattern in period_patterns:
            if pattern.search(context):
                return period

        amount = price_numeric
        if amount is None:
            match = PRICE_RE.search(context)
            amount = float(match.group(1).replace(",", "")) if match else None
        if amount is None or not description:
            return None

        for match in re.finditer(r"\$?\s*(\d[\d,]*)(?:\.(\d{1,2}))?", description):
            candidate = float(
                match.group(1).replace(",", "") + f".{match.group(2) or '0'}"
            )
            if abs(candidate - amount) > 0.005:
                continue
            after = re.split(r"[.;\n]", description[match.end() : match.end() + 48], maxsplit=1)[0]
            nearby = description[match.start() : match.end()] + after
            for period, pattern in period_patterns:
                if pattern.search(nearby):
                    return period

        # Explicit rental-frequency labels can establish the period even when
        # a description advertises a range or repeats a slightly different
        # amount.  These phrases remain scoped to rent/rates, unlike a bare
        # "monthly" that may describe cleaning or utilities.
        rental_frequency_re = re.compile(
            r"\bmonthly\s+(?:rent(?:al)?|rates?|lease)\b"
            r"|\brent(?:s|al)?\b[^.;\n]{0,64}?(?:per|/)\s*(?:mo(?:nth)?s?)\b"
            r"|\b(?:suites?|units?)\s+starting\b.{0,80}?\bper\s+month\b",
            re.I,
        )
        frequency_matches = list(rental_frequency_re.finditer(description))
        if frequency_matches:
            labelled_amounts: List[float] = []
            for frequency_match in frequency_matches:
                window = description[
                    max(0, frequency_match.start() - 96) : frequency_match.end() + 96
                ]
                for amount_match in re.finditer(
                    r"\$\s*(\d[\d,]*)(?:\.(\d{1,2}))?", window
                ):
                    labelled_amounts.append(
                        float(
                            amount_match.group(1).replace(",", "")
                            + f".{amount_match.group(2) or '0'}"
                        )
                    )
            if not labelled_amounts or min(labelled_amounts) <= amount <= max(labelled_amounts):
                return "month"
        return None

    @staticmethod
    def _normalize_monthly_price(
        price_numeric: Optional[float], price_period: Optional[str]
    ) -> Optional[float]:
        if price_numeric is None or price_period is None:
            return None
        if price_period in {"month", "month_per_bedroom"}:
            return round(price_numeric, 2)
        if price_period == "week":
            return round(price_numeric * WEEKS_PER_YEAR / MONTHS_PER_YEAR, 2)
        if price_period == "day":
            return round(price_numeric * DAYS_PER_YEAR / MONTHS_PER_YEAR, 2)
        return None

    @staticmethod
    def _parse_available_now(date_available: Optional[str], description: Optional[str]) -> Optional[bool]:
        text = f"{date_available or ''} {description or ''}".lower()
        if not text.strip(): return None
        if any(x in text for x in ["immediate", "available now", "move in now", "asap"]): return True
        return None

    # ── Rule extractors ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_parking_fields(
        amenities_list: List[str],
        description: Optional[str],
    ) -> Tuple[Optional[bool], Optional[int]]:
        """Site emits ['Parking', '(1)'] as adjacent tokens; we detect the pair."""
        parking_available: Optional[bool] = None
        parking_spaces:    Optional[int]  = None

        for i, item in enumerate(amenities_list):
            if PARKING_TOKEN_RE.match(item):
                parking_available = True
                if i + 1 < len(amenities_list):
                    m = PARKING_COUNT_TOKEN_RE.match(amenities_list[i + 1])
                    if m:
                        parking_spaces = int(m.group(1))
                break
            m = re.match(r"^\s*parking\s*\((\d+)\)\s*$", item, re.I)
            if m:
                parking_available = True
                parking_spaces    = int(m.group(1))
                break

        lower_text = f"{' | '.join(amenities_list)} {description or ''}".lower()
        if parking_available is None:
            if "parking" in lower_text:
                parking_available = False if any(
                    x in lower_text for x in ["no parking", "without parking", "parking not"]
                ) else True

        if parking_available and parking_spaces is None:
            m = re.search(r"(\d+)\s+(?:parking\s+)?spaces?", lower_text)
            if m:
                parking_spaces = int(m.group(1))

        return parking_available, parking_spaces

    @staticmethod
    def _parse_utilities_status(
        amenities_list: List[str],
        utilities_raw:  Optional[str],
        description:    Optional[str],
    ) -> Optional[str]:
        """
        Returns: "all_included" | "partially_included" | "not_included" | None

        "Utilities Incl" amenity token is highest confidence.
        UTILITIES_PARTIAL_RE is tight — only exact named-utility keywords directly
        before "included" will fire, blocking false positives like "snow removal included".
        """
        structured = re.sub(r"\s+", " ", (utilities_raw or "").strip().casefold())
        if structured in {"included", "utilities included"}:
            return "all_included"
        if structured in {"extra", "not included", "utilities extra"}:
            return "not_included"

        has_utilities_incl = any(UTILITIES_INCL_TOKEN_RE.match(a) for a in amenities_list)
        has_internet_incl  = any(INTERNET_INCL_TOKEN_RE.match(a) for a in amenities_list)
        has_cable_incl     = any(CABLE_INCL_TOKEN_RE.match(a) for a in amenities_list)

        if has_utilities_incl:
            return "all_included"

        combined = f"{utilities_raw or ''} {description or ''}".strip()

        if combined:
            if UTILITIES_ALL_RE.search(combined):
                return "all_included"
            has_none    = bool(UTILITIES_NONE_RE.search(combined))
            has_partial = bool(UTILITIES_PARTIAL_RE.search(combined))
            if has_partial or has_internet_incl or has_cable_incl:
                return "partially_included"
            if has_none:
                return "not_included"
        else:
            if has_internet_incl or has_cable_incl:
                return "partially_included"

        return None

    @staticmethod
    def _utilities_included_from_status(status: Optional[str]) -> Optional[bool]:
        if status == "all_included":                           return True
        if status == "not_included": return False
        return None

    @staticmethod
    def _detect_sublet(
        title: Optional[str],
        description: Optional[str],
        lease_term_raw: Optional[str],
        housing_type_raw: Optional[str] = None,
    ) -> Optional[bool]:
        """Returns True on strong explicit signals only. None = unknown (not False)."""
        if (housing_type_raw or "").strip().casefold() in {"sublet", "sublets"}:
            return True
        combined = f"{title or ''} {description or ''} {lease_term_raw or ''}".strip()
        if not combined: return None
        if SUBLET_RE.search(combined): return True
        return None

    @staticmethod
    def _parse_bathroom_type_rule(amenities_list: List[str]) -> Optional[str]:
        for item in amenities_list:
            t = item.strip().lower()
            if t in {"private bathroom", "private bath", "ensuite bathroom", "en-suite bathroom"}: return "private"
            if t in {"shared bathroom", "shared bath", "common bathroom"}:                         return "shared"
        return None

    @staticmethod
    def _parse_amenity_bool(amenities_list: List[str], match_tokens: set) -> Optional[bool]:
        """True if any token matches. None if absent (absence ≠ False)."""
        for item in amenities_list:
            if item.strip().lower() in match_tokens:
                return True
        return None

    @staticmethod
    def _parse_furnished_rule(amenities_list: List[str], description: Optional[str]) -> Optional[bool]:
        """
        Amenity tokens first (authoritative). If found → True (no description check needed).
        Only falls to description if no amenity signal.
        """
        furnished_tokens = {"furniture incl", "furnished bdrm(s)", "furnished room", "fully furnished"}
        for item in amenities_list:
            if item.strip().lower() in furnished_tokens:
                return True

        if not description:
            return None
        if UNFURNISHED_RE.search(description) or FURNISHINGS_NOT_INCLUDED_RE.search(description):
            return False
        if FURNISHED_RE.search(description):   return True
        return None

    @staticmethod
    def _parse_bathrooms_rule(description: Optional[str]) -> Optional[float]:
        """Extract an explicit whole-property/unit bathroom total.

        The patterns deliberately avoid generic mentions such as "bathroom on
        each floor" or counts describing only one tenant group.
        """
        if not description:
            return None
        matches: List[float] = []
        for pattern in _BATHROOM_TOTAL_PATTERNS:
            for match in pattern.finditer(description):
                context_after = description[match.end() : match.end() + 32]
                if re.match(
                    r"\s+(?:on|for|per)\s+(?:the\s+)?(?:each|main|upper|lower|first|second)\b",
                    context_after,
                    re.I,
                ):
                    continue
                raw = match.group("count").casefold()
                value = _NUMBER_WORDS.get(raw)
                if value is None:
                    value = float(raw)
                matches.append(value)
        unique = set(matches)
        return matches[0] if len(unique) == 1 else None

    @staticmethod
    def _recover_address_from_description(description: Optional[str]) -> Optional[str]:
        """Recover only explicitly labelled civic addresses from captured prose."""
        if not description:
            return None
        match = DESCRIPTION_ADDRESS_RE.search(description)
        return UWOListingScraper._clean_text(match.group("address")) if match else None

    @staticmethod
    def _parse_availability_category(
        title: Optional[str], description: Optional[str], date_available: Optional[str]
    ) -> Optional[str]:
        """Return the canonical category from deterministic availability evidence."""
        return UWOListingScraper._parse_availability_evidence(
            title, description, date_available
        ).category

    @staticmethod
    def _parse_availability_evidence(
        title: Optional[str], description: Optional[str], date_available: Optional[str]
    ) -> AvailabilityClassification:
        """Classify explicit closed intervals without fabricating missing boundaries.

        A structured ``Date Available`` interval wins over title/description text.
        Closed yearless ranges use only their cyclic month membership; no calendar
        year is assigned. Open-ended dates remain unknown.
        """
        structured = UWOListingScraper._classify_availability_text(
            date_available, "structured_date_available"
        )
        text_results = [
            UWOListingScraper._classify_availability_text(
                description, "deterministic_description_date_range"
            ),
            UWOListingScraper._classify_availability_text(
                title, "deterministic_title_date_range"
            ),
        ]
        text_results = [result for result in text_results if result.category]

        if structured.category:
            conflict = structured.conflict or any(
                result.category != structured.category for result in text_results
            )
            return AvailabilityClassification(
                category=structured.category,
                source=structured.source,
                evidence=structured.evidence,
                start_month=structured.start_month,
                end_month=structured.end_month,
                start_year=structured.start_year,
                end_year=structured.end_year,
                conflict=conflict,
            )

        if text_results:
            categories = {result.category for result in text_results}
            if len(categories) > 1 or any(result.conflict for result in text_results):
                return AvailabilityClassification(conflict=True)
            return text_results[0]

        return AvailabilityClassification()

    @staticmethod
    def _classify_availability_text(
        text: Optional[str], source: str
    ) -> AvailabilityClassification:
        if not text:
            return AvailabilityClassification()

        results: List[AvailabilityClassification] = []
        for match in AVAILABILITY_RANGE_RE.finditer(text):
            if (
                source != "structured_date_available"
                and UWOListingScraper._availability_interval_role(text, match)
                != "availability"
            ):
                continue
            start_month = MONTH_NUMBERS[match.group("start_month").rstrip(".").casefold()]
            end_month = MONTH_NUMBERS[match.group("end_month").rstrip(".").casefold()]
            start_year = int(match.group("start_year")) if match.group("start_year") else None
            end_year = int(match.group("end_year")) if match.group("end_year") else None
            months = UWOListingScraper._closed_interval_months(
                start_month, end_month, start_year, end_year
            )
            if months is None:
                continue
            category = (
                "summer_available" if months & SUMMER_MONTHS else "non_summer"
            )
            results.append(
                AvailabilityClassification(
                    category=category,
                    source=source,
                    evidence=match.group(0).strip(),
                    start_month=start_month,
                    end_month=end_month,
                    start_year=start_year,
                    end_year=end_year,
                )
            )

        summer_match = UWOListingScraper._find_summer_availability_match(
            text, structured=source == "structured_date_available"
        )
        categories = {result.category for result in results}
        if summer_match:
            categories.add("summer_available")
        if len(categories) > 1:
            if results:
                result = results[0]
                return AvailabilityClassification(
                    category=result.category,
                    source=result.source,
                    evidence=result.evidence,
                    start_month=result.start_month,
                    end_month=result.end_month,
                    start_year=result.start_year,
                    end_year=result.end_year,
                    conflict=True,
                )
            return AvailabilityClassification(conflict=True)
        if results:
            return results[0]
        if summer_match:
            return AvailabilityClassification(
                category="summer_available",
                source=source.replace("_date_range", "_explicit_summer_text"),
                evidence=summer_match.group(0),
            )
        return AvailabilityClassification()

    @staticmethod
    def _find_summer_availability_match(
        text: str, *, structured: bool
    ) -> Optional[re.Match[str]]:
        sentence_start = 0
        boundaries = list(CONTEXT_BOUNDARY_RE.finditer(text))
        for boundary in [*boundaries, None]:
            sentence_end = boundary.start() if boundary else len(text)
            for pattern in SUMMER_AVAILABILITY_PATTERNS:
                match = pattern.search(text, sentence_start, sentence_end)
                if match and (
                    structured
                    or UWOListingScraper._availability_interval_role(text, match)
                    == "availability"
                ):
                    return match
            if boundary is not None:
                sentence_start = boundary.end()
        return None

    @staticmethod
    def _availability_interval_role(text: str, match: re.Match[str]) -> str:
        """Return availability, pricing, or ambiguous for one local span.

        Signals are scoped to the containing sentence and a bounded window so a
        price elsewhere in the description cannot suppress genuine occupancy
        evidence. The closest direct signal wins; an otherwise isolated closed
        range retains the established bare-range contract.
        """
        span_start, span_end = match.span()
        while span_end > span_start and text[span_end - 1].isspace():
            span_end -= 1
        while span_end > span_start and text[span_end - 1] in ".!?;":
            span_end -= 1

        sentence_start = 0
        preceding_boundary: Optional[re.Match[str]] = None
        for boundary in CONTEXT_BOUNDARY_RE.finditer(text, 0, span_start):
            sentence_start = boundary.end()
            preceding_boundary = boundary
        boundary_after = CONTEXT_BOUNDARY_RE.search(text, span_end)
        sentence_end = boundary_after.start() if boundary_after else len(text)

        label_start = sentence_start
        if preceding_boundary and preceding_boundary.group(0) == ":":
            label_start = max(0, sentence_start - 48)
        context_start = max(label_start, span_start - 96)
        context_end = min(sentence_end, span_end + 96)
        context = text[context_start:context_end]
        range_start = span_start - context_start
        range_end = span_end - context_start

        def signal_distances(
            pattern: re.Pattern[str],
        ) -> tuple[Optional[int], Optional[int], Optional[int]]:
            before = []
            after = []
            overlapping = []
            for signal in pattern.finditer(context):
                if signal.end() <= range_start:
                    before.append(range_start - signal.end())
                elif signal.start() >= range_end:
                    after.append(signal.start() - range_end)
                else:
                    overlapping.append(0)
            all_distances = [*before, *after, *overlapping]
            return (
                min(all_distances) if all_distances else None,
                min(before) if before else None,
                min(after) if after else None,
            )

        availability_distance, availability_before, _availability_after = (
            signal_distances(AVAILABILITY_CONTEXT_RE)
        )
        pricing_distance, pricing_before, _pricing_after = signal_distances(
            PRICING_CONTEXT_RE
        )
        if availability_before is not None and (
            pricing_before is None or availability_before <= pricing_before
        ):
            return "availability"
        if availability_distance is not None and (
            pricing_distance is None or availability_distance <= pricing_distance
        ):
            return "availability"
        if pricing_distance is not None:
            return "pricing"

        sentence = text[sentence_start:sentence_end]
        relative_start = span_start - sentence_start
        relative_end = span_end - sentence_start
        remainder = sentence[:relative_start] + sentence[relative_end:]
        if not re.sub(r"[\W_]+", "", remainder, flags=re.UNICODE):
            return "availability"
        return "ambiguous"

    @staticmethod
    def _closed_interval_months(
        start_month: int,
        end_month: int,
        start_year: Optional[int],
        end_year: Optional[int],
    ) -> Optional[set[int]]:
        if start_year is not None and end_year is not None:
            start_index = start_year * 12 + start_month - 1
            end_index = end_year * 12 + end_month - 1
            if end_index < start_index:
                return None
            if end_index - start_index >= 11:
                return set(range(1, 13))
            return {
                index % 12 + 1 for index in range(start_index, end_index + 1)
            }

        months = {start_month}
        month = start_month
        while month != end_month:
            month = month % 12 + 1
            months.add(month)
        return months

    @staticmethod
    def _parse_lease_term_months_rule(lease_term_raw: Optional[str]) -> Optional[int]:
        """
        Handles: "12", "12.0", "4 (Negotiable)", "4.0 (Negotiable)", "12-month", "12 months"
        Returns int in range [1, 36] or None.
        """
        if lease_term_raw is None:
            return None
        text = str(lease_term_raw).strip()
        if not text or text.lower() in {"nan", "none", ""}:
            return None

        # Leading integer or float with optional trailing parenthetical / text
        m = LEASE_FLOAT_RE.match(text)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 36:
                return val

        # "N-month" or "N month" anywhere in string
        m = LEASE_MONTHS_TEXT_RE.search(text)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 36:
                return val

        # Final fallback: any leading integer
        m = INT_RE.match(text)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 36:
                return val

        return None

    @staticmethod
    def _parse_lease_type_rule(
        is_sublet:         Optional[bool],
        lease_term_months: Optional[int],
        description:       Optional[str],
    ) -> Optional[str]:
        """
        Priority:
          1. is_sublet=True              → "sublet"
          2. month-to-month in desc      → "short_term"
          3. 12 months                   → "standard"
          4. 8 / 9 / 10 months           → "fixed_term"  (academic year)
          5. 1–7 months                  → "short_term"
          6. otherwise                   → None  (AI decides)
        """
        if is_sublet is True:
            return "sublet"
        if description and MONTH_TO_MONTH_RE.search(description):
            return "short_term"
        if lease_term_months is not None:
            if lease_term_months == 12:              return "standard"
            if lease_term_months in {8, 9, 10}:      return "fixed_term"
            if 1 <= lease_term_months <= 7:           return "short_term"
        return None


# ─── CSV pipeline helpers ─────────────────────────────────────────────────────

def extract_urls_from_input_csv(input_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(input_csv)
    if "item_page_link" not in df.columns:
        raise ValueError("Input CSV must contain an 'item_page_link' column.")
    out = df[df["item_page_link"].notna()].copy()
    out["item_page_link"] = out["item_page_link"].astype(str).str.strip()
    out = out[out["item_page_link"].str.contains("/Listings/Details/", na=False)]
    return out.drop_duplicates(subset=["item_page_link"]).reset_index(drop=True)


def merge_with_original_rows(original_df: pd.DataFrame, detail_df: pd.DataFrame) -> pd.DataFrame:
    merged = original_df.copy()
    merged["item_page_link"] = merged["item_page_link"].astype(str).str.strip()
    detail_df = detail_df.copy()
    detail_df["item_page_link"] = detail_df["source_url"]
    return merged.merge(detail_df.drop(columns=["source_url"]), on="item_page_link", how="left")


def build_website_ready(detail_df: pd.DataFrame) -> pd.DataFrame:
    """Output for ai_enricher.py. Includes all *_rule and *_source columns."""
    website_ready = detail_df.copy()
    website_ready["listing_url"] = website_ready["source_url"]
    columns = [
        "listing_id", "listing_url", "title", "address_raw", "address",
        "price_numeric", "price_text", "price_period", "price_monthly",
        "housing_type_raw", "housing_type", "bedrooms_raw", "bedrooms",
        "utilities_raw", "utilities_included_rule", "utilities_status", "utilities_status_source",
        "date_available_raw", "date_available", "availability_text", "available_now_rule",
        "lease_term_raw", "lease_term_months_rule", "lease_type_rule",
        "location_area_raw", "location_area",
        "distance_to_campus_raw", "distance_to_campus_km",
        "preferred_gender_raw", "preferred_gender_rule",
        "tenant_type_raw", "tenant_type_rule", "smoking_raw", "smoking_allowed_rule",
        "description", "amenities", "amenities_list",
        "parking_available_rule", "parking_spaces_rule",
        "air_conditioning_rule", "laundry_rule", "dishwasher_rule",
        "furnished_rule", "bathrooms_rule", "bathroom_type_rule",
        "is_sublet", "is_sublet_source",
        "available_from", "available_to", "availability_category",
        "availability_category_source", "availability_category_evidence",
        "availability_category_conflict",
        "rental_arrangement", "sublet_type",
        "transit_routes", "landlord_name", "landlord_phone",
        "scraped_ok", "scrape_error",
    ]
    present = [c for c in columns if c in website_ready.columns]
    return website_ready[present]


def scrape_all(
    input_csv:      Path,
    output_csv:     Path,
    output_json:    Optional[Path] = None,
    checkpoint_csv: Optional[Path] = None,
    limit:          Optional[int]  = None,
    delay_min:      float          = 0.3,
    delay_max:      float          = 0.8,
    merged_csv:     Optional[Path] = None,
    website_ready_csv: Optional[Path] = None,
) -> pd.DataFrame:
    original_df = pd.read_csv(input_csv)
    urls_df     = extract_urls_from_input_csv(input_csv)
    if limit is not None:
        urls_df = urls_df.head(limit).copy()

    scraper  = UWOListingScraper(delay_min=delay_min, delay_max=delay_max)
    records: List[Dict[str, object]] = []

    for idx, row in urls_df.iterrows():
        url    = row["item_page_link"]
        record = scraper.scrape_listing(url)
        records.append(asdict(record))
        print(f"[{idx + 1}/{len(urls_df)}] {url} -> ok={record.scraped_ok}")

        if checkpoint_csv and (idx + 1) % 25 == 0:
            pd.DataFrame(records).to_csv(checkpoint_csv, index=False)
            print(f"  Checkpoint saved to {checkpoint_csv}")

        if idx + 1 < len(urls_df):
            scraper.polite_pause()

    detail_df = pd.DataFrame(records)
    detail_df.to_csv(output_csv, index=False)
    print(f"Saved detail rows → {output_csv}")

    merged_path = merged_csv or output_csv.with_name(output_csv.stem + "_merged.csv")
    merge_with_original_rows(original_df, detail_df).to_csv(merged_path, index=False)
    print(f"Saved merged rows → {merged_path}")

    if output_json:
        detail_df.to_json(output_json, orient="records", indent=2, force_ascii=False)
        print(f"Saved JSON → {output_json}")

    website_ready      = build_website_ready(detail_df)
    website_ready_path = website_ready_csv or output_csv.with_name(
        output_csv.stem + "_website_ready.csv"
    )
    website_ready.to_csv(website_ready_path, index=False)
    print(f"Saved website-ready CSV → {website_ready_path}")

    return detail_df


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape UWO off-campus listing detail pages.")
    parser.add_argument("input_csv", nargs="?", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--output-json",    type=Path, default=None)
    parser.add_argument("--checkpoint-csv", type=Path)
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="New or explicitly resumed versioned run directory.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--limit",     type=int,   default=None)
    parser.add_argument("--delay-min", type=float, default=0.3)
    parser.add_argument("--delay-max", type=float, default=0.8)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if (args.resume or args.overwrite) and args.run_dir is None:
        parser.error("--resume and --overwrite require --run-dir")

    context = None
    merged_csv = None
    website_ready_csv = None

    if args.run_dir is not None:
        if args.input_csv is not None:
            parser.error("input_csv is selected automatically when --run-dir is used")
        if args.output_csv is not None or args.checkpoint_csv is not None:
            parser.error(
                "--output-csv and --checkpoint-csv cannot be combined with --run-dir"
            )
        if args.output_json is not None:
            parser.error("--output-json cannot be combined with --run-dir")

        configuration = {
            "limit": args.limit,
            "delay_min": args.delay_min,
            "delay_max": args.delay_max,
        }
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={"stage1": configuration},
        )
        input_csv = context.paths.stage0_listing_links
        output_csv = context.paths.stage1_details
        checkpoint_csv = context.paths.stage1_checkpoint
        merged_csv = context.paths.stage1_merged
        website_ready_csv = context.paths.stage1_website_ready
        outputs = [output_csv, checkpoint_csv, merged_csv, website_ready_csv]
        context.ensure_outputs_available(
            outputs, allow_existing=args.resume or args.overwrite
        )
        if not input_csv.exists():
            raise FileNotFoundError(f"Stage 0 output not found: {input_csv}")
        context.manifest["configuration"]["stage1"] = configuration
        context.start_stage(
            "stage1", input_paths=[input_csv], output_paths=outputs
        )
    else:
        if args.input_csv is None:
            parser.error("input_csv is required unless --run-dir is used")
        input_csv = args.input_csv
        output_csv = args.output_csv or Path("uwo_listing_details_ollama.csv")
        checkpoint_csv = args.checkpoint_csv or Path(
            "uwo_listing_details_ollama_checkpoint.csv"
        )

    try:
        input_rows = len(extract_urls_from_input_csv(input_csv))
        if args.limit is not None:
            input_rows = min(input_rows, args.limit)
        detail_df = scrape_all(
            input_csv=input_csv,
            output_csv=output_csv,
            output_json=args.output_json,
            checkpoint_csv=checkpoint_csv,
            limit=args.limit,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            merged_csv=merged_csv,
            website_ready_csv=website_ready_csv,
        )
        if context is not None:
            scrape_errors = int((detail_df["scraped_ok"] != True).sum())
            warnings = (
                [f"{scrape_errors} listing detail scrape(s) failed."]
                if scrape_errors
                else []
            )
            context.finish_stage(
                "stage1",
                input_rows=input_rows,
                output_rows=len(detail_df),
                warnings=warnings,
                error_count=scrape_errors,
            )
    except Exception as exc:
        if context is not None:
            context.fail_stage("stage1", exc)
        raise


if __name__ == "__main__":
    main()
