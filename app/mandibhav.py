"""Mandi prices from Mandi Bhav India (mandibhavindia.in), a public aggregator of
AGMARKNET daily quotes with modal prices. Its robots.txt allows all crawlers.

Pages used: /en/commodity/{crop}/{state} (latest quotes + links to districts) and
/en/commodity/{crop}/{state}/{district} (every mandi in that district).
Polite: one request at a time with a pause, honest User-Agent, cached results.
Rows come back in the data.gov.in shape so the rest of the pipeline is shared.
"""
import asyncio
import html
import os
import re
import time

import httpx

BASE = "https://mandibhavindia.in/en/commodity"
HEADERS = {"User-Agent": "MandiSaathi/0.4 (student voice-agent project; github.com/peateargrifin/gnani-backend)"}
SOURCE = "mandibhavindia.in (AGMARKNET)"
PAUSE_SECONDS = 1.5
DISTRICT_PAGES = 12

CROP_SLUGS = {
    "potato": "aalu", "paddy": "dhan", "jowar": "sorghum", "ragi": "ragi-finger-millet", "moong": "green-gram-moongwhole",
    "urad": "black-gram-urd-beanswhole", "tur": "arhar-turred-gramwhole", "chana": "bengal-gram-whole", "ginger": "ginger-green",
    "green_chilli": "green-chilli", "bottle_gourd": "bottle-gourd", "bitter_gourd": "bitter-gourd", "peas": "green-peas",
    "radish": "raddish", "coriander": "coriander-leaves", "cumin": "cummin-seed", "custard_apple": "custard-apple-sharifa",
    "watermelon": "water-welon",
}

_lock = asyncio.Lock()
_last_call = 0.0
_cache = {}
_down_until = 0.0
_last_error = ""

class Unavailable(Exception):
    pass

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")

def crop_slug(crop):
    return CROP_SLUGS.get(crop, crop.replace("_", "-"))

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

def parse(page, state, district=None):
    """Quote rows from the price table (Date, [Districts], Markets, Commodity, ..., Modal Price)."""
    out = []
    for table in re.findall(r"<table.*?</table>", page, re.S):
        rows = re.findall(r"<tr.*?</tr>", table, re.S)
        if not rows:
            continue
        head = [h.casefold() for h in _cells(rows[0])]
        if "markets" not in head or "modal price" not in head:
            continue
        col = {h: i for i, h in enumerate(head)}
        for row in rows[1:]:
            c = _cells(row)
            if len(c) < len(head):
                continue
            rec = {"arrival_date": c[col["date"]], "market": c[col["markets"]], "commodity": c[col["commodity"]],
                   "variety": c[col.get("variety/grade", col["commodity"])], "state": state,
                   "district": c[col["districts"]] if "districts" in col else district,
                   "min_price": _money(c[col["min price"]]), "max_price": _money(c[col["max price"]]),
                   "modal_price": _money(c[col["modal price"]])}
            if rec["market"] and rec["district"] and rec["modal_price"]:
                out.append(rec)
    return out

def district_links(page, crop_s, state_s):
    return sorted(set(re.findall(rf'href="/en/commodity/{re.escape(crop_s)}/{re.escape(state_s)}/([a-z0-9-]+)"', page)))

def _latest_only(records):
    def day(r):
        d, m, y = (r["arrival_date"].split("/") + ["", "", ""])[:3]
        return f"{y}{m}{d}"
    best = {}
    for r in records:
        k = (r["market"], r["district"], r["variety"])
        if k not in best or day(r) > day(best[k]):
            best[k] = r
    return list(best.values())

async def rows(client, crop, state, deep=False, near=None):
    """Quotes for one crop in one state.
    near = (lat, lon, radius_km, farmer_district, locate_district) lets the source read
    the farmer's own district page first and, with deep=True, other districts in range."""
    global _down_until, _last_error
    key = (crop, state, deep, near[:3] if near else None)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < float(os.getenv("FEED_TTL_SECONDS", "300")):
        return hit[1], time.monotonic() - hit[0]
    if time.monotonic() < _down_until:
        if hit:
            return hit[1], time.monotonic() - hit[0]
        raise Unavailable(f"{_last_error}; retrying in {int(_down_until - time.monotonic())} s")
    c_slug, s_slug = crop_slug(crop), slug(state)
    try:
        page = await _get(client, f"{BASE}/{c_slug}/{s_slug}")
    except httpx.HTTPStatusError as exc:
        _last_error, _down_until = f"HTTP {exc.response.status_code}", time.monotonic() + 120
        raise Unavailable(_last_error) from exc
    except httpx.HTTPError as exc:
        _last_error, _down_until = type(exc).__name__, time.monotonic() + 120
        raise Unavailable(_last_error) from exc
    if not page:
        return [], 0.0
    records = parse(page, state)
    links = district_links(page, c_slug, s_slug)
    wanted = []
    if near:
        lat, lon, radius, farmer_district, locate = near
        own = slug(farmer_district)
        if own in links:
            wanted.append(own)
        if deep:
            from .core import distance_km
            scored = []
            for d in links:
                if d == own:
                    continue
                centre = await locate(client, d.replace("-", " ").title(), state)
                if centre:
                    km = distance_km(lat, lon, *centre)
                    if km <= radius + 30:
                        scored.append((km, d))
            wanted += [d for _, d in sorted(scored)][:DISTRICT_PAGES]
    for d in wanted:
        try:
            sub = await _get(client, f"{BASE}/{c_slug}/{s_slug}/{d}")
        except httpx.HTTPError:
            break  # keep what we have; the next refresh tries again
        if sub:
            records += parse(sub, state, d.replace("-", " ").title())
    records = _latest_only(records)
    _cache[key] = (time.monotonic(), records)
    return records, 0.0
