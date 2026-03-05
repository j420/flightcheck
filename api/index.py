"""
Vercel serverless Flask handler for the Evacuation Flight Tracker API.

CRITICAL: This API supports emergency evacuation operations.
Every endpoint includes error isolation and timestamps.
"""

import logging
import os
import sys
from datetime import datetime, timezone

from flask import Flask, jsonify, request

# Vercel runs this file from the project root, but our modules live in api/.
# Ensure api/ is on the import path so gcc_data, flight_service, etc. resolve.
_api_dir = os.path.dirname(os.path.abspath(__file__))
if _api_dir not in sys.path:
    sys.path.insert(0, _api_dir)

from gcc_data import GCC_AIRPORTS, GCC_IATA_CODES, PRIMARY_DEPARTURE_AIRPORTS
from flight_service import FlightService
from availability_service import is_configured as amadeus_configured, check_availability as amadeus_check, batch_check as amadeus_batch
from aviationstack_service import is_configured as avstack_configured, check_flight_status as avstack_check, batch_verify_flights as avstack_batch
from airport_scraper_service import (
    scrape_airport, scrape_all_airports, cross_reference,
    get_supported_airports as scraper_supported_airports,
    is_configured as aerodatabox_configured,
    AIRPORT_SOURCES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
flight_service = FlightService()


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.route("/api/departures/<airport_iata>")
def api_departures(airport_iata: str):
    """
    Get filtered evacuation flights from a GCC airport.

    CRITICAL: This endpoint NEVER returns 500. In an evacuation scenario,
    an error response breaks the UI and wastes people's time. We always
    return a valid JSON response — even if it contains zero flights.
    """
    airport_iata = airport_iata.upper()
    if airport_iata not in GCC_AIRPORTS:
        return jsonify({"error": f"Unknown GCC airport: {airport_iata}"}), 400

    try:
        flights = flight_service.get_departures(airport_iata)
    except Exception as e:
        logger.error(f"Error fetching departures for {airport_iata}: {e}")
        flights = []

    return jsonify({
        "airport": airport_iata,
        "airport_name": GCC_AIRPORTS[airport_iata]["name"],
        "city": GCC_AIRPORTS[airport_iata]["city"],
        "count": len(flights),
        "fetched_at": _utc_now(),
        "flights": [f.to_dict() for f in flights],
    })


@app.route("/api/scan")
def api_scan():
    """Scan selected airports for evacuation flights."""
    airports_param = request.args.get("airports", "")
    if airports_param:
        airports = [a.strip().upper() for a in airports_param.split(",")]
        airports = [a for a in airports if a in GCC_AIRPORTS]
    else:
        airports = PRIMARY_DEPARTURE_AIRPORTS

    try:
        results = flight_service.scan_all_gcc_departures(airports)
    except Exception as e:
        logger.error(f"Error scanning airports: {e}")
        results = {}

    output = {}
    for airport, flights in results.items():
        output[airport] = {
            "airport_name": GCC_AIRPORTS[airport]["name"],
            "city": GCC_AIRPORTS[airport]["city"],
            "count": len(flights),
            "flights": [f.to_dict() for f in flights],
        }
    return jsonify({"fetched_at": _utc_now(), "airports": output})


@app.route("/api/search/destination/<dest_iata>")
def api_search_destination(dest_iata: str):
    """
    Search all major GCC airports for flights to a specific destination.

    Returns flights from every GCC hub heading to the given destination.
    CRITICAL: Each airport is independent — one failure never blocks others.
    """
    dest_iata = dest_iata.upper()
    if not dest_iata or len(dest_iata) != 3:
        return jsonify({"error": "Invalid destination IATA code"}), 400
    if dest_iata in GCC_IATA_CODES:
        return jsonify({"error": "Destination must be outside GCC"}), 400

    airports_param = request.args.get("airports", "")
    if airports_param:
        airports = [a.strip().upper() for a in airports_param.split(",")]
        airports = [a for a in airports if a in GCC_AIRPORTS]
    else:
        airports = PRIMARY_DEPARTURE_AIRPORTS

    try:
        all_results = flight_service.scan_all_gcc_departures(airports)
    except Exception as e:
        logger.error(f"Error scanning for destination {dest_iata}: {e}")
        all_results = {}

    output = {}
    total_count = 0
    for airport, flights in all_results.items():
        # Filter to only flights heading to the requested destination
        matching = [f for f in flights if f.destination_iata.upper() == dest_iata]
        if matching:
            output[airport] = {
                "airport_name": GCC_AIRPORTS[airport]["name"],
                "city": GCC_AIRPORTS[airport]["city"],
                "count": len(matching),
                "flights": [f.to_dict() for f in matching],
            }
            total_count += len(matching)

    return jsonify({
        "destination": dest_iata,
        "total_flights": total_count,
        "fetched_at": _utc_now(),
        "airports": output,
    })


@app.route("/api/availability/<origin>/<dest>/<date>")
def api_availability(origin: str, dest: str, date: str):
    """Check seat availability for a specific route and date (Amadeus only)."""
    if not amadeus_configured():
        return jsonify({"error": "Availability checking not configured", "configured": False}), 503

    try:
        result = amadeus_check(origin.upper(), dest.upper(), date)
        return jsonify({**result, "route": f"{origin.upper()}-{dest.upper()}", "date": date})
    except Exception as e:
        logger.error(f"Availability check error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/availability/batch", methods=["POST"])
def api_availability_batch():
    """
    Batch check availability for multiple routes (Amadeus only).
    Body: {"routes": [{"origin":"DXB","dest":"LHR","date":"2026-03-04"}, ...]}
    """
    if not amadeus_configured():
        return jsonify({"error": "Availability checking not configured", "configured": False}), 503

    try:
        body = request.get_json(force=True) or {}
        routes = body.get("routes", [])
        if not routes:
            return jsonify({"error": "No routes provided"}), 400
        if len(routes) > 30:
            return jsonify({"error": "Max 30 routes per batch"}), 400

        results = amadeus_batch(routes)
        return jsonify({"results": results, "fetched_at": _utc_now()})
    except Exception as e:
        logger.error(f"Batch availability error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/availability/status")
def api_availability_status():
    """Check if Amadeus availability checking is configured."""
    return jsonify({"configured": amadeus_configured()})


@app.route("/api/verify/flight/<flight_number>")
def api_verify_flight(flight_number: str):
    """Verify a single flight's status via AviationStack (cancellation check)."""
    if not avstack_configured():
        return jsonify({"error": "AviationStack not configured", "configured": False}), 503
    try:
        result = avstack_check(flight_number)
        return jsonify({**result, "flight_number": flight_number.upper()})
    except Exception as e:
        logger.error(f"Flight verification error: {e}")
        return jsonify({"status": "unknown", "is_cancelled": False, "source": "error"}), 500


@app.route("/api/verify/batch", methods=["POST"])
def api_verify_batch():
    """
    Batch verify flight statuses via AviationStack.
    Body: {"flight_numbers": ["SQ495", "EK202", ...]}
    Max 10 per batch (free tier rate limits).
    """
    if not avstack_configured():
        return jsonify({"error": "AviationStack not configured", "configured": False}), 503
    try:
        body = request.get_json(force=True) or {}
        flight_numbers = body.get("flight_numbers", [])
        if not flight_numbers:
            return jsonify({"error": "No flight numbers provided"}), 400
        if len(flight_numbers) > 10:
            return jsonify({"error": "Max 10 flights per batch (free tier limit)"}), 400
        results = avstack_batch(flight_numbers)
        return jsonify({"results": results, "fetched_at": _utc_now()})
    except Exception as e:
        logger.error(f"Batch verify error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/verify/status")
def api_verify_status():
    """Check if AviationStack verification is configured."""
    return jsonify({"configured": avstack_configured()})


@app.route("/api/airport-status/<airport_iata>")
def api_airport_status(airport_iata: str):
    """
    Scrape departure data directly from an airport's official website.

    This provides a secondary data source to cross-verify FR24 data.
    CRITICAL: Never returns 500 — returns empty results on failure.
    """
    airport_iata = airport_iata.upper()
    try:
        result = scrape_airport(airport_iata)
    except Exception as e:
        logger.error(f"Airport scraper error for {airport_iata}: {e}")
        result = {
            "airport": airport_iata,
            "airport_name": AIRPORT_SOURCES.get(airport_iata, {}).get("name", "Unknown"),
            "source_url": AIRPORT_SOURCES.get(airport_iata, {}).get("url", ""),
            "flights": [],
            "flight_count": 0,
            "scraped_at": _utc_now(),
            "from_cache": False,
            "error": str(e),
        }
    return jsonify(result)


@app.route("/api/airport-status/scan")
def api_airport_status_scan():
    """
    Scrape departure data from all supported airport websites.
    Each airport is independent — one failure never blocks others.
    """
    airports_param = request.args.get("airports", "")
    if airports_param:
        airports = [a.strip().upper() for a in airports_param.split(",")]
    else:
        airports = None  # defaults to all supported airports

    try:
        results = scrape_all_airports(airports)
    except Exception as e:
        logger.error(f"Airport scraper scan error: {e}")
        results = {}

    return jsonify({"scraped_at": _utc_now(), "airports": results})


@app.route("/api/airport-status/cross-reference/<airport_iata>")
def api_cross_reference(airport_iata: str):
    """
    Cross-reference FR24 data with airport website data for a specific airport.

    Returns discrepancies — e.g., flights FR24 shows as "scheduled" but the
    airport website shows as "cancelled". These are the dangerous false positives.
    """
    airport_iata = airport_iata.upper()

    # Get FR24 flights
    try:
        fr24_flights = flight_service.get_departures(airport_iata)
        fr24_dicts = [f.to_dict() for f in fr24_flights]
    except Exception as e:
        logger.error(f"FR24 error during cross-ref for {airport_iata}: {e}")
        fr24_dicts = []

    # Get scraped flights
    try:
        scrape_result = scrape_airport(airport_iata)
        scraped_dicts = scrape_result.get("flights", [])
    except Exception as e:
        logger.error(f"Scraper error during cross-ref for {airport_iata}: {e}")
        scraped_dicts = []

    # Cross-reference
    discrepancies = cross_reference(fr24_dicts, scraped_dicts)

    return jsonify({
        "airport": airport_iata,
        "fr24_count": len(fr24_dicts),
        "scraped_count": len(scraped_dicts),
        "discrepancies": discrepancies,
        "discrepancy_count": len(discrepancies),
        "checked_at": _utc_now(),
    })


@app.route("/api/airport-status/supported")
def api_airport_status_supported():
    """List airports with AeroDataBox FIDS support."""
    supported = {}
    for iata, info in AIRPORT_SOURCES.items():
        supported[iata] = {
            "name": info["name"],
            "url": info["url"],
        }
    return jsonify({"supported_airports": supported, "configured": aerodatabox_configured()})


@app.route("/api/airport-status/config")
def api_airport_status_config():
    """Check if AeroDataBox airport status checking is configured."""
    return jsonify({"configured": aerodatabox_configured()})


@app.route("/api/airport-status/debug/<airport_iata>")
def api_airport_status_debug(airport_iata):
    """Debug endpoint: test AeroDataBox API for a single airport with full error details."""
    import urllib.request
    import urllib.error
    from datetime import datetime, timezone, timedelta

    airport_iata = airport_iata.upper()
    api_key = os.environ.get("AERODATABOX_API_KEY", "")
    key_preview = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("SET" if api_key else "NOT SET")

    _offsets = {"DXB": 4, "AUH": 4, "SHJ": 4, "DOH": 3, "RUH": 3, "JED": 3, "DMM": 3, "KWI": 3, "BAH": 3, "MCT": 4, "SLL": 4}
    utc_offset = _offsets.get(airport_iata, 4)
    local_tz = timezone(timedelta(hours=utc_offset))
    now_local = datetime.now(local_tz)
    from_time = now_local - timedelta(hours=2)
    to_time = now_local + timedelta(hours=12)
    from_str = from_time.strftime("%Y-%m-%dT%H:%M")
    to_str = to_time.strftime("%Y-%m-%dT%H:%M")

    url = (
        f"https://aerodatabox.p.rapidapi.com/flights/airports/iata/{airport_iata}"
        f"/{from_str}/{to_str}"
        f"?direction=Departure&withCancelled=true&withCodeshared=false&withLocation=false"
    )

    debug_info = {
        "airport": airport_iata,
        "api_key_status": key_preview,
        "utc_now": datetime.now(timezone.utc).isoformat(),
        "local_now": now_local.isoformat(),
        "utc_offset": f"+{utc_offset}",
        "request_url": url,
    }

    if not api_key:
        debug_info["error"] = "AERODATABOX_API_KEY not set"
        return jsonify(debug_info), 200

    try:
        req = urllib.request.Request(url)
        req.add_header("X-RapidAPI-Host", "aerodatabox.p.rapidapi.com")
        req.add_header("X-RapidAPI-Key", api_key)
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            departures = data.get("departures", [])
            debug_info["status_code"] = resp.status
            debug_info["response_keys"] = list(data.keys())
            debug_info["departure_count"] = len(departures)
            if departures:
                debug_info["first_flight"] = departures[0]
            else:
                debug_info["raw_response_preview"] = raw[:1000]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        debug_info["error"] = f"HTTP {e.code}"
        debug_info["error_body"] = body
    except Exception as e:
        debug_info["error"] = str(e)

    return jsonify(debug_info), 200


@app.route("/api/airports")
def api_airports():
    """List all available GCC airports."""
    return jsonify({
        "airports": {
            iata: {
                "name": info["name"],
                "city": info["city"],
                "country": info["country"],
            }
            for iata, info in GCC_AIRPORTS.items()
        },
        "primary": PRIMARY_DEPARTURE_AIRPORTS,
    })
