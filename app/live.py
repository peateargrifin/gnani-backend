"""Live mandi prices from the Government of India's AGMARKNET feed on data.gov.in,
with automatic market geocoding via OpenStreetMap Nominatim (cached in SQLite)."""
import asyncio
import os
import re
import time

import httpx

from .core import connect, ingest, now

RESOURCE = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"  # Current Daily Price of Various Commodities from Various Markets (Mandi)
SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"  # data.gov.in public sample key, max 10 rows
NOMINATIM = "https://nominatim.openstreetmap.org"
HEADERS = {"User-Agent": "MandiSaathi/0.3 (student voice-agent demo)"}
FEED_NAMES = {"potato": "Potato", "onion": "Onion", "tomato": "Tomato", "sugarcane": "Sugarcane"}
CACHE_SECONDS = 30 * 60

_geo_lock = asyncio.Lock()
_last_geo_call = 0.0
_fresh = {}          # (crop, state) -> time of last successful refresh
_background = set()  # keep references to background geocoding tasks
_feed_down_until = 0.0

def enabled():
    return os.getenv("LIVE_PRICES", "0").lower() in ("1", "true", "yes")

def api_key():
    return os.getenv("DATA_GOV_API_KEY") or SAMPLE_KEY

def init_cache():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS geocodes (query TEXT PRIMARY KEY, latitude REAL, longitude REAL, label TEXT, created_at TEXT NOT NULL)")

async def _nominatim(client, path, params):
    """Nominatim allows at most one request per second."""
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

async def state_of(client, lat, lon):
    key = f"reverse:{round(lat, 2)},{round(lon, 2)}"
    with connect() as db:
        hit = db.execute("SELECT label FROM geocodes WHERE query=?", (key,)).fetchone()
    if hit:
        return hit[0]
    data = await _nominatim(client, "reverse", {"lat": lat, "lon": lon, "zoom": 5, "addressdetails": 1})
    state = ((data or {}).get("address") or {}).get("state")
    if state:
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO geocodes VALUES(?,?,?,?,?)", (key, lat, lon, state, now()))
    return state

def _clean(market):
    name = re.sub(r"\(.*?\)", " ", market)
    name = re.sub(r"\b(apmc|mandi|market|yard|sabji|subzi|f\s*&\s*v|grain|new|old)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" ,-") or market

async def geocode_market(client, market, district, state):
    """Coordinates for a mandi: town name first, district as fallback. Cached forever."""
    key = f"market:{market}|{district}|{state}".casefold()
    with connect() as db:
        hit = db.execute("SELECT latitude, longitude, label FROM geocodes WHERE query=?", (key,)).fetchone()
    if hit:
        return (hit[0], hit[1], hit[2]) if hit[0] is not None else None
    found = None
    for query, precision in ((f"{_clean(market)}, {district}, {state}, India", "market"), (f"{district} district, {state}, India", "district")):
        data = await _nominatim(client, "search", {"q": query, "countrycodes": "in", "limit": 1, "addressdetails": 1})
        if data:
            place = data[0]
            place_state = (place.get("address") or {}).get("state", "")
            if not place_state or place_state.casefold() == state.casefold():
                found = (float(place["lat"]), float(place["lon"]), precision)
                break
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO geocodes VALUES(?,?,?,?,?)", (key, *(found or (None, None, "not_found")), now()))
    return found

async def fetch_feed(client, crop, state):
    records, offset = [], 0
    params = {"api-key": api_key(), "format": "json", "limit": 1000, "filters[commodity]": FEED_NAMES.get(crop, crop.title())}
    if state:
        params["filters[state.keyword]"] = state
    while True:
        resp = await client.get(RESOURCE, params={**params, "offset": offset}, timeout=15)
        resp.raise_for_status()
        batch = resp.json().get("records", [])
        records += batch
        if len(batch) < 1000 or api_key() == SAMPLE_KEY:
            return records
        offset += 1000

async def _register(client, records, deadline):
    """Make sure each market in the feed exists in the registry with coordinates."""
    pending = []
    seen = set()
    for r in records:
        ident = (str(r.get("market", "")).strip(), str(r.get("district", "")).strip(), str(r.get("state", "")).strip())
        if not all(ident) or ident in seen:
            continue
        seen.add(ident)
        with connect() as db:
            if db.execute("SELECT 1 FROM markets WHERE lower(name)=lower(?) AND lower(district)=lower(?) AND lower(state)=lower(?)", ident).fetchone():
                continue
        if deadline is not None and time.monotonic() > deadline:
            pending.append(ident)
            continue
        coords = await geocode_market(client, *ident)
        if coords:
            with connect() as db:
                db.execute("INSERT INTO markets(name,state,district,latitude,longitude) VALUES(?,?,?,?,?) ON CONFLICT(name,state,district) DO NOTHING",
                           (ident[0], ident[2], ident[1], coords[0], coords[1]))
    return pending

async def _finish_later(records):
    async with httpx.AsyncClient() as client:
        await _register(client, records, None)
    ingest(records, "data.gov.in")

async def refresh(client, crop, state, budget_seconds=10.0):
    """Pull today's government quotes for one crop and state into the database.
    Geocodes as many new markets as fit in the time budget; the rest continue in the background."""
    if crop not in FEED_NAMES:
        return {"status": "unsupported_crop"}
    cache_key = (crop, state)
    if time.monotonic() - _fresh.get(cache_key, -1e9) < CACHE_SECONDS:
        return {"status": "cached"}
    global _feed_down_until
    if time.monotonic() < _feed_down_until:
        return {"status": "feed_unavailable", "error": "recently failed, retrying shortly", "sample_key": api_key() == SAMPLE_KEY}
    try:
        records = await fetch_feed(client, crop, state)
    except httpx.HTTPStatusError as exc:
        _feed_down_until = time.monotonic() + 120
        reason = "rate limited (429): use your own DATA_GOV_API_KEY" if exc.response.status_code == 429 else f"HTTP {exc.response.status_code}"
        return {"status": "feed_unavailable", "error": reason, "sample_key": api_key() == SAMPLE_KEY}
    except (httpx.HTTPError, ValueError) as exc:
        _feed_down_until = time.monotonic() + 120
        return {"status": "feed_unavailable", "error": type(exc).__name__, "sample_key": api_key() == SAMPLE_KEY}
    pending = await _register(client, records, time.monotonic() + budget_seconds)
    result = ingest(records, "data.gov.in")
    if pending:
        task = asyncio.create_task(_finish_later(records))
        _background.add(task)
        task.add_done_callback(_background.discard)
    else:
        _fresh[cache_key] = time.monotonic()
    return {"status": "refreshed", "rows": len(records), "accepted": result["accepted"], "markets_still_locating": len(pending),
            "sample_key": api_key() == SAMPLE_KEY}

async def prewarm(states, crops=tuple(FEED_NAMES)):
    """Run at startup so the first farmer request is already fast."""
    async with httpx.AsyncClient() as client:
        for state in states:
            for crop in crops:
                await refresh(client, crop, state, budget_seconds=1e9)
