"""
Airport website scraper service — direct flight status from official airport sites.

CRITICAL: FR24 sometimes reports cancelled flights as "Scheduled". This service
scrapes official airport departure boards as a secondary data source to cross-verify
flight status and catch discrepancies.

Each airport scraper is isolated — one failure never blocks others.
Results are cached with stale fallback (same pattern as flight_service.py).
"""

import logging
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import Lock

from bs4 import BeautifulSoup

from gcc_data import GCC_AIRPORTS, GCC_IATA_CODES

logger = logging.getLogger(__name__)

# Browser-like headers to avoid being blocked by airport sites
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Cache: airport_iata -> {"flights": [...], "timestamp": float, "source_url": str}
_scrape_cache: dict[str, dict] = {}
_cache_lock = Lock()
CACHE_TTL_SECONDS = 300  # 5 minutes — airport sites update less frequently

# Request timeout — fast fail, don't let Vercel kill the function
REQUEST_TIMEOUT = 8


@dataclass
class ScrapedFlight:
    """A flight parsed from an official airport departure board."""
    flight_number: str
    airline: str
    destination: str
    destination_code: str  # IATA code if we can extract it, else ""
    scheduled_time: str    # Display string as shown on airport site
    status: str            # Raw status from airport site
    normalized_status: str  # One of: scheduled, delayed, cancelled, departed, boarding, unknown
    source_airport: str    # IATA of the airport we scraped
    source_url: str        # URL we scraped from

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Airport site configuration
# ---------------------------------------------------------------------------

AIRPORT_SOURCES = {
    "BAH": {
        "name": "Bahrain International Airport",
        "url": "https://www.bahrainairport.bh/flight-departures",
        "parser": "_parse_bahrain",
    },
    "MCT": {
        "name": "Muscat International Airport",
        "url": "https://www.muscatairport.co.om/flight-status?type=2",
        "parser": "_parse_muscat",
    },
    "SLL": {
        "name": "Salalah Airport",
        "url": "https://salalahairport.co.om/flight-status?type=2",
        "parser": "_parse_salalah",
    },
    "KWI": {
        "name": "Kuwait International Airport",
        "url": "https://www.kuwaitairport.gov.kw/en/flights-info/flight-status/departures/",
        "parser": "_parse_kuwait",
    },
    "DOH": {
        "name": "Hamad International Airport",
        "url": "https://dohahamadairport.com/airlines/flight-status?type=departures&day=today",
        "parser": "_parse_doha",
    },
    "DXB": {
        "name": "Dubai International Airport",
        "url": "https://www.dubaiairports.ae/flight-information/real-time-departures",
        "parser": "_parse_dubai",
    },
}


def get_supported_airports() -> list[str]:
    """Return IATA codes of airports we can scrape."""
    return list(AIRPORT_SOURCES.keys())


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    # Scheduled / On Time
    "scheduled": "scheduled",
    "on time": "scheduled",
    "on-time": "scheduled",
    "confirmed": "scheduled",
    "check-in": "scheduled",
    "check in": "scheduled",
    "go to gate": "scheduled",
    "gate open": "scheduled",
    "gate closed": "scheduled",
    "final call": "scheduled",
    "last call": "scheduled",
    # Boarding
    "boarding": "boarding",
    "now boarding": "boarding",
    # Delayed
    "delayed": "delayed",
    "late": "delayed",
    "rescheduled": "delayed",
    # Departed
    "departed": "departed",
    "airborne": "departed",
    "en route": "departed",
    "in flight": "departed",
    "took off": "departed",
    # Cancelled
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "cancel": "cancelled",
    # Diverted
    "diverted": "diverted",
    # Landed
    "landed": "landed",
    "arrived": "landed",
}


def _normalize_status(raw_status: str) -> str:
    """Normalize airport-specific status text to a standard value."""
    if not raw_status:
        return "unknown"
    cleaned = raw_status.strip().lower()
    # Direct match
    if cleaned in _STATUS_MAP:
        return _STATUS_MAP[cleaned]
    # Partial match — check if any key is contained in the status
    for key, val in _STATUS_MAP.items():
        if key in cleaned:
            return val
    return "unknown"


