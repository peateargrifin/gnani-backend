import csv
import heapq
import io
import json
import math
import os
import re
import sqlite3
import time
from datetime import date, datetime, timezone

import httpx

LANGUAGES = {"en-IN": "English", "hi-IN": "हिन्दी", "kn-IN": "ಕನ್ನಡ", "ta-IN": "தமிழ்", "te-IN": "తెలుగు", "mr-IN": "मराठी", "bn-IN": "বাংলা", "gu-IN": "ગુજરાતી", "pa-IN": "ਪੰਜਾਬੀ", "ml-IN": "മലയാളം"}

# id: (English, Hindi, aliases). Aliases cover AGMARKNET commodity names, native scripts
# and romanised Hinglish/Tanglish. Order matters where one alias contains another
# (groundnut before moong, cabbage before cauliflower): the first match wins.
CROP_TABLE = {
    "potato": ("Potato", "आलू", ["potato", "आलू", "आलु", "ಆಲೂಗಡ್ಡೆ", "உருளைக்கிழங்கு", "బంగాళాదుంప", "aloo", "alu", "batata", "बटाटा", "urulaikizhangu", "aloogadde"]),
    "onion": ("Onion", "प्याज़", ["onion", "प्याज", "प्याज़", "ಈರುಳ್ಳಿ", "வெங்காயம்", "ఉల్లిపాయ", "pyaz", "pyaaz", "piyaz", "kanda", "कांदा", "eerulli", "vengayam"]),
    "tomato": ("Tomato", "टमाटर", ["tomato", "टमाटर", "ಟೊಮೆಟೊ", "தக்காளி", "టమాటా", "tamatar", "tamaatar", "thakkali", "tamata"]),
    "sugarcane": ("Sugarcane", "गन्ना", ["sugarcane", "गन्ना", "गन्ने", "ಕಬ್ಬು", "கரும்பு", "చెరకు", "ganna", "ganne", "oos", "ऊस", "kabbu", "karumbu"]),
    "wheat": ("Wheat", "गेहूं", ["wheat", "गेहूं", "गेहूँ", "गेंहू", "ಗೋಧಿ", "கோதுமை", "గోధుమ", "gehu", "gehun", "gehoon", "genhu"]),
    "paddy": ("Paddy", "धान", ["paddy", "dhan", "धान", "ಭತ್ತ", "நெல்", "వరి"]),
    "rice": ("Rice", "चावल", ["rice", "chawal", "chaawal", "चावल", "ಅಕ್ಕಿ", "அரிசி", "బియ్యం"]),
    "maize": ("Maize", "मक्का", ["maize", "corn", "makka", "makai", "मक्का", "मकई", "ಮೆಕ್ಕೆಜೋಳ", "மக்காச்சோளம்"]),
    "bajra": ("Bajra", "बाजरा", ["bajra", "pearl millet", "cumbu", "बाजरा", "ಸಜ್ಜೆ", "கம்பு"]),
    "jowar": ("Jowar", "ज्वार", ["jowar", "sorghum", "ज्वार", "ಜೋಳ", "சோளம்"]),
    "ragi": ("Ragi", "रागी", ["ragi", "finger millet", "nachni", "रागी", "ನಾಚನಿ", "ರಾಗಿ", "கேழ்வரகு"]),
    "groundnut": ("Groundnut", "मूंगफली", ["groundnut", "peanut", "moongfali", "mungfali", "मूंगफली", "मूँगफली", "ಕಡಲೆಕಾಯಿ", "நிலக்கடலை"]),
    "moong": ("Moong", "मूंग", ["green gram", "moong", "mung", "मूंग", "मूँग", "ಹೆಸರು", "பாசிப்பயறு"]),
    "urad": ("Urad", "उड़द", ["black gram", "urd", "urad", "उड़द", "उड़द", "उडद", "ಉದ್ದು", "உளுந்து"]),
    "tur": ("Tur / Arhar", "अरहर", ["arhar", "tur", "toor", "red gram", "अरहर", "तुअर", "तूर", "ತೊಗರಿ", "துவரை"]),
    "chana": ("Chana", "चना", ["bengal gram", "chana", "chickpea", "चना", "ಕಡಲೆ", "கொண்டைக்கடலை"]),
    "soyabean": ("Soyabean", "सोयाबीन", ["soyabean", "soybean", "soya", "सोयाबीन", "ಸೋಯಾಬೀನ್"]),
    "mustard": ("Mustard", "सरसों", ["mustard", "sarson", "sarso", "सरसों", "सरसो", "ಸಾಸಿವೆ", "கடுகு"]),
    "cotton": ("Cotton", "कपास", ["cotton", "kapas", "कपास", "ಹತ್ತಿ", "பருத்தி", "పత్తి"]),
    "garlic": ("Garlic", "लहसुन", ["garlic", "lahsun", "lehsun", "लहसुन", "ಬೆಳ್ಳುಳ್ಳಿ", "பூண்டு"]),
    "ginger": ("Ginger", "अदरक", ["ginger", "adrak", "अदरक", "ಶುಂಠಿ", "இஞ்சி"]),
    "green_chilli": ("Green Chilli", "हरी मिर्च", ["green chilli", "hari mirch", "mirchi", "मिर्च", "मिर्ची", "ಮೆಣಸಿನಕಾಯಿ", "மிளகாய்"]),
    "cabbage": ("Cabbage", "पत्ता गोभी", ["cabbage", "patta gobhi", "band gobhi", "पत्ता गोभी", "पत्तागोभी", "बंद गोभी", "ಎಲೆಕೋಸು", "முட்டைக்கோஸ்"]),
    "cauliflower": ("Cauliflower", "फूल गोभी", ["cauliflower", "gobhi", "gobi", "phool gobhi", "गोभी", "ಹೂಕೋಸು", "காலிஃப்ளவர்"]),
    "brinjal": ("Brinjal", "बैंगन", ["brinjal", "eggplant", "baingan", "बैंगन", "ಬದನೆ", "கத்தரிக்காய்"]),
    "bhindi": ("Bhindi / Okra", "भिंडी", ["bhindi", "ladies finger", "okra", "भिंडी", "ಬೆಂಡೆ", "வெண்டைக்காய்"]),
    "peas": ("Green Peas", "मटर", ["peas", "matar", "mattar", "मटर", "ಬಟಾಣಿ", "பட்டாணி"]),
    "carrot": ("Carrot", "गाजर", ["carrot", "gajar", "गाजर", "ಕ್ಯಾರೆಟ್", "கேரட்"]),
    "radish": ("Radish", "मूली", ["raddish", "radish", "mooli", "muli", "मूली", "ಮೂಲಂಗಿ", "முள்ளங்கி"]),
    "cucumber": ("Cucumber", "खीरा", ["cucumbar", "cucumber", "kheera", "khira", "खीरा", "ಸೌತೆಕಾಯಿ", "வெள்ளரிக்காய்"]),
    "bottle_gourd": ("Bottle Gourd", "लौकी", ["bottle gourd", "lauki", "ghiya", "लौकी", "ಸೋರೆಕಾಯಿ", "சுரைக்காய்"]),
    "bitter_gourd": ("Bitter Gourd", "करेला", ["bitter gourd", "karela", "करेला", "ಹಾಗಲಕಾಯಿ", "பாகற்காய்"]),
    "pumpkin": ("Pumpkin", "कद्दू", ["pumpkin", "kaddu", "कद्दू", "ಕುಂಬಳಕಾಯಿ", "பூசணி"]),
    "spinach": ("Spinach", "पालक", ["spinach", "palak", "पालक", "ಪಾಲಕ್", "பசலைக்கீரை"]),
    "coriander": ("Coriander", "धनिया", ["coriander", "dhaniya", "dhania", "धनिया", "ಕೊತ್ತಂಬರಿ", "கொத்தமல்லி"]),
    "lemon": ("Lemon", "नींबू", ["lemon", "lime", "nimbu", "neembu", "नींबू", "निम्बू", "ನಿಂಬೆ", "எலுமிச்சை"]),
    "turmeric": ("Turmeric", "हल्दी", ["turmeric", "haldi", "हल्दी", "ಅರಿಶಿನ", "மஞ்சள்"]),
    "cumin": ("Cumin / Jeera", "जीरा", ["cummin", "cumin", "jeera", "jira", "जीरा", "ಜೀರಿಗೆ", "சீரகம்"]),
    "banana": ("Banana", "केला", ["banana", "kela", "केला", "केले", "ಬಾಳೆ", "வாழை"]),
    "custard_apple": ("Custard Apple", "सीताफल", ["custard apple", "sitaphal", "sharifa", "सीताफल", "शरीफा"]),
    "apple": ("Apple", "सेब", ["apple", "seb", "saib", "सेब", "ಸೇಬು", "ஆப்பிள்"]),
    "pomegranate": ("Pomegranate", "अनार", ["pomegranate", "anar", "anaar", "अनार", "ದಾಳಿಂಬೆ", "மாதுளை"]),
    "grapes": ("Grapes", "अंगूर", ["grapes", "grape", "angoor", "angur", "अंगूर", "ದ್ರಾಕ್ಷಿ", "திராட்சை"]),
    "papaya": ("Papaya", "पपीता", ["papaya", "papita", "पपीता", "ಪಪ್ಪಾಯಿ", "பப்பாளி"]),
    "guava": ("Guava", "अमरूद", ["guava", "amrud", "amrood", "अमरूद", "ಸೀಬೆ", "கொய்யா"]),
    "orange": ("Orange", "संतरा", ["orange", "santra", "santara", "संतरा", "ಕಿತ್ತಳೆ", "ஆரஞ்சு"]),
    "watermelon": ("Watermelon", "तरबूज", ["water melon", "watermelon", "tarbooz", "tarbuj", "तरबूज", "ಕಲ್ಲಂಗಡಿ", "தர்பூசணி"]),
    "coconut": ("Coconut", "नारियल", ["coconut", "nariyal", "नारियल", "ತೆಂಗಿನಕಾಯಿ", "தேங்காய்"]),
    "mango": ("Mango", "आम", ["mango", "aam", "आम", "ಮಾವು", "மாம்பழம்"]),
}
CROPS = {crop: aliases for crop, (_, _, aliases) in CROP_TABLE.items()}
# How each crop is named inside the spoken reply.
CROP_LABELS = {
    "en-IN": {crop: en.lower() for crop, (en, _, _) in CROP_TABLE.items()},
    "hi-IN": {crop: hi for crop, (_, hi, _) in CROP_TABLE.items()},
    "kn-IN": {"potato": "ಆಲೂಗಡ್ಡೆ", "sugarcane": "ಕಬ್ಬು", "onion": "ಈರುಳ್ಳಿ", "tomato": "ಟೊಮೆಟೊ", "ragi": "ರಾಗಿ", "paddy": "ಭತ್ತ", "rice": "ಅಕ್ಕಿ", "banana": "ಬಾಳೆ", "coconut": "ತೆಂಗಿನಕಾಯಿ"},
    "ta-IN": {"potato": "உருளைக்கிழங்கு", "sugarcane": "கரும்பு", "onion": "வெங்காயம்", "tomato": "தக்காளி", "paddy": "நெல்", "rice": "அரிசி", "banana": "வாழை", "coconut": "தேங்காய்"},
}
MESSAGES = {
    "en-IN": "Best estimated net return for {qty} kg of {crop}: {market}, ₹{net}. Quote dated {date}. Confirm the buyer and freight before travel.",
    "hi-IN": "{qty} किलो {crop} के लिए अनुमानित सबसे अच्छी शुद्ध रकम {market} में ₹{net} है। भाव {date} का है। जाने से पहले खरीदार और भाड़े की पुष्टि करें।",
    "kn-IN": "{qty} ಕೆಜಿ {crop} ಗೆ ಅಂದಾಜು ಉತ್ತಮ ನಿವ್ವಳ ಮೊತ್ತ {market} ನಲ್ಲಿ ₹{net}. ದರದ ದಿನಾಂಕ {date}. ಹೊರಡುವ ಮೊದಲು ಖರೀದಿದಾರ ಮತ್ತು ಸಾಗಣೆ ವೆಚ್ಚ ಖಚಿತಪಡಿಸಿ.",
    "ta-IN": "{qty} கிலோ {crop} க்கான அதிக மதிப்பிடப்பட்ட நிகர வரவு {market} சந்தையில் ₹{net}. விலை தேதி {date}. பயணத்திற்கு முன் வாங்குபவர் மற்றும் போக்குவரத்து கட்டணத்தை உறுதிசெய்யவும்.",
}

