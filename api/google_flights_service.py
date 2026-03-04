"""
Status-based flight availability fallback (no API key needed).

When Amadeus is not configured, we derive availability from flight status data:
- Scheduled/Estimated commercial flights are inherently bookable
- This is a reasonable heuristic: if FR24 shows a scheduled flight, seats
  exist for purchase (unless fully sold out, which is uncommon on most routes).

The frontend already provides direct booking links (Google Flights, KAYAK,
airline sites) for users to verify and purchase.

This module provides the same interface as availability_service so it
slots in as a drop-in fallback.
"""

import logging
import time
from threading import Lock

logger = logging.getLogger(__name__)

# Cache: "ORIGIN-DEST-DATE" -> {"result": ..., "ts": unix}
_status_cache: dict[str, dict] = {}
_status_lock = Lock()
STATUS_CACHE_TTL = 600  # 10 minutes


def check_availability_from_status(
    origin: str, dest: str, date: str, flights: list[dict] | None = None
) -> dict:
    """
    Derive availability from flight status data.

    When we know flights exist on a route (from FR24 data), we can infer
    they are bookable. This is the fallback when no API key is available.

    Args:
        origin: IATA code (e.g. "DXB")
        dest: IATA code (e.g. "LHR")
        date: ISO date string (e.g. "2026-03-04")
        flights: Optional list of flight dicts from FR24 for this route

    Returns standard availability dict.
    """
    origin = origin.upper()
    dest = dest.upper()
    cache_key = f"{origin}-{dest}-{date}"

    with _status_lock:
        cached = _status_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < STATUS_CACHE_TTL:
            return cached["result"]

    # If we have flight data, use it to determine availability
    if flights:
        bookable_statuses = {"scheduled", "estimated", "delayed", "departed", "unknown"}
        bookable = [
            f for f in flights
            if f.get("status", "").lower() in bookable_statuses
            and f.get("destination_iata", "").upper() == dest
        ]

        carriers = sorted({f.get("airline_name", "") for f in bookable if f.get("airline_name")})

        result = {
            "available": len(bookable) > 0,
            "offers_count": len(bookable),
            "seats_available": None,
            "cheapest": None,
            "carriers": carriers,
            "source": "flight_status",
            "error": None,
        }
    else:
        # No flight data provided — we can't determine availability
        result = {
            "available": None,
            "offers_count": 0,
            "seats_available": None,
            "cheapest": None,
            "carriers": [],
            "source": "flight_status",
            "error": None,
        }

    with _status_lock:
        _status_cache[cache_key] = {"result": result, "ts": time.time()}

    return result


def batch_check_from_status(routes: list[dict], all_flights: dict | None = None) -> dict:
    """
    Check availability for multiple routes using flight status data.

    Args:
        routes: [{"origin": "DXB", "dest": "LHR", "date": "2026-03-04"}, ...]
        all_flights: Optional dict of airport -> list of flight dicts from FR24

    Returns:
        {"DXB-LHR-2026-03-04": {availability result}, ...}
    """
    results = {}
    for route in routes:
        origin = route["origin"].upper()
        dest = route["dest"].upper()
        date = route["date"]
        key = f"{origin}-{dest}-{date}"

        if key not in results:
            # Find matching flights from FR24 data if available
            route_flights = None
            if all_flights and origin in all_flights:
                route_flights = [
                    f for f in all_flights[origin]
                    if f.get("destination_iata", "").upper() == dest
                ]

            results[key] = check_availability_from_status(
                origin, dest, date, route_flights
            )
    return results
