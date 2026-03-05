"""
Vercel serverless Flask handler for the Evacuation Flight Tracker API.

CRITICAL: This API supports emergency evacuation operations.
Every endpoint includes error isolation and timestamps.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from threading import Lock

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


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------
_suggestions: list[dict] = []
_suggestions_lock = Lock()
_SUGGESTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suggestions.json")


def _load_suggestions():
    """Load suggestions from disk if the file exists."""
    try:
        if os.path.exists(_SUGGESTIONS_FILE):
            with open(_SUGGESTIONS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_suggestions(suggestions):
    """Best-effort persist suggestions to disk."""
    try:
        with open(_SUGGESTIONS_FILE, "w") as f:
            json.dump(suggestions, f)
    except Exception:
        pass


@app.route("/api/suggestions", methods=["POST"])
def api_post_suggestion():
    """Submit a user suggestion."""
    try:
        body = request.get_json(force=True) or {}
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Suggestion text is required"}), 400
        if len(text) > 500:
            return jsonify({"error": "Suggestion too long (max 500 chars)"}), 400

        suggestion = {"text": text, "timestamp": _utc_now()}
        with _suggestions_lock:
            _suggestions.append(suggestion)
            _save_suggestions(_suggestions)

        logger.info(f"New suggestion: {text[:80]}")
        return jsonify({"ok": True, "message": "Thank you for your suggestion!"})
    except Exception as e:
        logger.error(f"Suggestion error: {e}")
        return jsonify({"error": "Failed to save suggestion"}), 500


@app.route("/api/suggestions", methods=["GET"])
def api_get_suggestions():
    """Retrieve all suggestions (for admin review)."""
    with _suggestions_lock:
        return jsonify({"suggestions": list(_suggestions), "count": len(_suggestions)})


# Load existing suggestions on startup
_suggestions = _load_suggestions()


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
