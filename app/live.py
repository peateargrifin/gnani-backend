"""Live mandi prices from the Government of India's AGMARKNET feed on data.gov.in.

Pipeline for a farmer at (lat, lon):
1. Reverse-geocode the farm to its state and district (OpenStreetMap Nominatim).
2. Pull today's quotes for the whole state, all commodities, from data.gov.in
   (cached for FEED_TTL_SECONDS so frequent page refreshes never hammer the API).
3. Locate every mandi in the feed: the farmer's own district first, then the rest
   of the state, then mandis in neighbouring-state districts within the search
   radius. Coordinates are cached in SQLite forever, so this happens once per mandi.
4. Ingest quotes as soon as their mandi is located, so results grow while the
   background job runs and the page's auto-refresh picks them up.
"""
import asyncio
import os
import re
import time

import httpx

from . import aggregator, mandibhav
from .core import connect, distance_km, ingest, now

RESOURCE = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"  # Current Daily Price of Various Commodities from Various Markets (Mandi)
SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"  # data.gov.in public sample key, max 10 rows
NOMINATIM = "https://nominatim.openstreetmap.org"
HEADERS = {"User-Agent": "MandiSaathi/0.4 (student voice-agent project)"}

STATE_NEIGHBOURS = {
    "Uttar Pradesh": ["Uttarakhand", "Himachal Pradesh", "Haryana", "Delhi", "Rajasthan", "Madhya Pradesh", "Chhattisgarh", "Jharkhand", "Bihar"],
    "Karnataka": ["Maharashtra", "Goa", "Kerala", "Tamil Nadu", "Andhra Pradesh", "Telangana"],
    "Tamil Nadu": ["Kerala", "Karnataka", "Andhra Pradesh", "Puducherry"],
    "Kerala": ["Karnataka", "Tamil Nadu", "Puducherry"],
    "Andhra Pradesh": ["Telangana", "Karnataka", "Tamil Nadu", "Odisha", "Chhattisgarh"],
    "Telangana": ["Maharashtra", "Chhattisgarh", "Andhra Pradesh", "Karnataka"],
    "Maharashtra": ["Gujarat", "Madhya Pradesh", "Chhattisgarh", "Telangana", "Karnataka", "Goa"],
    "Gujarat": ["Rajasthan", "Madhya Pradesh", "Maharashtra"],
    "Rajasthan": ["Punjab", "Haryana", "Uttar Pradesh", "Madhya Pradesh", "Gujarat"],
    "Madhya Pradesh": ["Rajasthan", "Uttar Pradesh", "Chhattisgarh", "Maharashtra", "Gujarat"],
    "Punjab": ["Haryana", "Rajasthan", "Himachal Pradesh", "Chandigarh", "Jammu and Kashmir"],
    "Haryana": ["Punjab", "Himachal Pradesh", "Uttar Pradesh", "Rajasthan", "Delhi", "Chandigarh"],
    "Delhi": ["Haryana", "Uttar Pradesh"],
    "Chandigarh": ["Punjab", "Haryana"],
    "Bihar": ["Uttar Pradesh", "Jharkhand", "West Bengal"],
    "West Bengal": ["Bihar", "Jharkhand", "Odisha", "Assam", "Sikkim"],
    "Odisha": ["West Bengal", "Jharkhand", "Chhattisgarh", "Andhra Pradesh"],
    "Jharkhand": ["Bihar", "Uttar Pradesh", "Chhattisgarh", "Odisha", "West Bengal"],
    "Chhattisgarh": ["Madhya Pradesh", "Uttar Pradesh", "Jharkhand", "Odisha", "Andhra Pradesh", "Telangana", "Maharashtra"],
    "Uttarakhand": ["Himachal Pradesh", "Uttar Pradesh"],
    "Himachal Pradesh": ["Jammu and Kashmir", "Punjab", "Haryana", "Uttarakhand"],
    "Jammu and Kashmir": ["Himachal Pradesh", "Punjab"],
    "Goa": ["Maharashtra", "Karnataka"],
    "Puducherry": ["Tamil Nadu", "Kerala", "Andhra Pradesh"],
    "Assam": ["West Bengal", "Meghalaya", "Nagaland", "Arunachal Pradesh", "Manipur", "Mizoram", "Tripura"],
}
# The feed and OpenStreetMap spell some states differently.
FEED_SPELLINGS = {"Uttarakhand": ["Uttarakhand", "Uttrakhand"], "Chhattisgarh": ["Chhattisgarh", "Chattisgarh"],
                  "Delhi": ["NCT of Delhi", "Delhi"], "Odisha": ["Odisha", "Orissa"], "Puducherry": ["Puducherry", "Pondicherry"]}
