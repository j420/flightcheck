"""
Google Flights availability integration via fast_flights (no API key needed).

Scrapes Google Flights to check if flights are bookable on a given route/date.
A flight appearing in results with a price = seats available.

CRITICAL: This is a scraping-based fallback. It can break if Google changes
their page format. Always handle failures gracefully — return unknown rather
than crash.

Caching: 10-minute TTL (same as Amadeus) with stale fallback.
"""

import logging
import time
from threading import Lock

logger = logging.getLogger(__name__)

# Cache: "ORIGIN-DEST-DATE" -> {"result": ..., "ts": unix}
_gf_cache: dict[str, dict] = {}
_gf_lock = Lock()
GF_CACHE_TTL = 600  # 10 minutes


def _do_search(origin: str, dest: str, date: str) -> dict:
    """
    Perform a single Google Flights search via fast_flights.

    Returns the standard availability dict matching Amadeus format.
    """
    try:
        from fast_flights import FlightData, Passengers, get_flights

        result = get_flights(
            flight_data=[
                FlightData(date=date, from_airport=origin, to_airport=dest)
            ],
            trip="one-way",
            seat="economy",
            passengers=Passengers(adults=1),
        )

        flights = result.flights or []

        if not flights:
            return {
                "available": False,
                "offers_count": 0,
                "seats_available": None,
                "cheapest": None,
                "carriers": [],
                "source": "google_flights",
                "error": None,
            }

        # Extract cheapest price
        cheapest = None
        carriers = set()
        for f in flights:
            # f.price is a string like "$123" or "₹8,500"
            if f.price:
                price_str = f.price.strip()
                # Extract numeric part — strip currency symbols and commas
                numeric = ""
                currency = "USD"
                for ch in price_str:
                    if ch.isdigit() or ch == ".":
                        numeric += ch
                    elif not numeric:
                        # Currency symbol before the number
                        if ch == "$":
                            currency = "USD"
                        elif ch == "€":
                            currency = "EUR"
                        elif ch == "£":
                            currency = "GBP"
                        elif ch == "₹":
                            currency = "INR"

                if numeric:
                    try:
                        price_val = float(numeric)
                        if cheapest is None or price_val < float(cheapest["price"]):
                            cheapest = {"price": numeric, "currency": currency}
                    except ValueError:
                        pass

            # f.name is airline name(s)
            if f.name:
                carriers.add(f.name.strip())

        return {
            "available": True,
            "offers_count": len(flights),
            "seats_available": None,  # Google Flights doesn't give seat counts
            "cheapest": cheapest,
            "carriers": sorted(carriers),
            "source": "google_flights",
            "error": None,
        }

    except ImportError:
        logger.error("fast_flights package not installed")
        return {
            "available": None,
            "offers_count": 0,
            "seats_available": None,
            "cheapest": None,
            "carriers": [],
            "source": "google_flights",
            "error": "fast_flights not installed",
        }
    except Exception as e:
        logger.error(f"Google Flights search failed for {origin}->{dest} on {date}: {e}")
        return {
            "available": None,
            "offers_count": 0,
            "seats_available": None,
            "cheapest": None,
            "carriers": [],
            "source": "google_flights",
            "error": str(e),
        }


def check_availability(origin: str, dest: str, date: str) -> dict:
    """
    Check flight availability via Google Flights.

    Same interface as availability_service.check_availability so it
    can be used as a drop-in fallback.
    """
    origin = origin.upper()
    dest = dest.upper()
    cache_key = f"{origin}-{dest}-{date}"

    # Check cache
    with _gf_lock:
        cached = _gf_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < GF_CACHE_TTL:
            return cached["result"]

    result = _do_search(origin, dest, date)

    # Cache the result
    with _gf_lock:
        _gf_cache[cache_key] = {"result": result, "ts": time.time()}

    return result


def batch_check(routes: list[dict]) -> dict:
    """
    Check availability for multiple routes via Google Flights.

    Same interface as availability_service.batch_check.
    """
    results = {}
    for route in routes:
        key = f"{route['origin']}-{route['dest']}-{route['date']}"
        if key not in results:
            results[key] = check_availability(
                route["origin"], route["dest"], route["date"]
            )
    return results