# ---------------------------------------------------------------------------
# HTTP fetch helper
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str | None:
    """Fetch an airport webpage. Returns HTML string or None on failure."""
    try:
        req = urllib.request.Request(url)
        for k, v in _HEADERS.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            # Handle different encodings
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP {e.code} fetching {url}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Generic table parser — many airport sites use HTML tables
# ---------------------------------------------------------------------------

def _parse_html_table(
    html: str,
    airport_iata: str,
    source_url: str,
    *,
    table_selector: str | None = None,
    flight_col: int = 0,
    airline_col: int = 1,
    dest_col: int = 2,
    time_col: int = 3,
    status_col: int = 4,
    min_cols: int = 4,
) -> list[ScrapedFlight]:
    """
    Generic parser for airport sites that render flights in HTML tables.

    Column indices are configurable per airport. Gracefully handles
    missing columns by returning partial data rather than failing.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the flight table
    if table_selector:
        table = soup.select_one(table_selector)
    else:
        # Try common patterns
        table = (
            soup.select_one("table.flight-table")
            or soup.select_one("table.departures-table")
            or soup.select_one("table.flights")
            or soup.select_one("#departures table")
            or soup.select_one(".departures table")
            or soup.select_one(".flight-board table")
            or soup.select_one("[data-flights] table")
            or soup.find("table")
        )

    if not table:
        logger.warning(f"No flight table found for {airport_iata}")
        return []

    flights = []
    rows = table.find_all("tr")

    for row in rows[1:]:  # Skip header row
        cells = row.find_all(["td", "th"])
        if len(cells) < min_cols:
            continue

        try:
            flight_num = _clean_text(cells[flight_col])
            airline = _clean_text(cells[airline_col]) if airline_col < len(cells) else ""
            dest_text = _clean_text(cells[dest_col]) if dest_col < len(cells) else ""
            sched_time = _clean_text(cells[time_col]) if time_col < len(cells) else ""
            status_text = _clean_text(cells[status_col]) if status_col < len(cells) else ""

            if not flight_num:
                continue

            # Try to extract IATA code from destination text
            dest_code = _extract_iata_code(dest_text)

            flights.append(ScrapedFlight(
                flight_number=flight_num,
                airline=airline,
                destination=dest_text,
                destination_code=dest_code,
                scheduled_time=sched_time,
                status=status_text,
                normalized_status=_normalize_status(status_text),
                source_airport=airport_iata,
                source_url=source_url,
            ))
        except Exception as e:
            logger.debug(f"Skipping row in {airport_iata}: {e}")
            continue

    return flights


def _parse_html_divs(
    html: str,
    airport_iata: str,
    source_url: str,
    *,
    container_selector: str,
    flight_selector: str = ".flight-number",
    airline_selector: str = ".airline",
    dest_selector: str = ".destination",
    time_selector: str = ".time, .scheduled",
    status_selector: str = ".status",
) -> list[ScrapedFlight]:
    """
    Parser for airport sites that use div-based layouts instead of tables.
    Common in modern responsive airport sites.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select(container_selector)

    if not container:
        logger.warning(f"No flight containers found for {airport_iata} with selector '{container_selector}'")
        return []

    flights = []
    for item in container:
        try:
            flight_num = _select_text(item, flight_selector)
            if not flight_num:
                continue

            airline = _select_text(item, airline_selector)
            dest_text = _select_text(item, dest_selector)
            sched_time = _select_text(item, time_selector)
            status_text = _select_text(item, status_selector)
            dest_code = _extract_iata_code(dest_text)

            flights.append(ScrapedFlight(
                flight_number=flight_num,
                airline=airline,
                destination=dest_text,
                destination_code=dest_code,
                scheduled_time=sched_time,
                status=status_text,
                normalized_status=_normalize_status(status_text),
                source_airport=airport_iata,
                source_url=source_url,
            ))
        except Exception as e:
            logger.debug(f"Skipping div item in {airport_iata}: {e}")
            continue

    return flights


# ---------------------------------------------------------------------------
# Per-airport parsers
# ---------------------------------------------------------------------------

def _parse_bahrain(html: str) -> list[ScrapedFlight]:
    """
    Parse Bahrain Airport departure board.
    bahrainairport.bh uses a table-based layout with flight info.
    """
    source = AIRPORT_SOURCES["BAH"]

    # Try table-based parsing first
    flights = _parse_html_table(
        html, "BAH", source["url"],
        flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
    )

    # Fallback: try div-based selectors common on this site
    if not flights:
        flights = _parse_html_divs(
            html, "BAH", source["url"],
            container_selector=".flight-row, .flight-item, .departures-row, [class*='flight']",
        )

    # Final fallback: broad search
    if not flights:
        flights = _broad_parse(html, "BAH", source["url"])

    return flights


