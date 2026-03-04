"""
Amadeus Flight Offers integration for seat availability checking.

Uses the Amadeus Self-Service API to check if flights have bookable seats.
Requires AMADEUS_API_KEY and AMADEUS_API_SECRET environment variables.

Free tier: 2,000 calls/month — results are cached aggressively.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from threading import Lock

logger = logging.getLogger(__name__)

# Amadeus API endpoints
AMADEUS_BASE = os.environ.get(
    "AMADEUS_BASE_URL", "https://api.amadeus.com"
)
TOKEN_URL = f"{AMADEUS_BASE}/v1/security/oauth2/token"
OFFERS_URL = f"{AMADEUS_BASE}/v2/shopping/flight-offers"

# Token cache
_token_cache = {"token": None, "expires_at": 0}
_token_lock = Lock()

# Availability cache: "ORIGIN-DEST-DATE" -> {"result": ..., "ts": unix}
_avail_cache: dict[str, dict] = {}
_avail_lock = Lock()
AVAIL_CACHE_TTL = 600  # 10 minutes


def is_configured() -> bool:
    """Check if Amadeus credentials are set."""
    return bool(
        os.environ.get("AMADEUS_API_KEY")
        and os.environ.get("AMADEUS_API_SECRET")
    )


def _get_token() -> str | None:
    """Get a valid OAuth2 access token, refreshing if needed."""
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 30:
            return _token_cache["token"]

    key = os.environ.get("AMADEUS_API_KEY", "")
    secret = os.environ.get("AMADEUS_API_SECRET", "")
    if not key or not secret:
        return None

    try:
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": key,
            "client_secret": secret,
        }).encode()

        req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())

        with _token_lock:
            _token_cache["token"] = body["access_token"]
            _token_cache["expires_at"] = time.time() + body.get("expires_in", 1799)

        return body["access_token"]

    except Exception as e:
        logger.error(f"Amadeus auth failed: {e}")
        return None


def check_availability(origin: str, dest: str, date: str) -> dict:
    """
    Check seat availability for a route on a given date.

    Args:
        origin: IATA code (e.g. "DXB")
        dest: IATA code (e.g. "LHR")
        date: ISO date string (e.g. "2026-03-04")

    Returns:
        {
            "available": bool,
            "offers_count": int,
            "cheapest": {"price": "123.45", "currency": "USD"} | None,
            "carriers": ["EK", "BA", ...],
            "error": str | None,
        }
    """
    cache_key = f"{origin}-{dest}-{date}"

    # Check cache first
    with _avail_lock:
        cached = _avail_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < AVAIL_CACHE_TTL:
            return cached["result"]

    if not is_configured():
        return {
            "available": None,
            "offers_count": 0,
            "cheapest": None,
            "carriers": [],
            "error": "Amadeus API not configured",
        }

    token = _get_token()
    if not token:
        return {
            "available": None,
            "offers_count": 0,
            "cheapest": None,
            "carriers": [],
            "error": "Authentication failed",
        }

    try:
        params = urllib.parse.urlencode({
            "originLocationCode": origin.upper(),
            "destinationLocationCode": dest.upper(),
            "departureDate": date,
            "adults": 1,
            "nonStop": "false",
            "max": 10,
            "currencyCode": "USD",
        })

        url = f"{OFFERS_URL}?{params}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())

        offers = body.get("data", [])
        carriers = set()
        cheapest = None

        for offer in offers:
            price = offer.get("price", {})
            total = price.get("grandTotal") or price.get("total")
            currency = price.get("currency", "USD")

            if total:
                if cheapest is None or float(total) < float(cheapest["price"]):
                    cheapest = {"price": total, "currency": currency}

            for seg in offer.get("itineraries", [{}])[0].get("segments", []):
                carrier = seg.get("carrierCode", "")
                if carrier:
                    carriers.add(carrier)

        result = {
            "available": len(offers) > 0,
            "offers_count": len(offers),
            "cheapest": cheapest,
            "carriers": sorted(carriers),
            "error": None,
        }

        # Cache the result
        with _avail_lock:
            _avail_cache[cache_key] = {"result": result, "ts": time.time()}

        return result

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode()
        except Exception:
            pass
        logger.error(f"Amadeus API error {e.code} for {origin}->{dest}: {error_body}")
        return {
            "available": None,
            "offers_count": 0,
            "cheapest": None,
            "carriers": [],
            "error": f"API error ({e.code})",
        }
    except Exception as e:
        logger.error(f"Availability check failed for {origin}->{dest}: {e}")
        return {
            "available": None,
            "offers_count": 0,
            "cheapest": None,
            "carriers": [],
            "error": str(e),
        }


def batch_check(routes: list[dict]) -> dict:
    """
    Check availability for multiple routes.

    Args:
        routes: [{"origin": "DXB", "dest": "LHR", "date": "2026-03-04"}, ...]

    Returns:
        {"DXB-LHR-2026-03-04": {availability result}, ...}
    """
    results = {}
    for route in routes:
        key = f"{route['origin']}-{route['dest']}-{route['date']}"
        if key not in results:
            results[key] = check_availability(
                route["origin"], route["dest"], route["date"]
            )
    return results
