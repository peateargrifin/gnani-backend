import csv
import heapq
import io
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timezone

import httpx

LANGUAGES = {"en-IN": "English", "hi-IN": "हिन्दी", "kn-IN": "ಕನ್ನಡ", "ta-IN": "தமிழ்", "te-IN": "తెలుగు", "mr-IN": "मराठी", "bn-IN": "বাংলা", "gu-IN": "ગુજરાતી", "pa-IN": "ਪੰਜਾਬੀ", "ml-IN": "മലയാളം"}

# Aliases include native scripts and romanised Hinglish/Tanglish spellings.
CROPS = {
    "potato": ["potato", "आलू", "आलु", "ಆಲೂಗಡ್ಡೆ", "உருளைக்கிழங்கு", "బంగాళాదుంప", "aloo", "alu", "batata", "बटाटा", "urulaikizhangu", "aloogadde"],
    "sugarcane": ["sugarcane", "गन्ना", "गन्ने", "ಕಬ್ಬು", "கரும்பு", "చెరకు", "ganna", "ganne", "oos", "ऊस", "kabbu", "karumbu"],
    "onion": ["onion", "प्याज", "प्याज़", "ಈರುಳ್ಳಿ", "வெங்காயம்", "ఉల్లిపాయ", "pyaz", "pyaaz", "piyaz", "kanda", "कांदा", "eerulli", "vengayam"],
    "tomato": ["tomato", "टमाटर", "ಟೊಮೆಟೊ", "தக்காளி", "టమాటా", "tamatar", "tamaatar", "thakkali", "tamata"],
}
# How each crop is named inside the spoken reply.
CROP_LABELS = {
    "hi-IN": {"potato": "आलू", "sugarcane": "गन्ना", "onion": "प्याज़", "tomato": "टमाटर"},
    "kn-IN": {"potato": "ಆಲೂಗಡ್ಡೆ", "sugarcane": "ಕಬ್ಬು", "onion": "ಈರುಳ್ಳಿ", "tomato": "ಟೊಮೆಟೊ"},
    "ta-IN": {"potato": "உருளைக்கிழங்கு", "sugarcane": "கரும்பு", "onion": "வெங்காயம்", "tomato": "தக்காளி"},
}
MESSAGES = {
    "en-IN": "Best estimated net return for {qty} kg of {crop}: {market}, ₹{net}. Quote dated {date}. Confirm the buyer and freight before travel.",
    "hi-IN": "{qty} किलो {crop} के लिए अनुमानित सबसे अच्छी शुद्ध रकम {market} में ₹{net} है। भाव {date} का है। जाने से पहले खरीदार और भाड़े की पुष्टि करें।",
    "kn-IN": "{qty} ಕೆಜಿ {crop} ಗೆ ಅಂದಾಜು ಉತ್ತಮ ನಿವ್ವಳ ಮೊತ್ತ {market} ನಲ್ಲಿ ₹{net}. ದರದ ದಿನಾಂಕ {date}. ಹೊರಡುವ ಮೊದಲು ಖರೀದಿದಾರ ಮತ್ತು ಸಾಗಣೆ ವೆಚ್ಚ ಖಚಿತಪಡಿಸಿ.",
    "ta-IN": "{qty} கிலோ {crop} க்கான அதிக மதிப்பிடப்பட்ட நிகர வரவு {market} சந்தையில் ₹{net}. விலை தேதி {date}. பயணத்திற்கு முன் வாங்குபவர் மற்றும் போக்குவரத்து கட்டணத்தை உறுதிசெய்யவும்.",
}

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    db = sqlite3.connect(os.getenv("DATABASE_PATH", "./mandi.db"))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db():
    with connect() as db:
        db.executescript("""
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

def crop_name(text):
    value = text.casefold().strip()
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

async def road_matrix(client, points):
    """All-pairs road distance (km) between points via one OSRM table call.
    Falls back to straight-line distance for any pair OSRM cannot answer."""
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
    base = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
    coords = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    try:
        resp = await client.get(f"{base}/route/v1/driving/{coords}", params={"overview": "simplified", "geometries": "geojson"}, timeout=6)
        resp.raise_for_status()
        return [[lat, lon] for lon, lat in resp.json()["routes"][0]["geometry"]["coordinates"]]
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

# Synthetic demo data: real mandi towns with approximate coordinates, made-up prices (INR/quintal).
DEMO_MARKETS = [
    # name, state, district, lat, lon, {crop: modal price}
    ("Agra", "Uttar Pradesh", "Agra", 27.1767, 78.0081, {"potato": 1180, "onion": 2150, "tomato": 1500}),
    ("Fatehabad", "Uttar Pradesh", "Agra", 27.0260, 78.3030, {"potato": 1120, "onion": 2050}),
    ("Firozabad", "Uttar Pradesh", "Firozabad", 27.1592, 78.3957, {"potato": 1240, "onion": 2200, "tomato": 1450}),
    ("Shikohabad", "Uttar Pradesh", "Firozabad", 27.1080, 78.5870, {"potato": 1390, "onion": 2100}),
    ("Mathura", "Uttar Pradesh", "Mathura", 27.4924, 77.6737, {"potato": 1300, "onion": 2400, "tomato": 1700}),
    ("Hathras", "Uttar Pradesh", "Hathras", 27.5960, 78.0499, {"potato": 1260, "onion": 2250, "tomato": 1600, "sugarcane": 370}),
    ("Aligarh", "Uttar Pradesh", "Aligarh", 27.8974, 78.0880, {"potato": 1420, "onion": 2500, "tomato": 1850, "sugarcane": 375}),
    ("Mainpuri", "Uttar Pradesh", "Mainpuri", 27.2350, 79.0270, {"potato": 1510, "onion": 2300, "sugarcane": 365}),
    ("Kolar", "Karnataka", "Kolar", 13.1360, 78.1290, {"tomato": 1650, "onion": 2050, "potato": 1700}),
    ("Chintamani", "Karnataka", "Chikkaballapur", 13.4000, 78.0560, {"tomato": 1500, "potato": 1650}),
    ("Mulbagal", "Karnataka", "Kolar", 13.1650, 78.3930, {"tomato": 1580, "onion": 1950}),
    ("Chikkaballapur", "Karnataka", "Chikkaballapur", 13.4355, 77.7315, {"tomato": 1720, "onion": 2150, "potato": 1760}),
    ("Hoskote", "Karnataka", "Bengaluru Rural", 13.0707, 77.7980, {"tomato": 1800, "onion": 2300, "potato": 1850}),
    ("Bengaluru (Binny Mill)", "Karnataka", "Bengaluru Urban", 12.9634, 77.5770, {"tomato": 2050, "onion": 2600, "potato": 2000}),
    ("Mandya", "Karnataka", "Mandya", 12.5223, 76.8970, {"sugarcane": 360, "tomato": 1600}),
]

def seed_demo():
    """Insert demo mandis and today's synthetic quotes. Safe to run on every start."""
    today = date.today().isoformat()
    with connect() as db:
        for name, state, district, lat, lon, _ in DEMO_MARKETS:
            db.execute("INSERT INTO markets(name,state,district,latitude,longitude) VALUES(?,?,?,?,?) ON CONFLICT(name,state,district) DO NOTHING", (name, state, district, lat, lon))
    rows = [{"market": n, "state": s, "district": d, "commodity": crop, "modal_price": price,
             "min_price": round(price*0.9), "max_price": round(price*1.08), "arrival_date": today}
            for n, s, d, _, _, prices in DEMO_MARKETS for crop, price in prices.items()]
    return ingest(rows, "demo_synthetic")
