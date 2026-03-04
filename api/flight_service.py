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

# Statuses we consider viable for booking
VIABLE_STATUSES = {"scheduled", "estimated", "delayed", "departed"}

# Statuses to exclude
EXCLUDED_STATUSES = {"canceled", "cancelled", "diverted", "landed"}

# Cache: airport_iata -> {"flights": [...], "timestamp": unix_ts}
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

        # Fetch page 1 with limited retries (2 attempts total, 0.5s backoff)
        try:
            details = _retry_api_call(
                lambda: self.api.get_airport_details(icao, flight_limit=100, page=1),
                retries=2,
                backoff=0.5,
            )
            departures = (
                details
                .get("airport", {})
                .get("pluginData", {})
                .get("schedule", {})
                .get("departures", {})
                .get("data", [])
            )
            all_flights.extend(departures)
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

        for flight_data in all_flights:
            evac = self._process_flight(flight_data, airport_iata, airport_info, airlines_map)
            if evac:
                evac_flights.append(evac)

        evac_flights.sort(key=lambda f: f.scheduled_departure)

        # Update cache
        self._set_cached(airport_iata, evac_flights)

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

    def _set_cached(self, airport_iata: str, flights: list[EvacFlight]):
        """Cache results for an airport."""
        with _cache_lock:
            _results_cache[airport_iata] = {
                "flights": flights,
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
            flight_info = flight_data.get("flight", {})

            # Extract status
            status_data = flight_info.get("status", {})
            status_text = status_data.get("text", "").lower().strip()
            if not status_text:
                status_text = status_data.get("generic", {}).get("status", {}).get("text", "").lower().strip()

            # Filter: only viable statuses
            if status_text in EXCLUDED_STATUSES:
                return None

            # Extract destination
            dest_info = flight_info.get("airport", {}).get("destination", {})
            if not dest_info:
                return None

            dest_iata = dest_info.get("code", {}).get("iata", "")
            dest_icao = dest_info.get("code", {}).get("icao", "")
            dest_name = dest_info.get("name", "Unknown")
            dest_country = (
                dest_info.get("position", {})
                .get("country", {})
                .get("code", "")
            )

            # Filter: destination must be outside GCC
            if dest_iata and dest_iata.upper() in GCC_IATA_CODES:
                return None
            if dest_icao and dest_icao.upper() in GCC_ICAO_CODES:
                return None

            # Extract flight number
            identification = flight_info.get("identification", {})
            callsign = identification.get("number", {}).get("default", "")
            if not callsign:
                callsign = identification.get("callsign", "N/A")

            # Extract airline
            airline_info = flight_info.get("airline", {})
            airline_icao = airline_info.get("code", {}).get("icao", "")
            airline_name = airline_info.get("name", "")
            if not airline_name and airline_icao:
                airline_name = airlines_map.get(airline_icao, "Unknown Airline")

            # Extract departure time
            time_info = flight_info.get("time", {})
            dep_scheduled = time_info.get("scheduled", {}).get("departure", 0)
            dep_estimated = time_info.get("estimated", {}).get("departure", 0)
            dep_time = dep_estimated or dep_scheduled
            if dep_time:
                dep_dt = datetime.fromtimestamp(dep_time, tz=timezone.utc)
                dep_str = dep_dt.strftime("%Y-%m-%d %H:%M UTC")
            else:
                dep_str = "Unknown"

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
                status=status_text.capitalize() if status_text else "Unknown",
                aircraft_type=aircraft_type,
                booking_url=booking_url,
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