def _parse_muscat(html: str) -> list[ScrapedFlight]:
    """
    Parse Muscat International Airport departure board.
    muscatairport.co.om — Oman Airports Management Company site.
    """
    source = AIRPORT_SOURCES["MCT"]

    flights = _parse_html_table(
        html, "MCT", source["url"],
        flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
    )

    if not flights:
        flights = _parse_html_divs(
            html, "MCT", source["url"],
            container_selector=".flight-row, .flight-item, .departures-row, [class*='flight']",
        )

    if not flights:
        flights = _broad_parse(html, "MCT", source["url"])

    return flights


def _parse_salalah(html: str) -> list[ScrapedFlight]:
    """
    Parse Salalah Airport departure board.
    salalahairport.co.om — same operator as Muscat (OAMC), similar structure.
    """
    source = AIRPORT_SOURCES["SLL"]

    flights = _parse_html_table(
        html, "SLL", source["url"],
        flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
    )

    if not flights:
        flights = _parse_html_divs(
            html, "SLL", source["url"],
            container_selector=".flight-row, .flight-item, .departures-row, [class*='flight']",
        )

    if not flights:
        flights = _broad_parse(html, "SLL", source["url"])

    return flights


def _parse_kuwait(html: str) -> list[ScrapedFlight]:
    """
    Parse Kuwait Airport departure board.
    kuwaitairport.gov.kw — government site, typically table-based.
    """
    source = AIRPORT_SOURCES["KWI"]

    flights = _parse_html_table(
        html, "KWI", source["url"],
        flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
    )

    if not flights:
        flights = _parse_html_divs(
            html, "KWI", source["url"],
            container_selector=".flight-row, .flight-item, .departures-row, .flight-info, [class*='flight']",
        )

    if not flights:
        flights = _broad_parse(html, "KWI", source["url"])

    return flights


def _parse_doha(html: str) -> list[ScrapedFlight]:
    """
    Parse Hamad International Airport (Doha) departure board.
    dohahamadairport.com — modern site, may use JS rendering.
    """
    source = AIRPORT_SOURCES["DOH"]

    # Doha's site often uses a flight-status component with specific classes
    flights = _parse_html_divs(
        html, "DOH", source["url"],
        container_selector=".flight-status-row, .flight-row, .flight-item, [class*='flight-status']",
    )

    if not flights:
        flights = _parse_html_table(
            html, "DOH", source["url"],
            flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
        )

    if not flights:
        flights = _broad_parse(html, "DOH", source["url"])

    return flights


def _parse_dubai(html: str) -> list[ScrapedFlight]:
    """
    Parse Dubai International Airport departure board.
    dubaiairports.ae — may load flights via JS/API.
    """
    source = AIRPORT_SOURCES["DXB"]

    flights = _parse_html_table(
        html, "DXB", source["url"],
        flight_col=0, airline_col=1, dest_col=2, time_col=3, status_col=4,
    )

    if not flights:
        flights = _parse_html_divs(
            html, "DXB", source["url"],
            container_selector=".flight-row, .flight-item, [class*='flight'], [class*='departure']",
        )

    if not flights:
        flights = _broad_parse(html, "DXB", source["url"])

    return flights


# ---------------------------------------------------------------------------
# Broad fallback parser — tries to find any flight-like data in the HTML
# ---------------------------------------------------------------------------