_STATE_ALIASES = {"uttrakhand": "uttarakhand", "chattisgarh": "chhattisgarh", "nctofdelhi": "delhi", "orissa": "odisha", "pondicherry": "puducherry"}

_geo_lock = asyncio.Lock()
_last_geo_call = 0.0
_rows = {}           # state -> (fetched_at, records)
_jobs = {}           # area key -> (task, progress dict)
_feed_down_until = 0.0

def enabled():
    return os.getenv("LIVE_PRICES", "0").lower() in ("1", "true", "yes")

def api_key():
    return os.getenv("DATA_GOV_API_KEY") or SAMPLE_KEY

def source():
    """data.gov.in when the user has configured a key, otherwise the public aggregator."""
    choice = os.getenv("PRICE_SOURCE", "auto").casefold()
    if choice in ("datagov", "mandibhav", "commodityonline"):
        return choice
    return "datagov" if os.getenv("DATA_GOV_API_KEY") else "aggregators"

_active_label = mandibhav.SOURCE

def source_label():
    return "data.gov.in (AGMARKNET)" if source() == "datagov" else _active_label

def _aggregators():
    """Public aggregators in the order they are tried."""
    choice = source()
    if choice == "mandibhav":
        return [mandibhav]
    if choice == "commodityonline":
        return [aggregator]
    return [mandibhav, aggregator]

async def feed(client, state, crop, deep=False, near=None):
    """(records, age_seconds) for a state from the active source. Ingests what it can.
    Aggregators are tried in order; the first one with quotes wins."""
    global _active_label
    state = canonical_state(state)
    if source() == "datagov":
        return await state_rows(client, state)
    errors, mods = [], _aggregators()
    for i, mod in enumerate(mods):
        try:
            if mod is mandibhav:
                records, age = await mod.rows(client, crop, state, deep, (*near, geocode_district) if near else None)
            else:
                records, age = await mod.rows(client, crop, state, deep)
        except (mandibhav.Unavailable, aggregator.Unavailable) as exc:
            errors.append(f"{mod.SOURCE}: {exc}")
            continue
        if records or i == len(mods) - 1:
            for r in records:
                r["_source"] = mod.SOURCE
            ingest(records, mod.SOURCE)
            _active_label = mod.SOURCE
            return records, age
    raise FeedUnavailable(" | ".join(errors) or "no price source answered")

def _ingest(records):
    by_source = {}
    for r in records:
        by_source.setdefault(r.get("_source", "data.gov.in"), []).append(r)
    for label, rows in by_source.items():
        ingest(rows, label)

def feed_ttl():
    return float(os.getenv("FEED_TTL_SECONDS", "300"))

def norm_state(name):
    key = re.sub(r"[^a-z]", "", (name or "").casefold())
    return _STATE_ALIASES.get(key, key)

def canonical_state(name):
    """Map any spelling to the key used in STATE_NEIGHBOURS."""
    target = norm_state(name)
    for state in STATE_NEIGHBOURS:
        if norm_state(state) == target:
            return state
    return name

def init_cache():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS geocodes (query TEXT PRIMARY KEY, latitude REAL, longitude REAL, label TEXT, created_at TEXT NOT NULL)")

# ---------- OpenStreetMap ----------
async def _nominatim(client, path, params):
    """Nominatim's usage policy allows at most one request per second."""
    global _last_geo_call
    async with _geo_lock:
        wait = 1.05 - (time.monotonic() - _last_geo_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await client.get(f"{NOMINATIM}/{path}", params={**params, "format": "jsonv2"}, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        finally:
            _last_geo_call = time.monotonic()

def _cache_get(key):
    with connect() as db:
        return db.execute("SELECT latitude, longitude, label FROM geocodes WHERE query=?", (key,)).fetchone()

def _cache_put(key, lat, lon, label):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO geocodes VALUES(?,?,?,?,?)", (key, lat, lon, label, now()))

async def find_place(client, query):
    """{lat, lon, label} for a typed village/town in India. Cached."""
    key = f"place:{query}".casefold()
    hit = _cache_get(key)
    if hit:
        return {"lat": hit[0], "lon": hit[1], "label": hit[2]} if hit[0] is not None else None
    data = await _nominatim(client, "search", {"q": f"{query}, India", "countrycodes": "in", "limit": 1, "addressdetails": 1})
    found = None
    if data:
        a = data[0].get("address") or {}
        town = a.get("village") or a.get("town") or a.get("city") or a.get("hamlet") or data[0].get("name") or query
        district = a.get("state_district") or a.get("county") or ""
        label = ", ".join(x for x in (town, re.sub(r"\s+district$", "", district, flags=re.I), a.get("state", "")) if x)
        found = {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]), "label": label}
    _cache_put(key, found["lat"] if found else None, found["lon"] if found else None, found["label"] if found else "not_found")
    return found

