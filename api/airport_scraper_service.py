"""
Airport direct status service — secondary flight data via AeroDataBox FIDS API.

CRITICAL: FR24 sometimes reports cancelled flights as "Scheduled". This service
uses AeroDataBox's FIDS (Flight Information Display System) API as a secondary
data source to cross-verify flight status and catch discrepancies.

Each airport is independent — one failure never blocks others.
Results are cached with stale fallback (same pattern as flight_service.py).
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from threading import Lock

from gcc_data import GCC_AIRPORTS, GCC_IATA_CODES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AeroDataBox API configuration
# ---------------------------------------------------------------------------
# Free tier via RapidAPI: ~300 calls/month
# Sign up at: https://rapidapi.com/aedbx-aedbx/api/aerodatabox
AERODATABOX_API_KEY = os.environ.get("AERODATABOX_API_KEY", "")
AERODATABOX_HOST = "aerodatabox.p.rapidapi.com"
AERODATABOX_BASE = f"https://{AERODATABOX_HOST}"

# ICAO codes for GCC airports (AeroDataBox works best with ICAO)
_IATA_TO_ICAO = {
    "DXB": "OMDB", "AUH": "OMAA", "SHJ": "OMSJ",
    "DOH": "OTHH", "RUH": "OERK", "JED": "OEJN",
    "DMM": "OEDF", "KWI": "OKBK", "BAH": "OBBI",
    "MCT": "OOMS", "SLL": "OOSA",
}

# Cache: airport_iata -> {"flights": [...], "timestamp": float}
_scrape_cache: dict[str, dict] = {}
_cache_lock = Lock()
CACHE_TTL_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScrapedFlight:
    """A flight from the AeroDataBox FIDS API."""
    flight_number: str
    airline: str
    destination: str
    destination_code: str
    scheduled_time: str
    status: str
    normalized_status: str
    source_airport: str
    source_url: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Airport source metadata (kept for UI display + source links)
# ---------------------------------------------------------------------------

AIRPORT_SOURCES = {
    "BAH": {
        "name": "Bahrain International Airport",
        "url": "https://www.bahrainairport.bh/flight-departures",
    },
    "MCT": {
        "name": "Muscat International Airport",
        "url": "https://www.muscatairport.co.om/flight-status?type=2",
    },
    "SLL": {
        "name": "Salalah Airport",
        "url": "https://salalahairport.co.om/flight-status?type=2",
    },
    "KWI": {
        "name": "Kuwait International Airport",
        "url": "https://www.kuwaitairport.gov.kw/en/flights-info/flight-status/departures/",
    },
    "DOH": {
        "name": "Hamad International Airport",
        "url": "https://dohahamadairport.com/airlines/flight-status?type=departures&day=today",
    },
    "DXB": {
        "name": "Dubai International Airport",
        "url": "https://www.dubaiairports.ae/flight-information/real-time-departures",
    },
    "AUH": {
        "name": "Zayed International Airport (Abu Dhabi)",
        "url": "https://www.zayedinternationalairport.ae/en/flights-and-check-in/flight-status/departures",
    },
    "RUH": {
        "name": "King Khalid International Airport (Riyadh)",
        "url": "https://www.kkia.sa/en/flights/departures-and-arrivals",
    },
    "JED": {
        "name": "King Abdulaziz International Airport (Jeddah)",
        "url": "https://www.kaia.sa/en/Flights?Departures=",
    },
    "DMM": {
        "name": "King Fahd International Airport (Dammam)",
        "url": "https://kfia.gov.sa/",
    },
    "SHJ": {
        "name": "Sharjah International Airport",
        "url": "https://www.sharjahairport.ae/en/traveller/flight-information/passenger-departures/",
    },
}


def is_configured() -> bool:
    """Check if AeroDataBox API key is set."""
    return bool(AERODATABOX_API_KEY)


def get_supported_airports() -> list[str]:
    """Return IATA codes of airports we support."""
    return list(AIRPORT_SOURCES.keys())


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "scheduled": "scheduled",
    "on time": "scheduled",
    "confirmed": "scheduled",
    "check-in": "scheduled",
    "gate open": "scheduled",
    "gate closed": "scheduled",
    "final call": "scheduled",
    "boarding": "boarding",
    "now boarding": "boarding",
    "delayed": "delayed",
    "late": "delayed",
    "rescheduled": "delayed",
    "departed": "departed",
    "airborne": "departed",
    "en route": "departed",
    "in flight": "departed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "diverted": "diverted",
    "landed": "landed",
    "arrived": "landed",
    "unknown": "unknown",
    "expected": "scheduled",
}


def _normalize_status(raw_status: str) -> str:
    """Normalize API status text to a standard value."""
    if not raw_status:
        return "unknown"
    cleaned = raw_status.strip().lower()
    if cleaned in _STATUS_MAP:
        return _STATUS_MAP[cleaned]
    for key, val in _STATUS_MAP.items():
        if key in cleaned:
            return val
    return "unknown"


# ---------------------------------------------------------------------------
# AeroDataBox API caller
# ---------------------------------------------------------------------------

def _fetch_departures_aerodatabox(airport_iata: str) -> list[ScrapedFlight]:
    """
    Fetch departures from AeroDataBox FIDS API.

    Endpoint: GET /flights/airports/iata/{code}/{fromLocal}/{toLocal}
    With ?direction=Departure&withCancelled=true&withCodeshared=false

    Returns list of ScrapedFlight or empty list on failure.
    """
    if not AERODATABOX_API_KEY:
        return []

    # Build time range: from 2 hours ago to 12 hours ahead (local airport time)
    # AeroDataBox expects local time, but also accepts UTC with offset
    now = datetime.now(timezone.utc)
    from_time = now - timedelta(hours=2)
    to_time = now + timedelta(hours=12)

    from_str = from_time.strftime("%Y-%m-%dT%H:%M")
    to_str = to_time.strftime("%Y-%m-%dT%H:%M")

    # Try IATA-based endpoint first
    url = (
        f"{AERODATABOX_BASE}/flights/airports/iata/{airport_iata}"
        f"/{from_str}/{to_str}"
        f"?direction=Departure&withCancelled=true&withCodeshared=false&withLocation=false"
    )

    source_url = AIRPORT_SOURCES.get(airport_iata, {}).get("url", "")

    try:
        req = urllib.request.Request(url)
        req.add_header("x-rapidapi-host", AERODATABOX_HOST)
        req.add_header("x-rapidapi-key", AERODATABOX_API_KEY)
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.error(f"AeroDataBox HTTP {e.code} for {airport_iata}")
        return []
    except Exception as e:
        logger.error(f"AeroDataBox fetch failed for {airport_iata}: {e}")
        return []

    # Parse response — AeroDataBox returns {"departures": [...]}
    departures = data.get("departures", [])
    if not departures:
        logger.info(f"AeroDataBox returned 0 departures for {airport_iata}")
        return []

    flights = []
    for dep in departures:
        try:
            # Flight number
            flight_num = dep.get("number", "")
            if not flight_num:
                continue

            # Airline
            airline_obj = dep.get("airline", {})
            airline_name = airline_obj.get("name", "")

            # Destination
            arrival = dep.get("arrival", {})
            arrival_airport = arrival.get("airport", {})
            dest_name = arrival_airport.get("name", "")
            dest_code = arrival_airport.get("iata", "")

            # Scheduled time
            departure_info = dep.get("departure", {})
            sched_local = departure_info.get("scheduledTimeLocal", "")
            # Extract just the time part: "2026-03-05T14:30+04:00" -> "14:30"
            sched_display = _extract_time(sched_local)

            # Status
            raw_status = dep.get("status", "Unknown")
            normalized = _normalize_status(raw_status)

            flights.append(ScrapedFlight(
                flight_number=flight_num,
                airline=airline_name,
                destination=dest_name,
                destination_code=dest_code,
                scheduled_time=sched_display,
                status=raw_status,
                normalized_status=normalized,
                source_airport=airport_iata,
                source_url=source_url,
            ))
        except Exception as e:
            logger.debug(f"Skipping flight in {airport_iata}: {e}")
            continue

    return flights


def _extract_time(iso_str: str) -> str:
    """Extract HH:MM from an ISO datetime string like '2026-03-05T14:30+04:00'."""
    if not iso_str:
        return ""
    try:
        # Handle both "2026-03-05T14:30+04:00" and "2026-03-05 14:30"
        if "T" in iso_str:
            time_part = iso_str.split("T")[1]
            # Remove timezone offset if present
            for sep in ("+", "-"):
                if sep in time_part and time_part.index(sep) > 0:
                    time_part = time_part[:time_part.index(sep)]
                    break
            return time_part[:5]  # "14:30"
        return iso_str[:5]
    except Exception:
        return iso_str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_airport(airport_iata: str) -> dict:
    """
    Get departure data for an airport via AeroDataBox FIDS API.

    Returns:
        {
            "airport": "DXB",
            "airport_name": "Dubai International Airport",
            "source_url": "https://...",
            "flights": [ScrapedFlight.to_dict(), ...],
            "flight_count": int,
            "scraped_at": "2026-03-05 12:00:00 UTC",
            "from_cache": bool,
            "error": str | None,
            "configured": bool,
        }
    """
    airport_iata = airport_iata.upper()
    source = AIRPORT_SOURCES.get(airport_iata, {})
    airport_name = source.get("name", GCC_AIRPORTS.get(airport_iata, {}).get("name", "Unknown"))
    source_url = source.get("url", "")

    if not AERODATABOX_API_KEY:
        return {
            "airport": airport_iata,
            "airport_name": airport_name,
            "source_url": source_url,
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": "AeroDataBox API not configured — set AERODATABOX_API_KEY",
            "configured": False,
        }

    # Check cache
    cached = _get_cached(airport_iata)
    cache_age = _get_cache_age(airport_iata)
    if cached is not None and cache_age is not None and cache_age < CACHE_TTL_SECONDS:
        return {
            "airport": airport_iata,
            "airport_name": airport_name,
            "source_url": source_url,
            "flights": [f.to_dict() for f in cached],
            "flight_count": len(cached),
            "scraped_at": _utc_now(),
            "from_cache": True,
            "error": None,
            "configured": True,
        }

    # Fetch from API
    flights = _fetch_departures_aerodatabox(airport_iata)

    if not flights and cached is not None:
        # Serve stale cache on failure
        logger.info(f"Serving stale cache for {airport_iata} ({len(cached)} flights)")
        return {
            "airport": airport_iata,
            "airport_name": airport_name,
            "source_url": source_url,
            "flights": [f.to_dict() for f in cached],
            "flight_count": len(cached),
            "scraped_at": _utc_now(),
            "from_cache": True,
            "error": "API fetch failed — showing cached data",
            "configured": True,
        }

    if not flights:
        return {
            "airport": airport_iata,
            "airport_name": airport_name,
            "source_url": source_url,
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": "No departures returned from AeroDataBox",
            "configured": True,
        }

    # Cache the results
    _set_cached(airport_iata, flights)

    return {
        "airport": airport_iata,
        "airport_name": airport_name,
        "source_url": source_url,
        "flights": [f.to_dict() for f in flights],
        "flight_count": len(flights),
        "scraped_at": _utc_now(),
        "from_cache": False,
        "error": None,
        "configured": True,
    }


def scrape_all_airports(airports: list[str] | None = None) -> dict[str, dict]:
    """
    Fetch departure data for multiple airports. Each airport is independent.

    Returns: {"DXB": {result}, "DOH": {result}, ...}
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
            logger.error(f"Unexpected error fetching {iata}: {e}")
            results[iata] = {
                "airport": iata,
                "airport_name": AIRPORT_SOURCES.get(iata, {}).get("name", "Unknown"),
                "source_url": AIRPORT_SOURCES.get(iata, {}).get("url", ""),
                "flights": [],
                "flight_count": 0,
                "scraped_at": _utc_now(),
                "from_cache": False,
                "error": str(e),
                "configured": is_configured(),
            }
        # Small delay between airports to respect rate limits
        time.sleep(0.3)

    return results


def cross_reference(
    fr24_flights: list[dict],
    scraped_flights: list[dict],
) -> list[dict]:
    """
    Cross-reference FR24 flights with AeroDataBox data.

    If FR24 shows "scheduled" but AeroDataBox shows "cancelled", flag it.
    This catches FR24's known reliability gaps.

    Returns a list of discrepancy dicts.
    """
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

        # Key discrepancy: FR24 says active, AeroDataBox says cancelled
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
