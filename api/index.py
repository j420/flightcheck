"""
Vercel serverless Flask handler for the Evacuation Flight Tracker API.
"""

import logging
from flask import Flask, jsonify, request

from .gcc_data import GCC_AIRPORTS, PRIMARY_DEPARTURE_AIRPORTS
from .flight_service import FlightService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
flight_service = FlightService()


@app.route("/api/departures/<airport_iata>")
def api_departures(airport_iata: str):
    """Get filtered evacuation flights from a GCC airport."""
    airport_iata = airport_iata.upper()
    if airport_iata not in GCC_AIRPORTS:
        return jsonify({"error": f"Unknown GCC airport: {airport_iata}"}), 400

    try:
        flights = flight_service.get_departures(airport_iata)
        return jsonify({
            "airport": airport_iata,
            "airport_name": GCC_AIRPORTS[airport_iata]["name"],
            "city": GCC_AIRPORTS[airport_iata]["city"],
            "count": len(flights),
            "flights": [f.to_dict() for f in flights],
        })
    except Exception as e:
        logger.error(f"Error fetching departures for {airport_iata}: {e}")
        return jsonify({"error": str(e)}), 500


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
        output = {}
        for airport, flights in results.items():
            output[airport] = {
                "airport_name": GCC_AIRPORTS[airport]["name"],
                "city": GCC_AIRPORTS[airport]["city"],
                "count": len(flights),
                "flights": [f.to_dict() for f in flights],
            }
        return jsonify(output)
    except Exception as e:
        logger.error(f"Error scanning airports: {e}")
        return jsonify({"error": str(e)}), 500


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
