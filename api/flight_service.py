"""
FlightRadar24 integration service for evacuation flight tracking.

CRITICAL: This service supports emergency evacuation operations.
All API calls include retry logic. Failed airports never block others.
Results are cached so stale data can be served if the API goes down.
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from threading import Lock

from FlightRadar24 import FlightRadar24API

from gcc_data import (
    GCC_AIRPORTS,
    GCC_ICAO_CODES,
    GCC_IATA_CODES,
    PRIMARY_DEPARTURE_AIRPORTS,
    GOOGLE_FLIGHTS_BASE,
)

logger = logging.getLogger(__name__)

# Statuses we consider viable for booking — only flights people can still board.
# "departed" is excluded because you can't book a flight that already left.
VIABLE_STATUSES = {"scheduled", "estimated", "delayed"}

# Statuses to exclude explicitly (matched via startswith, since FR24
# returns e.g. "Departed 14:30" or "Landed 16:45")
EXCLUDED_STATUSES = {"canceled", "cancelled", "diverted", "landed", "departed"}

# Statuses that confirm a flight successfully left the airport
# (first-word match after lowercasing, e.g. "Departed 14:30" → "departed")
DEPARTED_STATUSES = {"departed", "landed", "airborne", "en"}  # "en" catches "En Route"

# Generic status types that mean departed (FR24 generic.status.type field)
DEPARTED_TYPES = {"departure"}

# How far back to count departed flights (seconds)
DEPARTED_WINDOW_SECONDS = 6 * 3600  # 6 hours


# IATA → ICAO airline code mapping (fallback when FR24 only provides IATA)
_IATA_TO_ICAO = {
    "EK": "UAE", "QR": "QTR", "EY": "ETD", "SV": "SVA", "GF": "GFA",
    "KU": "KAC", "WY": "OMA", "FZ": "FDB", "G9": "ABY", "XY": "NAS",
    "TK": "THY", "BA": "BAW", "LH": "DLH", "AF": "AFR", "KL": "KLM",
    "SQ": "SIA", "CX": "CPA", "DL": "DAL", "AA": "AAL", "UA": "UAL",
    "AI": "AIC", "6E": "IGO", "PR": "PAL", "MH": "MAS", "PK": "PIA",
    "MS": "EGF", "RJ": "RJA", "ME": "MEA", "ET": "ETH", "KQ": "KQA",
    "LX": "SWR", "OS": "AUA", "TP": "TAP", "W6": "WIZ",
}


def _normalize_status(status_data: dict) -> str:
    """Extract the base status word from FR24 status data.

    FR24 returns status text like "Departed 14:30", "Landed 16:45",
    "Estimated 15:00", etc.  We only care about the first word.
    Falls back to generic.status.text if the top-level text is empty.
    """
    raw = (status_data.get("text") or "").strip()
    if not raw:
        generic = (status_data.get("generic") or {}).get("status") or {}
        raw = (generic.get("text") or "").strip()
    # Take the first word only (e.g. "Departed 14:30" -> "departed")
    return raw.split()[0].lower() if raw else ""

# Cache: airport_iata -> {"flights": [...], "departed_count": int, "timestamp": unix_ts}
_results_cache: dict[str, dict] = {}
_cache_lock = Lock()
CACHE_TTL_SECONDS = 120  # serve cached data for up to 2 minutes


@dataclass
class EvacFlight:
    """A potential evacuation flight."""
    flight_number: str
    airline_name: str
    airline_icao: str
    origin_iata: str
    origin_name: str
    destination_iata: str
    destination_name: str
    destination_country: str
    scheduled_departure: str  # ISO format or display string
    status: str
    aircraft_type: str = ""
    booking_url: str = ""
    has_gcc_stopover: bool = False
    delay_minutes: int = 0  # >0 when estimated departure is later than scheduled

    def to_dict(self) -> dict:
        return asdict(self)


def _retry_api_call(fn, retries=3, backoff=1.0):
    """
    Retry an API call with exponential backoff.
    Critical for reliability when FR24 is under load.
    """
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            wait = backoff * (2 ** attempt)
            logger.warning(f"API call failed (attempt {attempt + 1}/{retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)
    raise last_err


class FlightService:
    """Service to fetch and filter evacuation-viable flights from FR24."""

    def __init__(self):
        self.api = FlightRadar24API()
        self.api.timeout = 4  # Fast fail — don't let Vercel kill the function
        self._airlines_cache: list[dict] | None = None
        self._airlines_map_cache: dict[str, str] | None = None

    def get_airlines_map(self) -> dict[str, str]:
        """Get a mapping of airline ICAO code -> airline name. Cached permanently."""
        if self._airlines_map_cache is not None:
            return self._airlines_map_cache

        try:
            airlines = _retry_api_call(self.api.get_airlines, retries=1, backoff=0.5)
            self._airlines_map_cache = {
                a.get("ICAO", ""): a.get("Name", "Unknown")
                for a in airlines
                if a.get("ICAO")
            }
        except Exception as e:
            logger.error(f"Failed to fetch airlines: {e}")
            self._airlines_map_cache = {}

        return self._airlines_map_cache

    def get_departed_count(self, airport_iata: str) -> int:
        """Return the number of departed flights from cache for an airport."""
        with _cache_lock:
            cached = _results_cache.get(airport_iata.upper())
        if cached:
            return cached.get("departed_count", 0)
        return 0

    def get_departures(self, airport_iata: str) -> list[EvacFlight]:
        """
        Fetch departures from a GCC airport and filter for evacuation-viable flights.

        CRITICAL: This method NEVER raises exceptions. It returns cached data or
        an empty list on failure. In an evacuation, partial/stale data is infinitely
        better than an error page.
        """
        airport_iata = airport_iata.upper()
        airport_info = GCC_AIRPORTS.get(airport_iata)
        if not airport_info:
            logger.warning(f"Airport {airport_iata} not found in GCC data")
            return []

        # Serve from cache if fresh enough (avoids unnecessary API calls)
        cached = self._get_cached(airport_iata)
        cache_age = self._get_cache_age(airport_iata)
        if cached and cache_age is not None and cache_age < CACHE_TTL_SECONDS:
            logger.info(f"Serving fresh cache for {airport_iata} (age: {cache_age:.0f}s)")
            return cached

        icao = airport_info["icao"]
        all_flights = []
        schedule_total = 0  # Total departures reported by FR24 schedule

        # Fetch page 1 with limited retries (2 attempts total, 0.5s backoff)
        try:
            details = _retry_api_call(
                lambda: self.api.get_airport_details(icao, flight_limit=100, page=1),
                retries=2,
                backoff=0.5,
            )
            dep_section = (
                details
                .get("airport", {})
                .get("pluginData", {})
                .get("schedule", {})
                .get("departures", {})
            )
            all_flights.extend(dep_section.get("data", []))
            # FR24 reports total scheduled departures in item.total
            schedule_total = (dep_section.get("item") or {}).get("total", 0)
        except Exception as e:
            logger.error(f"All retries failed for {airport_iata} page 1: {e}")
            # Return stale cache (any age) — stale data beats no data
            if cached:
                logger.info(f"Serving stale cache for {airport_iata} ({len(cached)} flights)")
                return cached
            return []

        # Page 2 best-effort only — keeps total request time under Vercel limits
        try:
            details = self.api.get_airport_details(icao, flight_limit=100, page=2)
            departures = (
                details
                .get("airport", {})
                .get("pluginData", {})
                .get("schedule", {})
                .get("departures", {})
                .get("data", [])
            )
            if departures:
                all_flights.extend(departures)
        except Exception as e:
            logger.warning(f"Failed to fetch {airport_iata} page 2 (non-critical): {e}")

        airlines_map = self.get_airlines_map()
        evac_flights = []
        departed_count = 0
        now_ts = time.time()
        cutoff_ts = now_ts - DEPARTED_WINDOW_SECONDS

        for flight_data in all_flights:
            flight_info = flight_data.get("flight") or {}

            # Count departed flights using MULTIPLE signals from FR24:
            status_data = flight_info.get("status") or {}
            status_word = _normalize_status(status_data)

            # Signal 1: FR24 "live" field — true when aircraft is tracked in-air
            is_live = bool(status_data.get("live"))

            # Signal 2: Generic status type (more reliable than text)
            generic = (status_data.get("generic") or {}).get("status") or {}
            generic_type = (generic.get("type") or "").lower()
            generic_text = (generic.get("text") or "").lower()

            # Signal 3: Departure timestamps
            time_info = flight_info.get("time") or {}
            dep_scheduled = ((time_info.get("scheduled") or {}).get("departure")) or 0
            dep_estimated = ((time_info.get("estimated") or {}).get("departure")) or 0
            dep_actual = ((time_info.get("real") or {}).get("departure")) or 0
            dep_time = dep_estimated or dep_scheduled

            # A flight counts as departed if ANY of these are true:
            flight_departed = False

            # 1. Status text says departed/landed/airborne
            if status_word in DEPARTED_STATUSES:
                flight_departed = True
            # 2. Generic status text says departed/landed
            elif generic_text in {"departed", "landed", "airborne", "en route"}:
                flight_departed = True
            # 3. Live tracking is active (aircraft in air)
            elif is_live:
                flight_departed = True
            # 4. Has an actual departure time recorded
            elif dep_actual and dep_actual > 0:
                flight_departed = True
            # 5. Departure time is in the past (within window)
            elif isinstance(dep_time, (int, float)) and dep_time > 0 and cutoff_ts <= dep_time < now_ts:
                flight_departed = True

            if flight_departed:
                departed_count += 1

            evac = self._process_flight(flight_data, airport_iata, airport_info, airlines_map)
            if evac:
                evac_flights.append(evac)

        evac_flights.sort(key=lambda f: f.scheduled_departure)

        # Use schedule_total as a floor for departed count at busy airports
        # (FR24 only returns ~200 flights but schedule_total reflects all)
        if schedule_total > len(all_flights) and departed_count > 0:
            # Estimate: proportion of departed in fetched data × total scheduled
            ratio = departed_count / max(len(all_flights), 1)
            estimated_total_departed = int(schedule_total * ratio)
            if estimated_total_departed > departed_count:
                departed_count = estimated_total_departed

        logger.info(
            f"{airport_iata}: {departed_count} departed out of "
            f"{len(all_flights)} fetched, schedule_total={schedule_total}"
        )

        # Update cache (includes departed count for airport status)
        self._set_cached(airport_iata, evac_flights, departed_count)

        return evac_flights

    def _get_cached(self, airport_iata: str) -> list[EvacFlight]:
        """Return cached results regardless of age. Stale data beats no data."""
        with _cache_lock:
            cached = _results_cache.get(airport_iata)
        if cached:
            return cached["flights"]
        return []

    def _get_cache_age(self, airport_iata: str) -> float | None:
        """Return cache age in seconds, or None if not cached."""
        with _cache_lock:
            cached = _results_cache.get(airport_iata)
        if cached:
            return time.time() - cached["timestamp"]
        return None

    def _set_cached(self, airport_iata: str, flights: list[EvacFlight], departed_count: int = 0):
        """Cache results for an airport."""
        with _cache_lock:
            _results_cache[airport_iata] = {
                "flights": flights,
                "departed_count": departed_count,
                "timestamp": time.time(),
            }

    def _process_flight(
        self,
        flight_data: dict,
        origin_iata: str,
        origin_info: dict,
        airlines_map: dict[str, str],
    ) -> EvacFlight | None:
        """Process a single flight record and return EvacFlight if viable."""
        try:
            # FR24 sometimes returns None for nested dicts, so use `or {}`
            # to guard every chained .get() — `.get("k", {})` does NOT help
            # when the key exists but its value is explicitly None.
            flight_info = flight_data.get("flight") or {}

            # Extract status — normalize "Departed 14:30" → "departed"
            status_data = flight_info.get("status") or {}
            status_text = _normalize_status(status_data)

            # Track whether this is a departed/landed flight (still shown, but marked)
            is_departed_status = status_text in EXCLUDED_STATUSES
            # Cancelled/diverted flights are still excluded — they're not useful
            if status_text in {"canceled", "cancelled", "diverted"}:
                return None

            # Extract destination
            airport_data = flight_info.get("airport") or {}
            dest_info = airport_data.get("destination") or {}
            if not dest_info:
                return None

            dest_code = dest_info.get("code") or {}
            dest_iata = dest_code.get("iata") or ""
            dest_icao = dest_code.get("icao") or ""
            dest_name = dest_info.get("name") or "Unknown"
            dest_position = dest_info.get("position") or {}
            dest_country = (dest_position.get("country") or {}).get("code") or ""

            # Filter: destination must be outside GCC
            if dest_iata and dest_iata.upper() in GCC_IATA_CODES:
                return None
            if dest_icao and dest_icao.upper() in GCC_ICAO_CODES:
                return None

            # Extract flight number
            identification = flight_info.get("identification") or {}
            number_info = identification.get("number") or {}
            callsign = number_info.get("default") or ""
            if not callsign:
                callsign = identification.get("callsign") or "N/A"

            # Extract airline — try ICAO first, fall back to IATA→ICAO mapping,
            # then try deriving from the flight number prefix (e.g. "EK202" → "EK" → "UAE")
            airline_info = flight_info.get("airline") or {}
            airline_code = airline_info.get("code") or {}
            airline_icao = airline_code.get("icao") or ""
            airline_iata = airline_code.get("iata") or ""
            airline_name = airline_info.get("name") or ""

            # Fallback: use IATA→ICAO mapping if ICAO is missing
            if not airline_icao and airline_iata:
                airline_icao = _IATA_TO_ICAO.get(airline_iata.upper(), "")

            # Fallback: derive from flight number prefix (e.g. "EK202" → "EK")
            if not airline_icao and callsign:
                prefix = ""
                for ch in callsign:
                    if ch.isdigit():
                        break
                    prefix += ch
                if prefix:
                    airline_icao = _IATA_TO_ICAO.get(prefix.upper(), "")

            if not airline_name and airline_icao:
                airline_name = airlines_map.get(airline_icao, "Unknown Airline")

            # Extract departure time and detect delays
            time_info = flight_info.get("time") or {}
            dep_scheduled = ((time_info.get("scheduled") or {}).get("departure")) or 0
            dep_estimated = ((time_info.get("estimated") or {}).get("departure")) or 0
            dep_time = dep_estimated or dep_scheduled
            if dep_time:
                dep_dt = datetime.fromtimestamp(dep_time, tz=timezone.utc)
                dep_str = dep_dt.strftime("%Y-%m-%d %H:%M UTC")
            else:
                dep_str = "Unknown"

            # Delay detection: if estimated is 15+ minutes after scheduled
            delay_minutes = 0
            if (
                isinstance(dep_scheduled, (int, float))
                and isinstance(dep_estimated, (int, float))
                and dep_scheduled > 0
                and dep_estimated > 0
                and dep_estimated > dep_scheduled
            ):
                diff = int((dep_estimated - dep_scheduled) / 60)
                if diff >= 15:
                    delay_minutes = diff

            # Extract aircraft
            aircraft = flight_info.get("aircraft", {})
            aircraft_type = ""
            if aircraft:
                model = aircraft.get("model", {})
                aircraft_type = model.get("text", "") if model else ""

            # Build booking URL via Google Search (shows flight booking cards)
            dep_date = dep_dt.strftime("%Y-%m-%d") if dep_time else ""
            date_part = f"+on+{dep_date}" if dep_date else ""
            booking_url = (
                f"https://www.google.com/search?q=flights+from+"
                f"{origin_iata}+to+{dest_iata}{date_part}"
            )

            # Override display status for clarity
            display_status = status_text.capitalize() if status_text else "Unknown"
            if is_departed_status:
                display_status = "Departed"
            elif delay_minutes > 0:
                display_status = "Delayed"

            return EvacFlight(
                flight_number=callsign,
                airline_name=airline_name,
                airline_icao=airline_icao,
                origin_iata=origin_iata,
                origin_name=origin_info["name"],
                destination_iata=dest_iata,
                destination_name=dest_name,
                destination_country=dest_country,
                scheduled_departure=dep_str,
                status=display_status,
                aircraft_type=aircraft_type,
                booking_url=booking_url,
                delay_minutes=delay_minutes,
            )

        except Exception as e:
            logger.error(f"Error processing flight: {e}")
            return None

    def scan_all_gcc_departures(self, airports: list[str] | None = None) -> dict[str, list[EvacFlight]]:
        """
        Scan multiple GCC airports for evacuation flights.
        Each airport is independent - one failure never blocks the rest.
        """
        airports = airports or PRIMARY_DEPARTURE_AIRPORTS
        results = {}

        for airport in airports:
            try:
                logger.info(f"Scanning departures from {airport}...")
                flights = self.get_departures(airport)
                if flights:
                    results[airport] = flights
            except Exception as e:
                logger.error(f"Failed to scan {airport}: {e}")
            time.sleep(0.5)

        return results