async def place_of(client, lat, lon):
    """(state, district) for a farm location."""
    key = f"reverse:{round(lat, 2)},{round(lon, 2)}"
    hit = _cache_get(key)
    if hit:
        state, _, district = hit[2].partition("|")
        return state, district or None
    data = await _nominatim(client, "reverse", {"lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1})
    address = (data or {}).get("address") or {}
    state = address.get("state")
    district = address.get("state_district") or address.get("county") or address.get("city")
    if district:
        district = re.sub(r"\s+(district|tahsil|tehsil)$", "", district, flags=re.I)
    if state:
        _cache_put(key, lat, lon, f"{state}|{district or ''}")
    return state, district

def _clean(market):
    name = re.sub(r"\(.*?\)", " ", market)
    name = re.sub(r"\b(apmc|mandi|market|yard|sabji|subzi|f\s*&\s*v|grain|new|old|krishi upaj)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" ,-") or market

async def _search(client, query, state):
    data = await _nominatim(client, "search", {"q": query, "countrycodes": "in", "limit": 1, "addressdetails": 1})
    if data:
        place = data[0]
        place_state = (place.get("address") or {}).get("state", "")
        if not place_state or norm_state(place_state) == norm_state(state):
            return float(place["lat"]), float(place["lon"])
    return None

async def geocode_district(client, district, state):
    key = f"district:{district}|{state}".casefold()
    hit = _cache_get(key)
    if hit:
        return (hit[0], hit[1]) if hit[0] is not None else None
    found = await _search(client, f"{district} district, {state}, India", state) or await _search(client, f"{district}, {state}, India", state)
    _cache_put(key, *(found or (None, None)), "district" if found else "not_found")
    return found

async def geocode_market(client, market, district, state):
    """Coordinates for a mandi: town name first, district as fallback. Cached forever."""
    key = f"market:{market}|{district}|{state}".casefold()
    hit = _cache_get(key)
    if hit:
        return (hit[0], hit[1], hit[2]) if hit[0] is not None else None
    found = await _search(client, f"{_clean(market)}, {district}, {state}, India", state)
    precision = "market"
    if not found:
        found, precision = await geocode_district(client, district, state), "district"
    _cache_put(key, *(found or (None, None)), precision if found else "not_found")
    return (*found, precision) if found else None

# ---------- data.gov.in ----------
async def _fetch(client, feed_state):
    records, offset = [], 0
    params = {"api-key": api_key(), "format": "json", "limit": 1000, "filters[state.keyword]": feed_state}
    while True:
        resp = await client.get(RESOURCE, params={**params, "offset": offset}, timeout=20)
        resp.raise_for_status()
        batch = resp.json().get("records", [])
        records += batch
        if len(batch) < 1000 or api_key() == SAMPLE_KEY or offset > 20000:
            return records
        offset += 1000

async def state_rows(client, state):
    """Today's quotes for every commodity in one state. Cached for FEED_TTL_SECONDS."""
    global _feed_down_until
    state = canonical_state(state)
    cached = _rows.get(state)
    if cached and time.monotonic() - cached[0] < feed_ttl():
        return cached[1], time.monotonic() - cached[0]
    if time.monotonic() < _feed_down_until:
        if cached:
            return cached[1], time.monotonic() - cached[0]
        raise FeedUnavailable("recently failed, retrying shortly")
    try:
        records = []
        for spelling in FEED_SPELLINGS.get(state, [state]):
            records = await _fetch(client, spelling)
            if records:
                break
    except httpx.HTTPStatusError as exc:
        _feed_down_until = time.monotonic() + 120
        if cached:
            return cached[1], time.monotonic() - cached[0]
        raise FeedUnavailable("rate limited (429): add your own DATA_GOV_API_KEY" if exc.response.status_code == 429 else f"HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        _feed_down_until = time.monotonic() + 120
        if cached:
            return cached[1], time.monotonic() - cached[0]
        raise FeedUnavailable(type(exc).__name__) from exc
    _rows[state] = (time.monotonic(), records)
    ingest(records, "data.gov.in")  # quotes for already-located mandis are usable immediately
    return records, 0.0

class FeedUnavailable(Exception):
    pass

# ---------- locating mandis ----------
def _market_ids(records):
    seen = {}
    for r in records:
        ident = (str(r.get("market", "")).strip(), str(r.get("district", "")).strip(), str(r.get("state", "")).strip())
        if all(ident):
            seen.setdefault(ident, None)
    return list(seen)

def _registered(ident):
    with connect() as db:
        return db.execute("SELECT 1 FROM markets WHERE lower(name)=lower(?) AND lower(district)=lower(?) AND lower(state)=lower(?)", ident).fetchone() is not None

async def _locate(client, ident):
    coords = await geocode_market(client, *ident)
    if coords:
        with connect() as db:
            db.execute("INSERT INTO markets(name,state,district,latitude,longitude) VALUES(?,?,?,?,?) ON CONFLICT(name,state,district) DO NOTHING",
                       (ident[0], ident[2], ident[1], coords[0], coords[1]))
    return coords

async def _locate_area(state, district, lat, lon, radius, crop, progress):
    async with httpx.AsyncClient() as client:
        # 1. Farmer's own state, own district first.
        records, _ = await feed(client, state, crop, deep=True, near=(lat, lon, radius, district))
        todo = [m for m in _market_ids(records) if not _registered(m)]
        todo.sort(key=lambda m: norm_state(m[1]) != norm_state(district or ""))
        progress["total"] += len(todo)
        for n, ident in enumerate(todo, 1):
            await _locate(client, ident)
            progress["done"] += 1
            if n % 5 == 0:
                _ingest(records)
        _ingest(records)
        # 2. Neighbouring states: only districts within reach of the farm.
        for nb in STATE_NEIGHBOURS.get(canonical_state(state), []):
            try:
                nb_records, _ = await feed(client, nb, crop, deep=True, near=(lat, lon, radius, None))
            except FeedUnavailable:
                continue
            idents = [m for m in _market_ids(nb_records) if not _registered(m)]
            districts = sorted({(m[1], m[2]) for m in idents})
            progress["total"] += len(districts)
            near = set()
            for d, s in districts:
                centre = await geocode_district(client, d, s)
                progress["done"] += 1
                if centre and distance_km(lat, lon, *centre) <= radius + 30:
                    near.add((d, s))
            nearby = [m for m in idents if (m[1], m[2]) in near]
            progress["total"] += len(nearby)
            for ident in nearby:
                await _locate(client, ident)
                progress["done"] += 1
            _ingest(nb_records)

async def ensure_area(client, lat, lon, radius, crop, wait_seconds=8.0):
    """Make sure live quotes for mandis around the farm are in the database.
    Waits up to `wait_seconds` for new mandis to be located; the rest continue in
    the background and appear on later requests."""
    state, district = await place_of(client, lat, lon)
    if not state:
        return {"status": "location_unknown", "error": "could not find the state for this location"}
    status = {"state": state, "district": district, "source": source_label(),
              "sample_key": source() == "datagov" and api_key() == SAMPLE_KEY}
    try:
        records, age = await feed(client, state, crop, near=(lat, lon, radius, district))
    except FeedUnavailable as exc:
        return {**status, "status": "feed_unavailable", "error": str(exc)}
    status["source"] = source_label()
    key = (norm_state(state), round(lat, 1), round(lon, 1), round(radius), crop if source() != "datagov" else "*")
    job = _jobs.get(key)
    if job is None or (job[0].done() and time.monotonic() - job[1]["finished"] > feed_ttl()):
        progress = {"done": 0, "total": 0, "finished": 0.0}
        task = asyncio.create_task(_locate_area(state, district, lat, lon, radius, crop, progress))
        task.add_done_callback(lambda t, p=progress: p.update(finished=time.monotonic()))
        _jobs[key] = job = (task, progress)
    task, progress = job
    if wait_seconds and not task.done():
        await asyncio.wait({task}, timeout=wait_seconds)
    return {**status, "status": "live", "rows_in_feed": len(records), "feed_age_seconds": round(age),
            "markets_still_locating": 0 if task.done() else max(0, progress["total"] - progress["done"]),
            "searching": not task.done()}

async def prewarm(states, crops=("potato", "onion", "tomato")):
    """Locate mandis in these states at startup so the first farmer request is fast."""
    for state in states:
        for crop in crops if source() != "datagov" else ("*",):
            try:
                async with httpx.AsyncClient() as client:
                    records, _ = await feed(client, state, crop)
                    for ident in [m for m in _market_ids(records) if not _registered(m)]:
                        await _locate(client, ident)
                    _ingest(records)
            except FeedUnavailable:
                continue
