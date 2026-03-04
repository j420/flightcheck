"""
GCC (Gulf Cooperation Council) airport and region data for evacuation flight filtering.
"""

# GCC country codes (ISO 3166-1 alpha-2)
GCC_COUNTRIES = {"AE", "SA", "QA", "BH", "KW", "OM"}

# Major GCC airports: IATA -> details
GCC_AIRPORTS = {
    # UAE
    "DXB": {"name": "Dubai International", "city": "Dubai", "country": "AE", "icao": "OMDB"},
    "AUH": {"name": "Zayed International (Abu Dhabi)", "city": "Abu Dhabi", "country": "AE", "icao": "OMAA"},
    "SHJ": {"name": "Sharjah International", "city": "Sharjah", "country": "AE", "icao": "OMSJ"},
    "DWC": {"name": "Al Maktoum International", "city": "Dubai", "country": "AE", "icao": "OMDW"},
    "RKT": {"name": "Ras Al Khaimah International", "city": "Ras Al Khaimah", "country": "AE", "icao": "OMRK"},
    "FJR": {"name": "Fujairah International", "city": "Fujairah", "country": "AE", "icao": "OMFJ"},
    # Saudi Arabia
    "RUH": {"name": "King Khalid International", "city": "Riyadh", "country": "SA", "icao": "OERK"},
    "JED": {"name": "King Abdulaziz International", "city": "Jeddah", "country": "SA", "icao": "OEJN"},
    "DMM": {"name": "King Fahd International", "city": "Dammam", "country": "SA", "icao": "OEDF"},
    "MED": {"name": "Prince Mohammad bin Abdulaziz", "city": "Medina", "country": "SA", "icao": "OEMA"},
    "AHB": {"name": "Abha Regional", "city": "Abha", "country": "SA", "icao": "OEAB"},
    "TIF": {"name": "Taif Regional", "city": "Taif", "country": "SA", "icao": "OETF"},
    "TUU": {"name": "Tabuk Regional", "city": "Tabuk", "country": "SA", "icao": "OETB"},
    "GIZ": {"name": "Jazan Airport", "city": "Jazan", "country": "SA", "icao": "OEGN"},
    # Qatar
    "DOH": {"name": "Hamad International", "city": "Doha", "country": "QA", "icao": "OTHH"},
    # Bahrain
    "BAH": {"name": "Bahrain International", "city": "Manama", "country": "BH", "icao": "OBBI"},
    # Kuwait
    "KWI": {"name": "Kuwait International", "city": "Kuwait City", "country": "KW", "icao": "OKBK"},
    # Oman
    "MCT": {"name": "Muscat International", "city": "Muscat", "country": "OM", "icao": "OOMS"},
    "SLL": {"name": "Salalah Airport", "city": "Salalah", "country": "OM", "icao": "OOSA"},
}

# All GCC IATA codes for quick lookup
GCC_IATA_CODES = set(GCC_AIRPORTS.keys())

# All GCC ICAO codes for quick lookup
GCC_ICAO_CODES = {info["icao"] for info in GCC_AIRPORTS.values()}

# Primary departure airports (the big hubs people would evacuate from)
PRIMARY_DEPARTURE_AIRPORTS = ["DXB", "AUH", "DOH", "RUH", "JED", "KWI", "MCT", "BAH", "DMM", "SHJ"]

# Airline booking URLs - maps ICAO airline code to booking base URL
AIRLINE_BOOKING_URLS = {
    "UAE": "https://www.emirates.com/flights/book",
    "ETD": "https://www.etihad.com/en/book",
    "QTR": "https://www.qatarairways.com/en/booking.html",
    "SVA": "https://www.saudia.com/booking",
    "GFA": "https://www.gulfair.com/book",
    "KAC": "https://www.kuwaitairways.com/en/booking",
    "OMA": "https://www.omanair.com/en/book",
    "FDB": "https://www.flydubai.com/en/booking",
    "AXB": "https://www.airexplore.eu",
    "ABY": "https://www.airarabia.com/en/booking",
    "WIZ": "https://wizzair.com/en-gb/booking",
    "THY": "https://www.turkishairlines.com/en-int/flights/booking",
    "BAW": "https://www.britishairways.com/travel/book",
    "DLH": "https://www.lufthansa.com/xx/en/book-a-flight",
    "AFR": "https://www.airfrance.com/booking",
    "KLM": "https://www.klm.com/booking",
    "SIA": "https://www.singaporeair.com/en_UK/plan-and-book",
    "CPA": "https://www.cathaypacific.com/cx/en_HK/book-a-trip",
    "DAL": "https://www.delta.com/flight-search",
    "AAL": "https://www.aa.com/booking",
    "UAL": "https://www.united.com/en/us/book-flight",
    "AIC": "https://www.airindia.com/en/book.html",
    "IGO": "https://www.goindigo.in/booking",
    "PAL": "https://www.philippineairlines.com/en/booking",
    "MAS": "https://www.malaysiaairlines.com/booking",
    "PIA": "https://www.piac.com.pk/booking",
    "EGF": "https://www.egyptair.com/en/fly/book-a-flight",
    "RJA": "https://www.rj.com/en/book",
    "MEA": "https://www.mea.com.lb/english/book",
    "ETH": "https://www.ethiopianairlines.com/book",
    "KQA": "https://www.kenya-airways.com/booking",
    "SWR": "https://www.swiss.com/ch/en/book",
    "AUA": "https://www.austrian.com/at/en/book",
    "TAP": "https://www.flytap.com/en/booking",
}

# Fallback: Google Flights search URL builder
GOOGLE_FLIGHTS_BASE = "https://www.google.com/travel/flights"


def is_gcc_airport(iata_code: str) -> bool:
    """Check if an airport IATA code is within the GCC region."""
    return iata_code.upper() in GCC_IATA_CODES


def is_gcc_icao(icao_code: str) -> bool:
    """Check if an airport ICAO code is within the GCC region."""
    return icao_code.upper() in GCC_ICAO_CODES


def get_airport_info(iata_code: str) -> dict | None:
    """Get GCC airport info by IATA code."""
    return GCC_AIRPORTS.get(iata_code.upper())


def get_booking_url(airline_icao: str) -> str | None:
    """Get airline booking URL by ICAO code."""
    return AIRLINE_BOOKING_URLS.get(airline_icao.upper())