def _broad_parse(html: str, airport_iata: str, source_url: str) -> list[ScrapedFlight]:
    """
    Last-resort parser that searches for flight number patterns in the HTML
    and tries to extract surrounding context. Better than returning nothing.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Find flight numbers (2-letter airline code + 1-4 digit number)
    flight_pattern = re.compile(r'\b([A-Z]{2}\s?\d{1,4})\b')
    matches = flight_pattern.findall(text)

    if not matches:
        logger.info(f"No flight numbers found in broad parse for {airport_iata}")
        return []

    flights = []
    seen = set()
    for match in matches:
        fn = match.replace(" ", "")
        if fn in seen:
            continue
        seen.add(fn)

        flights.append(ScrapedFlight(
            flight_number=fn,
            airline="",
            destination="",
            destination_code="",
            scheduled_time="",
            status="",
            normalized_status="unknown",
            source_airport=airport_iata,
            source_url=source_url,
        ))

    return flights


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _clean_text(element) -> str:
    """Extract clean text from a BeautifulSoup element."""
    if element is None:
        return ""
    return " ".join(element.get_text(strip=True).split())


def _select_text(parent, selector: str) -> str:
    """Select first matching element and return its text."""
    # Handle comma-separated selectors
    el = parent.select_one(selector)
    if el:
        return _clean_text(el)
    return ""


def _extract_iata_code(text: str) -> str:
    """Try to extract a 3-letter IATA airport code from text."""
    if not text:
        return ""
    # Look for parenthesized codes like "London (LHR)" or "LHR - London"
    paren_match = re.search(r'\(([A-Z]{3})\)', text)
    if paren_match:
        return paren_match.group(1)
    # Look for standalone 3-letter codes
    code_match = re.search(r'\b([A-Z]{3})\b', text)
    if code_match:
        candidate = code_match.group(1)
        # Avoid matching common English words that are 3 uppercase letters
        if candidate not in {"THE", "AND", "FOR", "NOT", "ALL", "BUT", "ARE", "WAS", "HAS", "NEW", "VIA"}:
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Parser dispatch table
_PARSERS = {
    "_parse_bahrain": _parse_bahrain,
    "_parse_muscat": _parse_muscat,
    "_parse_salalah": _parse_salalah,
    "_parse_kuwait": _parse_kuwait,
    "_parse_doha": _parse_doha,
    "_parse_dubai": _parse_dubai,
}


def scrape_airport(airport_iata: str) -> dict:
    """
    Scrape departure data from an airport's official website.

    Returns:
        {
            "airport": "BAH",
            "airport_name": "Bahrain International Airport",
            "source_url": "https://...",
            "flights": [ScrapedFlight.to_dict(), ...],
            "flight_count": int,
            "scraped_at": "2026-03-05 12:00:00 UTC",
            "from_cache": bool,
            "error": str | None,
        }
    """
    airport_iata = airport_iata.upper()

    if airport_iata not in AIRPORT_SOURCES:
        return {
            "airport": airport_iata,
            "airport_name": GCC_AIRPORTS.get(airport_iata, {}).get("name", "Unknown"),
            "source_url": "",
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": f"No scraper configured for {airport_iata}",
        }

    source = AIRPORT_SOURCES[airport_iata]

    # Check cache
    cached = _get_cached(airport_iata)
    cache_age = _get_cache_age(airport_iata)
    if cached is not None and cache_age is not None and cache_age < CACHE_TTL_SECONDS:
        return {
            "airport": airport_iata,
            "airport_name": source["name"],
            "source_url": source["url"],
            "flights": [f.to_dict() for f in cached],
            "flight_count": len(cached),
            "scraped_at": _utc_now(),
            "from_cache": True,
            "error": None,
        }

    # Fetch the page
    html = _fetch_page(source["url"])
    if not html:
        # Serve stale cache if available
        if cached is not None:
            logger.info(f"Serving stale cache for {airport_iata} scraper ({len(cached)} flights)")
            return {
                "airport": airport_iata,
                "airport_name": source["name"],
                "source_url": source["url"],
                "flights": [f.to_dict() for f in cached],
                "flight_count": len(cached),
                "scraped_at": _utc_now(),
                "from_cache": True,
                "error": "Failed to fetch — showing cached data",
            }
        return {
            "airport": airport_iata,
            "airport_name": source["name"],
            "source_url": source["url"],
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": "Failed to fetch airport page",
        }

    # Parse flights
    parser_name = source["parser"]
    parser_fn = _PARSERS.get(parser_name)
    if not parser_fn:
        return {
            "airport": airport_iata,
            "airport_name": source["name"],
            "source_url": source["url"],
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": f"Parser '{parser_name}' not found",
        }

    try:
        flights = parser_fn(html)
    except Exception as e:
        logger.error(f"Parser error for {airport_iata}: {e}")
        # Serve stale cache
        if cached is not None:
            return {
                "airport": airport_iata,
                "airport_name": source["name"],
                "source_url": source["url"],
                "flights": [f.to_dict() for f in cached],
                "flight_count": len(cached),
                "scraped_at": _utc_now(),
                "from_cache": True,
                "error": f"Parse error — showing cached data: {e}",
            }
        flights = []

    # Cache the results
    _set_cached(airport_iata, flights)

    return {
        "airport": airport_iata,
        "airport_name": source["name"],
        "source_url": source["url"],
        "flights": [f.to_dict() for f in flights],
        "flight_count": len(flights),
        "scraped_at": _utc_now(),
        "from_cache": False,
        "error": None,
    }


def scrape_all_airports(airports: list[str] | None = None) -> dict[str, dict]:
    """
    Scrape multiple airport sites. Each airport is independent.

    Returns: {"BAH": {scrape result}, "DOH": {scrape result}, ...}
    """
    if airports is None:
        airports = list(AIRPORT_SOURCES.keys())

    results = {}
    for iata in airports:
        iata = iata.upper()
        if iata not in AIRPORT_SOURCES:
            continue
        try:
            results[iata] = scrape_airport(iata)
        except Exception as e:
            logger.error(f"Unexpected error scraping {iata}: {e}")
            results[iata] = {
                "airport": iata,
                "airport_name": AIRPORT_SOURCES.get(iata, {}).get("name", "Unknown"),
                "source_url": AIRPORT_SOURCES.get(iata, {}).get("url", ""),
                "flights": [],
                "flight_count": 0,
                "scraped_at": _utc_now(),
                "from_cache": False,
                "error": str(e),
            }
        # Small delay between airports to be respectful
        time.sleep(0.3)

    return results


def cross_reference(
    fr24_flights: list[dict],
    scraped_flights: list[dict],
) -> list[dict]:
    """
    Cross-reference FR24 flights with airport-scraped data.

    If an FR24 flight shows as "scheduled" but the airport site shows
    "cancelled", flag it. This catches FR24's known reliability gaps.

    Returns a list of discrepancy dicts:
        [{"flight_number": "EK202", "fr24_status": "Scheduled",
          "airport_status": "cancelled", "source_airport": "DXB",
          "recommendation": "LIKELY CANCELLED"}, ...]
    """
    # Build lookup from scraped flights: flight_number -> scraped info
    scraped_map: dict[str, dict] = {}
    for sf in scraped_flights:
        fn = sf.get("flight_number", "").replace(" ", "").upper()
        if fn:
            scraped_map[fn] = sf

    discrepancies = []
    for fr24 in fr24_flights:
        fn = fr24.get("flight_number", "").replace(" ", "").upper()
        if not fn or fn not in scraped_map:
            continue

        scraped = scraped_map[fn]
        fr24_status = (fr24.get("status") or "").lower()
        airport_status = scraped.get("normalized_status", "unknown")

        # Key discrepancy: FR24 says active, airport says cancelled
        if fr24_status in ("scheduled", "estimated", "unknown") and airport_status == "cancelled":
            discrepancies.append({
                "flight_number": fn,
                "fr24_status": fr24.get("status", "Unknown"),
                "airport_status": scraped.get("status", ""),
                "normalized_airport_status": airport_status,
                "source_airport": scraped.get("source_airport", ""),
                "source_url": scraped.get("source_url", ""),
                "recommendation": "LIKELY CANCELLED",
            })
        # FR24 says active, airport says delayed
        elif fr24_status == "scheduled" and airport_status == "delayed":
            discrepancies.append({
                "flight_number": fn,
                "fr24_status": fr24.get("status", "Unknown"),
                "airport_status": scraped.get("status", ""),
                "normalized_airport_status": airport_status,
                "source_airport": scraped.get("source_airport", ""),
                "source_url": scraped.get("source_url", ""),
                "recommendation": "LIKELY DELAYED",
            })

    return discrepancies


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_cached(airport_iata: str) -> list[ScrapedFlight] | None:
    with _cache_lock:
        cached = _scrape_cache.get(airport_iata)
    if cached:
        return cached["flights"]
    return None


def _get_cache_age(airport_iata: str) -> float | None:
    with _cache_lock:
        cached = _scrape_cache.get(airport_iata)
    if cached:
        return time.time() - cached["timestamp"]
    return None


def _set_cached(airport_iata: str, flights: list[ScrapedFlight]):
    with _cache_lock:
        _scrape_cache[airport_iata] = {
            "flights": flights,
            "timestamp": time.time(),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