def now():
    return datetime.now(timezone.utc).isoformat()

_ready = set()  # database files whose tables exist in this process

def db_path():
    # Serverless hosts (Vercel) only allow writing to /tmp.
    return os.getenv("DATABASE_PATH") or ("/tmp/mandi.db" if os.getenv("VERCEL") else "./mandi.db")

def connect():
    path = db_path()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if path not in _ready:  # create tables on first use: serverless may skip startup hooks
        _ready.add(path)
        _create_tables(db)
    return db

def init_db():
    with connect() as db:
        _create_tables(db)

def _create_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS geocodes (query TEXT PRIMARY KEY, latitude REAL, longitude REAL, label TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS markets (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL,
            district TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            UNIQUE(name,state,district));
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY, market_id INTEGER NOT NULL REFERENCES markets(id),
            commodity TEXT NOT NULL, variety TEXT NOT NULL DEFAULT '',
            min_price REAL, max_price REAL, modal_price REAL NOT NULL,
            observed_on TEXT NOT NULL, source TEXT NOT NULL, imported_at TEXT NOT NULL,
            UNIQUE(market_id,commodity,variety,observed_on,source));
        CREATE INDEX IF NOT EXISTS quotes_lookup ON quotes(commodity,observed_on);
        CREATE TABLE IF NOT EXISTS dispatches (
            id TEXT PRIMARY KEY, market_id INTEGER NOT NULL REFERENCES markets(id),
            commodity TEXT NOT NULL, quantity_kg REAL NOT NULL, contact TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL);
        """)

def _alias_hit(alias, text):
    # Latin aliases must match whole words ("alu" must not match "value").
    if alias.isascii():
        return re.search(rf"\b{re.escape(alias)}\b", text) is not None
    return alias in text

# Processed products and look-alikes that must not be priced as the raw crop.
NOT_RAW_CROP = re.compile(r"sweet potato|\batta\b|\boil\b|coir|cotton seed|bags|dried|powder|dry grapes|banana stem|\bdal\b|"
                          r"broken rice|beaten rice|baby corn|sweet corn|liquor|spring onion|onion green|wild cucumber|rat tail|"
                          r"sweet lime|mousambi|galgal|mustard leaf|tender coconut|coconut seed|sweet pumpkin|white pumpkin|white peas")

def crop_name(text):
    value = text.casefold().strip()
    if NOT_RAW_CROP.search(value):
        return value
    for crop, aliases in CROPS.items():
        if any(_alias_hit(a.casefold(), value) for a in aliases):
            return crop
    return value

UNITS = r"tonnes?|tons?|टन|quintals?|qtls?|kwintal|क्विंटल|कुंतल|कुन्तल|ಕ್ವಿಂಟಾಲ್|குவிண்டால்|క్వింటాల్|kgs?|kilos?|किलो|ಕೆಜಿ|ಕಿಲೋ|கிலோ|కిలో|కేజీ"

def parse_intent(text):
    lowered = text.casefold()
    crop = next((c for c, aliases in CROPS.items() if any(_alias_hit(a.casefold(), lowered) for a in aliases)), None)
    normalized = text.translate(str.maketrans("०१२३४५६७८९", "0123456789")).casefold()
    # Accepts 500, 2.5 and Indian grouping such as 1,00,000.
    match = re.search(rf"(\d{{1,3}}(?:,\d{{2,3}})+|\d+(?:\.\d+)?)\s*({UNITS})?", normalized)
    qty = None
    if match:
        qty = float(match.group(1).replace(",", ""))
        unit = match.group(2) or "kg"
        if unit.startswith(("ton", "टन")):
            qty *= 1000
        elif unit.startswith(("quint", "qtl", "kwin", "क्विं", "कुं", "कुन", "ಕ್ವಿ", "குவி", "క్విం")):
            qty *= 100
    return {"commodity": crop, "quantity_kg": qty, "needs_clarification": not (crop and qty)}

def distance_km(a, b, c, d):
    radius = 6371.0088
    lat1, lat2 = math.radians(a), math.radians(c)
    delta_lat, delta_lon = lat2 - lat1, math.radians(d-b)
    x = math.sin(delta_lat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(delta_lon/2)**2
    return 2*radius*math.asin(math.sqrt(x))

_route_cache = {}  # roads don't change: cache OSRM answers so auto-refresh stays cheap

def _cached(key):
    hit = _route_cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < 6 * 3600 else None

def _remember(key, value):
    if len(_route_cache) > 500:
        _route_cache.clear()
    _route_cache[key] = (time.monotonic(), value)
    return value

TOMTOM = "https://api.tomtom.com"

def tomtom_key():
    return os.getenv("TOMTOM_API_KEY") or None

async def road_data(client, points):
    """Distances (km), travel times (minutes) and traffic delay for every pair of points.
    OSRM gives the full road matrix; with TOMTOM_API_KEY the farm-to-mandi legs
    (row and column 0) are replaced by TomTom live-traffic values.
    Returns {"km", "min", "delay_min", "method", "traffic"}."""
    key = ("data", tuple((round(a, 4), round(b, 4)) for a, b in points))
    if (hit := _cached(key)) is not None and not tomtom_key():
        return hit
    km, minutes, method = await _osrm_table(client, points)
    delay = [[0.0] * len(points) for _ in points]
    traffic = False
    if tomtom_key() and len(points) > 1:
        live = await _tomtom_row(client, points)
        if live:
            traffic = True
            for j, (k, m, d) in live.items():
                km[0][j] = km[j][0] = k
                minutes[0][j] = minutes[j][0] = m
                delay[0][j] = delay[j][0] = d
    result = {"km": km, "min": minutes, "delay_min": delay, "method": method, "traffic": traffic}
    if method == "road_route" and not traffic:
        _remember(key, result)
    return result

async def _osrm_table(client, points):
    straight = [[distance_km(*a, *b) for b in points] for a in points]
    est_min = [[d / 40 * 60 for d in row] for row in straight]  # ~40 km/h when no router answers
    base = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        resp = await client.get(f"{base}/table/v1/driving/{coords}", params={"annotations": "distance,duration"}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        km = [[m/1000 if m is not None else straight[i][j] for j, m in enumerate(row)] for i, row in enumerate(data["distances"])]
        mins = [[s/60 if s is not None else est_min[i][j] for j, s in enumerate(row)] for i, row in enumerate(data["durations"])]
        return km, mins, "road_route"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return straight, est_min, "straight_line_estimate"

async def _tomtom_row(client, points):
    """Farm -> each mandi with live traffic (TomTom Matrix Routing v2, synchronous)."""
    body = {"origins": [{"point": {"latitude": points[0][0], "longitude": points[0][1]}}],
            "destinations": [{"point": {"latitude": a, "longitude": b}} for a, b in points[1:]],
            "options": {"departAt": "now", "routeType": "fastest", "traffic": "live", "travelMode": "truck"}}
    try:
        resp = await client.post(f"{TOMTOM}/routing/matrix/2", params={"key": tomtom_key()}, json=body, timeout=15)
        resp.raise_for_status()
        out = {}
        for cell in resp.json().get("data", []):
            s = cell.get("routeSummary")
            if s:
                out[cell["destinationIndex"] + 1] = (s["lengthInMeters"] / 1000, s["travelTimeInSeconds"] / 60, s.get("trafficDelayInSeconds", 0) / 60)
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None

async def traffic_route(client, waypoints):
    """Road polyline and live traffic delay through waypoints (TomTom), or None."""
    if not tomtom_key():
        return None
    loc = ":".join(f"{lat},{lon}" for lat, lon in waypoints)
    try:
        resp = await client.get(f"{TOMTOM}/routing/1/calculateRoute/{loc}/json",
                                params={"key": tomtom_key(), "traffic": "true", "travelMode": "truck", "routeType": "fastest"}, timeout=10)
        resp.raise_for_status()
        route = resp.json()["routes"][0]
        line = [[p["latitude"], p["longitude"]] for leg in route["legs"] for p in leg["points"]]
        s = route["summary"]
        return {"line": line[::max(1, len(line) // 400)], "minutes": s["travelTimeInSeconds"] / 60,
                "traffic_delay_min": s.get("trafficDelayInSeconds", 0) / 60, "km": s["lengthInMeters"] / 1000}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return None

async def road_matrix(client, points):
    """All-pairs road distance (km) between points via one OSRM table call.
    Falls back to straight-line distance for any pair OSRM cannot answer."""
    key = ("matrix", tuple((round(a, 4), round(b, 4)) for a, b in points))
    if (hit := _cached(key)) is not None:
        return hit
    result = await _road_matrix(client, points)
    return _remember(key, result) if result[1] == "road_route" else result

async def _road_matrix(client, points):
    straight = [[distance_km(*a, *b) for b in points] for a in points]
    base = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        resp = await client.get(f"{base}/table/v1/driving/{coords}", params={"annotations": "distance"}, timeout=8)
        resp.raise_for_status()
        meters = resp.json()["distances"]
        matrix = [[m/1000 if m is not None else straight[i][j] for j, m in enumerate(row)] for i, row in enumerate(meters)]
        return matrix, "road_route"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return straight, "straight_line_estimate"

def build_graph(matrix, neighbours=4):
    """Sparse road network: each place links to its nearest `neighbours` places
    (both directions), so trucks can route through intermediate mandi towns."""
    n = len(matrix)
    adj = {i: {} for i in range(n)}
    def link(i, j):
        w = min(matrix[i][j], matrix[j][i])
        adj[i][j] = adj[j][i] = w
    for i in range(n):
        for j in sorted((j for j in range(n) if j != i), key=lambda j: matrix[i][j])[:neighbours]:
            link(i, j)
    # Make sure every place is reachable from the farm (node 0).
    while True:
        dist, _ = dijkstra(adj, 0)
        missing = [j for j in range(n) if j not in dist]
        if not missing:
            return adj
        i, j = min(((i, j) for i in dist for j in missing), key=lambda p: matrix[p[0]][p[1]])
        link(i, j)

def dijkstra(adj, source):
    """Classic Dijkstra with a binary heap. Returns (distance, previous-node) maps."""
    dist, prev, heap = {source: 0.0}, {}, [(0.0, source)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    return dist, prev

def path_to(prev, target):
    path = [target]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    return path[::-1]

async def route_geometry(client, waypoints):
    """Road polyline [[lat, lon], ...] through the given waypoints, or None."""
    key = ("route", tuple((round(a, 4), round(b, 4)) for a, b in waypoints))
    if (hit := _cached(key)) is not None:
        return hit
    base = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
    coords = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    try:
        resp = await client.get(f"{base}/route/v1/driving/{coords}", params={"overview": "simplified", "geometries": "geojson"}, timeout=6)
        resp.raise_for_status()
        return _remember(key, [[lat, lon] for lon, lat in resp.json()["routes"][0]["geometry"]["coordinates"]])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return None

def read_rows(content, filename):
    if filename.lower().endswith(".json"):
        data = json.loads(content)
        return data["records"] if isinstance(data, dict) else data
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

def ingest(records, source):
    accepted, skipped = 0, 0
    with connect() as db:
        for row in records:
            try:
                market, state, district = (str(row[k]).strip() for k in ("market", "state", "district"))
                crop = crop_name(str(row["commodity"]))
                price = float(row.get("modal_price") or row.get("modal_price_rs_per_quintal"))
                day = str(row.get("arrival_date") or row.get("observed_on")).strip()
                day = datetime.strptime(day, "%d/%m/%Y").date().isoformat() if "/" in day else datetime.fromisoformat(day).date().isoformat()
                if not (market and state and district and crop and price > 0):
                    raise ValueError("missing quote")
                coord = db.execute("SELECT id FROM markets WHERE lower(name)=lower(?) AND lower(state)=lower(?) AND lower(district)=lower(?)", (market,state,district)).fetchone()
                if not coord:
                    skipped += 1
                    continue
                db.execute("""INSERT INTO quotes(market_id,commodity,variety,min_price,max_price,modal_price,observed_on,source,imported_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(market_id,commodity,variety,observed_on,source)
                    DO UPDATE SET min_price=excluded.min_price,max_price=excluded.max_price,modal_price=excluded.modal_price,imported_at=excluded.imported_at""",
                    (coord[0],crop,str(row.get("variety") or ""),float(row["min_price"]) if row.get("min_price") else None,
                     float(row["max_price"]) if row.get("max_price") else None,price,day,source,now()))
                accepted += 1
            except (KeyError, ValueError, TypeError):
                skipped += 1
    return {"accepted": accepted, "skipped": skipped, "unit": "INR_per_quintal", "note": "Unmapped markets and invalid rows skipped"}
