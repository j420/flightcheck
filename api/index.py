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

from gcc_data import GCC_AIRPORTS, PRIMARY_DEPARTURE_AIRPORTS
from flight_service import FlightService
from availability_service import is_configured as amadeus_configured, check_availability, batch_check

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


@app.route("/api/availability/<origin>/<dest>/<date>")
def api_availability(origin: str, dest: str, date: str):
    """Check seat availability for a specific route and date."""
    if not amadeus_configured():
        return jsonify({"error": "Availability checking not configured", "configured": False}), 503

    try:
        result = check_availability(origin.upper(), dest.upper(), date)
        return jsonify({**result, "route": f"{origin.upper()}-{dest.upper()}", "date": date})
    except Exception as e:
        logger.error(f"Availability check error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/availability/batch", methods=["POST"])
def api_availability_batch():
    """
    Batch check availability for multiple routes.
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

        results = batch_check(routes)
        return jsonify({"results": results, "fetched_at": _utc_now()})
    except Exception as e:
        logger.error(f"Batch availability error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/availability/status")
def api_availability_status():
    """Check if Amadeus availability checking is configured."""
    return jsonify({"configured": amadeus_configured()})


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
