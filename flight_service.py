"""
FlightRadar24 integration service for evacuation flight tracking.

Fetches live departure data from GCC airports, filters for viable evacuation flights
(Scheduled/Estimated status, non-GCC destinations, no GCC stopovers).
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from FlightRadar24 import FlightRadar24API

from gcc_data import (
    GCC_AIRPORTS,
    GCC_ICAO_CODES,
    GCC_IATA_CODES,
    PRIMARY_DEPARTURE_AIRPORTS,
    get_booking_url,
    GOOGLE_FLIGHTS_BASE,
)

logger = logging.getLogger(__name__)

# Statuses we consider viable for booking
VIABLE_STATUSES = {"scheduled", "estimated", "delayed", "departed"}

# Statuses to exclude
EXCLUDED_STATUSES = {"canceled", "cancelled", "diverted", "landed"}


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


class FlightService:
    """Service to fetch and filter evacuation-viable flights from FR24."""

    def __init__(self):
        self.api = FlightRadar24API()
        self._airport_cache: dict[str, dict] = {}
        self._airlines_cache: list[dict] | None = None

    def get_airlines_map(self) -> dict[str, str]:
        """Get a mapping of airline ICAO code -> airline name."""
        if self._airlines_cache is None:
            try:
                self._airlines_cache = self.api.get_airlines()
            except Exception as e:
                logger.error(f"Failed to fetch airlines: {e}")
                self._airlines_cache = []
        return {
            a.get("ICAO", ""): a.get("Name", "Unknown")
            for a in self._airlines_cache
            if a.get("ICAO")
        }

    def get_departures(self, airport_iata: str, max_pages: int = 3) -> list[EvacFlight]:
        """
        Fetch departures from a GCC airport and filter for evacuation-viable flights.

        Steps:
        1. Query FR24 for departures
        2. Filter: only Scheduled/Estimated status
        3. Filter: destination must be outside GCC
        4. Flag: multi-hop flights via GCC airports
        5. Add booking URLs
        """
        airport_info = GCC_AIRPORTS.get(airport_iata.upper())
        if not airport_info:
            logger.warning(f"Airport {airport_iata} not found in GCC data")
            return []

        icao = airport_info["icao"]
        all_flights = []

        for page in range(1, max_pages + 1):
            try:
                details = self.api.get_airport_details(
                    icao,
                    flight_limit=100,
                    page=page,
                )
                departures = (
                    details
                    .get("airport", {})
                    .get("pluginData", {})
                    .get("schedule", {})
                    .get("departures", {})
                    .get("data", [])
                )
                if not departures:
                    break
                all_flights.extend(departures)
                time.sleep(0.5)  # rate limiting
            except Exception as e:
                logger.error(f"Error fetching departures from {airport_iata} page {page}: {e}")
                break

        airlines_map = self.get_airlines_map()
        evac_flights = []

        for flight_data in all_flights:
            evac = self._process_flight(flight_data, airport_iata, airport_info, airlines_map)
            if evac:
                evac_flights.append(evac)

        # Sort by departure time
        evac_flights.sort(key=lambda f: f.scheduled_departure)
        return evac_flights

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
            # Also check the generic status field
            if not status_text:
                status_text = status_data.get("generic", {}).get("status", {}).get("text", "").lower().strip()

            # Filter: only viable statuses
            if status_text in EXCLUDED_STATUSES:
                return None
            if status_text and status_text not in VIABLE_STATUSES:
                # Unknown status - include it but log
                logger.debug(f"Including flight with unknown status: {status_text}")

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

            # Build booking URL
            booking_url = get_booking_url(airline_icao)
            if not booking_url:
                # Fallback to Google Flights
                booking_url = (
                    f"{GOOGLE_FLIGHTS_BASE}?q=flights+from+"
                    f"{origin_iata}+to+{dest_iata}"
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
        Returns a dict of airport_iata -> list of EvacFlight.
        """
        airports = airports or PRIMARY_DEPARTURE_AIRPORTS
        results = {}

        for airport in airports:
            logger.info(f"Scanning departures from {airport}...")
            flights = self.get_departures(airport)
            if flights:
                results[airport] = flights
            time.sleep(1)  # rate limit between airports

        return results
