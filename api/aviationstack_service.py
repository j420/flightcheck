"""
AviationStack fallback service for verifying flight cancellations.

CRITICAL: This is a secondary verification layer. FR24 is the primary data source,
but it misses cancellations (returns cancelled flights as "Scheduled").
AviationStack catches these cancellations before we show bad data to evacuees.

Free tier: 100 requests/month. We check sparingly (every 15 minutes)
and only for flights that FR24 reports as "scheduled".
"""

import logging
import os
import time
from threading import Lock

logger = logging.getLogger(__name__)

# AviationStack API config
AVIATIONSTACK_API_KEY = os.environ.get("AVIATIONSTACK_API_KEY", "")
AVIATIONSTACK_BASE_URL = "http://api.aviationstack.com/v1"

# Cache: flight_number -> {"status": str, "timestamp": float}
_status_cache: dict[str, dict] = {}
_cache_lock = Lock()
CACHE_TTL_SECONDS = 900  # 15 minutes — matches our check interval


def is_configured() -> bool:
    """Check if AviationStack API key is set."""
    return bool(AVIATIONSTACK_API_KEY)


def _get_cached_status(flight_number: str) -> dict | None:
    """Return cached status if fresh enough."""
    with _cache_lock:
        cached = _status_cache.get(flight_number)
    if cached and (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS:
        return cached
    return None


def _set_cached_status(flight_number: str, status: str, is_cancelled: bool):
    """Cache a flight status."""
    with _cache_lock:
        _status_cache[flight_number] = {
            "status": status,
            "is_cancelled": is_cancelled,
            "timestamp": time.time(),
        }


def check_flight_status(flight_number: str) -> dict:
    """
    Check a single flight's status via AviationStack.

    Returns: {"status": str, "is_cancelled": bool, "source": "aviationstack"}
    On failure, returns {"status": "unknown", "is_cancelled": False} —
    we never block flights based on a failed check.
    """
    if not AVIATIONSTACK_API_KEY:
        return {"status": "unknown", "is_cancelled": False, "source": "not_configured"}

    # Check cache first
    cached = _get_cached_status(flight_number)
    if cached:
        return {
            "status": cached["status"],
            "is_cancelled": cached["is_cancelled"],
            "source": "aviationstack_cache",
        }

    try:
        import requests

        # Strip whitespace, normalize
        fn = flight_number.strip().upper()

        resp = requests.get(
            f"{AVIATIONSTACK_BASE_URL}/flights",
            params={
                "access_key": AVIATIONSTACK_API_KEY,
                "flight_iata": fn,
                "limit": 1,
            },
            timeout=5,
        )

        if resp.status_code != 200:
            logger.warning(f"AviationStack returned {resp.status_code} for {fn}")
            return {"status": "unknown", "is_cancelled": False, "source": "aviationstack_error"}

        data = resp.json()
        flights = data.get("data") or []

        if not flights:
            # No data — don't mark as cancelled, could just be missing
            return {"status": "unknown", "is_cancelled": False, "source": "aviationstack_no_data"}

        flight = flights[0]
        status = (flight.get("flight_status") or "").lower()
        is_cancelled = status in ("cancelled", "canceled")

        _set_cached_status(fn, status, is_cancelled)

        return {
            "status": status,
            "is_cancelled": is_cancelled,
            "source": "aviationstack",
        }

    except Exception as e:
        logger.error(f"AviationStack check failed for {flight_number}: {e}")
        return {"status": "unknown", "is_cancelled": False, "source": "aviationstack_error"}


def batch_verify_flights(flight_numbers: list[str]) -> dict[str, dict]:
    """
    Verify multiple flights. Returns dict of flight_number -> status info.

    AviationStack free tier only supports single-flight lookups,
    so we loop through them but respect rate limits.
    """
    results = {}
    for fn in flight_numbers:
        results[fn] = check_flight_status(fn)
        # Small delay to avoid hitting rate limits
        time.sleep(0.2)
    return results
