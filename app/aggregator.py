"""Mandi prices from Commodity Online (commodityonline.com), a public aggregator that
republishes AGMARKNET daily mandi quotes. Used when no data.gov.in key is configured.

Polite by design: one request at a time with a pause between them, an honest
User-Agent, results cached for FEED_TTL_SECONDS, and only pages robots.txt allows.
Rows are returned in the same shape as the data.gov.in feed so the rest of the
pipeline (geocoding, ingest, routing) is shared.
"""
import asyncio
import html
import os
import re
import time

import httpx

BASE = "https://www.commodityonline.com/mandiprices"
HEADERS = {"User-Agent": "MandiSaathi/0.4 (student voice-agent project; github.com/peateargrifin/gnani-backend)"}
SOURCE = "commodityonline.com (AGMARKNET)"
PAUSE_SECONDS = 1.5
MARKET_PAGES_PER_STATE = 25

# Our crop id -> Commodity Online commodity slug (only where it differs from the id).
CROP_SLUGS = {
    "paddy": "paddy-dhan-common", "bajra": "bajra-pearl-milletcumbu", "jowar": "jowar-sorghum", "ragi": "ragi-finger-millet",
    "moong": "green-gram-moong-whole", "urad": "black-gram-urd-beans-whole", "tur": "arhar-turred-gram-whole",
    "chana": "bengal-gram-gram-whole", "ginger": "ginger-green", "green_chilli": "green-chilli", "bhindi": "bhindi-ladies-finger",
    "peas": "green-peas", "radish": "raddish", "cucumber": "cucumbar-kheera", "bottle_gourd": "bottle-gourd",
    "bitter_gourd": "bitter-gourd", "coriander": "coriander-leaves", "cumin": "cummin-seed-jeera",
    "custard_apple": "custard-apple-sharifa", "watermelon": "water-melon",
}
STATE_SLUGS = {"Chhattisgarh": ["chattisgarh"], "Delhi": ["nct-of-delhi"], "Uttarakhand": ["uttarakhand", "uttrakhand"]}

_lock = asyncio.Lock()
_last_call = 0.0
_cache = {}          # (crop, state, deep) -> (fetched_at, records)
_down_until = 0.0
_last_error = ""

class Unavailable(Exception):
    pass

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")

def crop_slug(crop):
    return CROP_SLUGS.get(crop, crop.replace("_", "-"))

def state_slugs(state):
    return STATE_SLUGS.get(state, [slug(state)])

async def _get(client, url):
    global _last_call
    async with _lock:
        wait = PAUSE_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        finally:
            _last_call = time.monotonic()
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text

def _cells(row):
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in re.findall(r"<t[hd][^>]*>.*?</t[hd]>", row, re.S)]

def _money(text):
    m = re.search(r"([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None

def parse(page):
    """Rows from the detailed quote table (Commodity, Arrival Date, ..., Avg price)."""
    records, seen = [], set()
    for table in re.findall(r"<table.*?</table>", page, re.S):
        rows = re.findall(r"<tr.*?</tr>", table, re.S)
        if not rows:
            continue
        head = [h.casefold() for h in _cells(rows[0])]
        if not ("district" in head and "market" in head and "arrival date" in head):
            continue
        col = {name: head.index(name) for name in head}
        for row in rows[1:]:
            c = _cells(row)
            if len(c) < len(head) - 1:
                continue
            get = lambda name: c[col[name]] if name in col and col[name] < len(c) else ""
            rec = {"commodity": get("commodity"), "arrival_date": get("arrival date"), "variety": get("variety"),
                   "state": get("state"), "district": get("district"), "market": get("market"),
                   "min_price": _money(get("min price")), "max_price": _money(get("max price")),
                   "modal_price": _money(get("avg price"))}  # site publishes an average; used as the quote
            key = (rec["market"], rec["commodity"], rec["variety"], rec["arrival_date"])
            if rec["market"] and rec["modal_price"] and key not in seen:
                seen.add(key)
                records.append(rec)
    return records

def market_links(page, crop_s, state_s):
    return sorted(set(re.findall(rf'href="https?://www\.commodityonline\.com/mandiprices/{re.escape(crop_s)}/{re.escape(state_s)}/([a-z0-9-]+)"', page)))

def _latest_only(records):
    """Keep each market's most recent day (market pages include history)."""
    def day(r):
        d, m, y = (r["arrival_date"].split("/") + ["", "", ""])[:3]
        return f"{y}{m}{d}"
    best = {}
    for r in records:
        k = (r["market"], r["variety"])
        if k not in best or day(r) > day(best[k]):
            best[k] = r
    return list(best.values())

async def rows(client, crop, state, deep=False):
    """Quotes for one crop in one state. deep=True also visits individual mandi pages
    (the state page lists only the ~10 most recent quotes)."""
    global _down_until
    key = (crop, state, deep)
    ttl = float(os.getenv("FEED_TTL_SECONDS", "300"))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1], time.monotonic() - hit[0]
    global _last_error
    if time.monotonic() < _down_until:
        if hit:
            return hit[1], time.monotonic() - hit[0]
        raise Unavailable(f"{_last_error}; retrying in {int(_down_until - time.monotonic())} s")
    c_slug = crop_slug(crop)
    try:
        page, s_slug = None, None
        for s_slug in state_slugs(state):
            page = await _get(client, f"{BASE}/{c_slug}/{s_slug}")
            if page:
                break
    except httpx.HTTPStatusError as exc:
        _last_error = f"HTTP {exc.response.status_code}"
        _down_until = time.monotonic() + 120
        if hit:
            return hit[1], time.monotonic() - hit[0]
        raise Unavailable(_last_error) from exc
    except httpx.HTTPError as exc:
        _last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        _down_until = time.monotonic() + 120
        if hit:
            return hit[1], time.monotonic() - hit[0]
        raise Unavailable(_last_error) from exc
    if not page:
        return [], 0.0
    records = parse(page)
    if deep:
        have = {slug(r["market"]) for r in records}
        for m in [m for m in market_links(page, c_slug, s_slug) if m not in have][:MARKET_PAGES_PER_STATE]:
            try:
                sub = await _get(client, f"{BASE}/{c_slug}/{s_slug}/{m}")
            except httpx.HTTPError:
                break  # keep what we already have; try the rest on the next refresh
            if sub:
                records += parse(sub)
    records = _latest_only(records)
    _cache[key] = (time.monotonic(), records)
    return records, 0.0
