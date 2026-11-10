import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import live, transport
from .core import CROP_LABELS, CROP_TABLE, LANGUAGES, MESSAGES, build_graph, connect, crop_name, dijkstra, distance_km, ingest, init_db, now, parse_intent, path_to, read_rows, road_data, route_geometry, tomtom_key, traffic_route, TOMTOM

load_dotenv()
GNANI = "https://api.vachana.ai"
# Speech context for Prisma: mandi words plus every crop in English and Hindi.
KEYTERMS = ",".join(["mandi", "quintal", "मंडी", "किलो", "क्विंटल"] + [w for en, hi, _ in CROP_TABLE.values() for w in (en.split(" / ")[0], hi)][:45])
STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app):
    init_db()
    live.init_cache()
    warm = None
    states = [s.strip() for s in os.getenv("PREWARM_STATES", "").split(",") if s.strip()]
    if live.enabled() and states:
        warm = asyncio.create_task(live.prewarm(states))  # locate mandis in the background
    yield
    if warm:
        warm.cancel()

app = FastAPI(title="Mandi Saathi API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")], allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["Content-Type","X-Admin-Token"])

def admin(token):
    configured = os.getenv("ADMIN_TOKEN")
    if not configured or token != configured:
        raise HTTPException(403, "Admin token required")

def gnani_key():
    key = os.getenv("GNANI_API_KEY")
    if not key:
        raise HTTPException(503, "Gnani is not configured")
    return {"X-API-Key-ID": key}

async def upstream(method, url, **kwargs):
    try:
        async with httpx.AsyncClient() as client:
            result = await client.request(method, url, timeout=35, **kwargs)
        result.raise_for_status()
        return result
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"Upstream returned HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Upstream unavailable") from exc

def inr(value):
    """Indian digit grouping: 125000 -> 1,25,000."""
    whole = str(int(round(abs(value))))
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    text = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if value < 0 else "") + text

class MarketIn(BaseModel):
    name: str = Field(min_length=2)
    state: str = Field(min_length=2)
    district: str = Field(min_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

class CompareIn(BaseModel):
    commodity: str = Field(min_length=2)
    quantity_kg: float = Field(gt=0, le=1000000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=150, gt=0, le=1000)
    max_age_days: int = Field(default=7, ge=0, le=90)
    wait_seconds: float = Field(default=8, ge=0, le=20)  # 0 for quick auto-refresh polls
    own_vehicle: str | None = None  # None = compare transporters; else bike/car/tractor/three_wheeler/tata_ace/pickup/truck
    language: str = "en-IN"

class DispatchIn(BaseModel):
    market_id: int
    commodity: str
    quantity_kg: float = Field(gt=0)
    contact: str = Field(min_length=5, max_length=100)
    note: str = Field(default="", max_length=500)

class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=800)
    language: str = "hi-IN"
    voice: str = "Shivani"

class LocateIn(BaseModel):
    query: str = Field(min_length=2, max_length=120)

class IntentIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)

@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "index.html")

@app.get("/pitch", include_in_schema=False)
def pitch():
    """One-page solution pitch for the challenge submission."""
    return FileResponse(STATIC / "pitch.html")

@app.get("/health")
def health():
    return {"status":"ok", "gnani_configured":bool(os.getenv("GNANI_API_KEY")), "live_prices":live.enabled(),
        "price_source":live.source_label() if live.enabled() else "database"}

@app.get("/v1/languages")
def languages():
    return LANGUAGES

@app.get("/v1/commodities")
def commodities():
    return [{"id": crop, "en": en, "hi": hi} for crop, (en, hi, _) in CROP_TABLE.items()]

@app.get("/v1/markets")
def markets():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM markets ORDER BY state,district,name")]

@app.post("/v1/locate")
async def locate(item: LocateIn):
    """Village or town name -> coordinates (OpenStreetMap), for farmers who don't share GPS."""
    async with httpx.AsyncClient() as client:
        place = await live.find_place(client, item.query)
    if not place:
        raise HTTPException(404, "Place not found")
    return place

@app.post("/v1/intent")
def intent(item: IntentIn):
    return {"transcript": item.text, "intent": parse_intent(item.text), "reply": transport.parse_reply(item.text)}

@app.post("/v1/admin/markets")
def add_market(item: MarketIn, x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    with connect() as db:
        db.execute("INSERT INTO markets(name,state,district,latitude,longitude) VALUES(?,?,?,?,?) ON CONFLICT(name,state,district) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude", tuple(item.model_dump().values()))
        return {"market": dict(db.execute("SELECT * FROM markets WHERE name=? AND state=? AND district=?", (item.name,item.state,item.district)).fetchone())}

@app.post("/v1/admin/quotes/import")
async def import_quotes(file: UploadFile = File(...), x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    if not file.filename or not file.filename.lower().endswith((".csv", ".json")):
        raise HTTPException(400, "Upload CSV or JSON")
    content = await file.read(5_000_001)
    if len(content) > 5_000_000:
        raise HTTPException(413, "File too large")
    try:
        return ingest(read_rows(content, file.filename), "admin_upload")
    except (ValueError, UnicodeError, KeyError) as exc:
        raise HTTPException(400, "Invalid quote file") from exc

@app.post("/v1/admin/quotes/sync")
async def sync_quotes(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    url, key = os.getenv("DATA_GOV_RESOURCE_URL"), os.getenv("DATA_GOV_API_KEY")
    if not url or not key or not url.startswith("https://api.data.gov.in/resource/"):
        raise HTTPException(503, "Official data.gov.in resource URL and API key required")
    total = {"accepted":0,"skipped":0}
    for offset in range(0, 10000, 1000):
        response = await upstream("GET", url, params={"api-key":key,"format":"json","limit":1000,"offset":offset})
        try:
            records = response.json()["records"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, "Invalid data.gov.in response") from exc
        batch = ingest(records, "data.gov.in")
        total["accepted"] += batch["accepted"]
        total["skipped"] += batch["skipped"]
        if len(records) < 1000:
            break
    return {**total,"source":"data.gov.in", "unit":"INR_per_quintal"}

TRANSPORT_MESSAGES = {
    "en-IN": "You can make the most, about ₹{net}, by taking {qty} kg of {crop} to {market} mandi with {provider} {vehicle}. Price dated {date}. Transport is an estimate, so confirm the fare before booking.",
    "hi-IN": "{qty} किलो {crop} {provider} के {vehicle} से {market} मंडी ले जाने पर आपको सबसे ज़्यादा, लगभग ₹{net} बचेंगे। भाव {date} का है। भाड़ा अनुमान है, बुकिंग से पहले पुष्टि करें।",
}
OWN_MESSAGES = {
    "en-IN": "With your own {vehicle}, {market} mandi gives the most: about ₹{net} for {qty} kg of {crop}, after fuel, tolls and permit costs. Price dated {date}.",
    "hi-IN": "अपने {vehicle} से {market} मंडी जाने पर {qty} किलो {crop} के लिए सबसे ज़्यादा, लगभग ₹{net} बचेंगे, ईंधन, टोल और परमिट खर्च घटाकर। भाव {date} का है।",
}

@app.post("/v1/compare")
async def compare(item: CompareIn):
    crop = crop_name(item.commodity)
    if item.language not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    if item.own_vehicle and item.own_vehicle not in transport.OWN_VEHICLES:
        raise HTTPException(400, "Unknown own vehicle")
    live_status, farmer_state = None, None
    if live.enabled():
        async with httpx.AsyncClient() as client:
            live_status = await live.ensure_area(client, item.latitude, item.longitude, item.radius_km, crop, item.wait_seconds)
        farmer_state = live_status.get("state")
    today = date.today()
    with connect() as db:
        rows = [dict(row) for row in db.execute("""SELECT q.*,m.name,m.state,m.district,m.latitude,m.longitude FROM quotes q JOIN markets m ON m.id=q.market_id
            WHERE q.commodity=? AND q.observed_on>=? AND q.observed_on<=? ORDER BY q.observed_on DESC,q.id DESC""",
            (crop, date.fromordinal(today.toordinal()-item.max_age_days).isoformat(), today.isoformat()))]
    price_source = f"{live.source_label()}, live" if live.enabled() else "database"
    latest = {}
    for row in rows:  # latest quote per market
        latest.setdefault(row["market_id"], row)
    nearby = [r for r in latest.values() if distance_km(item.latitude,item.longitude,r["latitude"],r["longitude"]) <= item.radius_km]
    neighbours = int(os.getenv("GRAPH_NEIGHBOURS", "4"))
    qty = item.quantity_kg
    opts = transport.options(qty, item.own_vehicle)
    # One physical graph: node 0 = farm, 1..n = mandis, links to nearest places.
    # Every transport option puts its own rupee weights on the same links.
    points = [(item.latitude,item.longitude)] + [(r["latitude"],r["longitude"]) for r in nearby]
    names = ["Your farm"] + [r["name"] for r in nearby]
    results, edges, geometry, road = [], [], None, {"method": None, "traffic": False}
    async with httpx.AsyncClient() as client:
        if nearby:
            road = await road_data(client, points)
            km_m, min_m, delay_m = road["km"], road["min"], road["delay_min"]
            adj = build_graph(km_m, neighbours)
            edges = sorted({tuple(sorted((i, j))) for i in adj for j in adj[i]})
            leg_min = lambda i, j: min(min_m[i][j], min_m[j][i])
            per_option = []
            for o in opts:
                weighted = {i: {j: transport.edge_cost(o, adj[i][j], leg_min(i, j), qty) for j in adj[i]} for i in adj}
                cost, prev = dijkstra(weighted, 0)
                per_option.append((o, cost, prev))
            for idx, row in enumerate(nearby, start=1):
                gross = round(row["modal_price"]*qty/100, 2)   # money in hand = price x quantity
                choices = []
                for o, cost, prev in per_option:
                    path = path_to(prev, idx)
                    legs = list(zip(path, path[1:]))
                    km = sum(adj[a][b] for a, b in legs)
                    mins = sum(leg_min(a, b) for a, b in legs)
                    delay = sum(delay_m[a][b] for a, b in legs)
                    total = transport.trip_cost(o, cost[idx], km, qty)
                    if total is None:
                        continue
                    choices.append({"option_id": o["id"], "provider": o["provider"], "kind": o["kind"], "vehicle": o["vehicle"],
                        "vehicle_label": o["vehicle_label"], "trips": o["trips"], "note": o["note"],
                        "transport_inr": round(total), "keep_inr": round(gross - total), "distance_km": round(km, 1),
                        "minutes": round(mins), "traffic_delay_min": round(delay), "route": [names[p] for p in path], "route_nodes": path,
                        "breakdown": transport.breakdown(o, km, mins, qty)})
                if not choices:
                    continue
                choices.sort(key=lambda c: c["keep_inr"], reverse=True)
                b = choices[0]
                results.append({"market_id":row["market_id"],"market":row["name"],"state":row["state"],"district":row["district"],"latitude":row["latitude"],"longitude":row["longitude"],
                    "quote_inr_per_quintal":row["modal_price"],"observed_on":row["observed_on"],"source":row["source"],"variety":row["variety"],
                    "distance_km":b["distance_km"],"distance_method":road["method"],"route":b["route"],"route_nodes":b["route_nodes"],
                    "gross_inr":gross,"estimated_transport_inr":b["transport_inr"],"estimated_net_inr":b["keep_inr"],
                    "best_option":b,"options":choices})
            results.sort(key=lambda r: r["estimated_net_inr"], reverse=True)
            if results:
                waypoints = [points[p] for p in results[0]["route_nodes"]]
                geometry = await traffic_route(client, waypoints)
                if geometry is None and road["method"] == "road_route":
                    line = await route_geometry(client, waypoints)
                    geometry = {"line": line} if line else None
    best = results[0] if results else None
    graph = {"nodes":[{"id":i,"name":names[i],"latitude":p[0],"longitude":p[1]} for i,p in enumerate(points)] if nearby else [],
        "edges":[[i,j,round(adj[i][j],1)] for i,j in edges] if nearby else [],
        "best_route_geometry":(geometry or {}).get("line"),"best_route_traffic":geometry if geometry and "traffic_delay_min" in geometry else None,
        "live_traffic":road["traffic"]}
    summary_language = item.language if item.language in MESSAGES else "en-IN"
    summary = None
    if best:
        q = int(qty) if qty == int(qty) else qty
        label = CROP_LABELS.get(summary_language, {}).get(crop) or CROP_LABELS["en-IN"].get(crop, crop)
        o = best["best_option"]
        hindi = summary_language == "hi-IN"
        if o["kind"] == "own" and summary_language in OWN_MESSAGES:
            v = transport.OWN_VEHICLES[o["vehicle"]]
            summary = OWN_MESSAGES[summary_language].format(vehicle=v["hi"] if hindi else v["label"].lower(), market=best["market"], net=inr(best["estimated_net_inr"]), qty=q, crop=label, date=best["observed_on"])
        elif summary_language in TRANSPORT_MESSAGES:
            vname = transport.VEHICLES[o["vehicle"]]["hi" if hindi else "label"] if o["vehicle"] in transport.VEHICLES else ("साझा ट्रक" if hindi else "shared truck")
            summary = TRANSPORT_MESSAGES[summary_language].format(provider=o["provider"], vehicle=vname, market=best["market"], net=inr(best["estimated_net_inr"]), qty=q, crop=label, date=best["observed_on"])
        else:
            summary = MESSAGES[summary_language].format(qty=q,crop=label,market=best["market"],net=inr(best["estimated_net_inr"]),date=best["observed_on"])
    rec = transport.recommended_vehicle(qty)
    return {"commodity":crop,"quantity_kg":qty,"ranking":results,"best":best,"spoken_summary":summary,"summary_language":summary_language,
        "transport":{"own_vehicle":item.own_vehicle,"recommended_vehicle":rec,"recommended_label":transport.VEHICLES[rec]["label"],
                     "options":[{"id":o["id"],"provider":o["provider"],"vehicle_label":o["vehicle_label"],"kind":o["kind"],"trips":o["trips"]} for o in opts]},
        "graph":graph,"price_source":price_source,"farmer_state":farmer_state,"live":live_status,"updated_at":now(),
        "disclaimer":"Mandi prices are published quotes, not buyer offers. Transport costs are estimates from transporter rate cards or fuel use; confirm the fare before booking.",
        "no_quotes":not bool(best)}

@app.get("/v1/transport/catalog")
def transport_catalog(quantity_kg: float = 500):
    rec = transport.recommended_vehicle(quantity_kg)
    return {"recommended_vehicle": rec, "vehicles": transport.VEHICLES, "own_vehicles": transport.OWN_VEHICLES,
            "transporters": {k: v["note"] for k, v in transport.RATE_CARDS.items()}, "live_traffic": bool(tomtom_key())}

_tiles = {}

@app.get("/v1/traffic/{z}/{x}/{y}.png", include_in_schema=False)
async def traffic_tile(z: int, x: int, y: int):
    """Live traffic flow tiles, relayed so the TomTom key never reaches the browser."""
    if not tomtom_key() or not (0 <= z <= 18):
        raise HTTPException(404, "Live traffic not configured")
    loop_now = asyncio.get_running_loop().time()
    hit = _tiles.get((z, x, y))
    if hit and hit[0] > loop_now:
        return Response(hit[1], media_type="image/png")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TOMTOM}/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png", params={"key": tomtom_key()}, timeout=10)
    if r.status_code != 200:
        raise HTTPException(502, "Traffic tile unavailable")
    if len(_tiles) > 2000:
        _tiles.clear()
    _tiles[(z, x, y)] = (loop_now + 120, r.content)
    return Response(r.content, media_type="image/png")

@app.post("/v1/dispatch-requests", status_code=201)
def dispatch(item: DispatchIn):
    identifier = str(uuid.uuid4())
    with connect() as db:
        if not db.execute("SELECT 1 FROM markets WHERE id=?", (item.market_id,)).fetchone():
            raise HTTPException(404, "Market not found")
        db.execute("INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?)", (identifier,item.market_id,crop_name(item.commodity),item.quantity_kg,item.contact,"pending_manual_match",now(),item.note))
    return {"id":identifier,"status":"pending_manual_match","message":"Request saved. No vehicle has been booked."}

@app.get("/v1/admin/dispatch-requests")
def dispatches(x_admin_token: str | None = Header(default=None)):
    admin(x_admin_token)
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM dispatches ORDER BY created_at DESC")]

@app.post("/v1/voice/transcribe")
async def transcribe(file: UploadFile = File(...), language: str = Form(default="indic-auto")):
    if language != "indic-auto" and language not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    if not file.filename or not file.filename.lower().endswith((".wav",".mp3",".ogg",".flac",".aac",".m4a")):
        raise HTTPException(400, "Unsupported audio format")
    audio = await file.read(10_000_001)
    if len(audio) > 10_000_000:
        raise HTTPException(413, "Audio too large; Prisma REST supports at most 30 seconds")
    result = await upstream("POST", f"{GNANI}/stt/v3", headers=gnani_key(), data={"model":"prisma-v3.0","language_code":language,"format":"transcribe","topic":"Indian agricultural commodity and mandi market price enquiry","keyterms":KEYTERMS}, files={"audio_file":(file.filename,audio,file.content_type or "application/octet-stream")})
    try:
        data = result.json()
        transcript = data["transcript"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Invalid Gnani transcription response") from exc
    return {"transcript":transcript,"language":data.get("detected_language_code"),"model":data.get("model"),"intent":parse_intent(transcript),"reply":transport.parse_reply(transcript)}

@app.post("/v1/voice/speak")
async def speak(item: SpeakIn):
    if item.language not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    if item.voice not in {"Avinash","Shivani","Lakshmi","Kalpana","Shubham","Prakash","Devansh","Yashraj","Chetana","Vasudha","Riyaa","Tarika","Harshad"}:
        raise HTTPException(400, "Unsupported Timbre v3.0 voice")
    payload = {"text":item.text,"model":"timbre-v3.0","voice":item.voice,"language":item.language,"audio_config":{"encoding":"linear_pcm","container":"wav","num_channels":1,"sample_rate":24000,"sample_width":2}}
    result = await upstream("POST",f"{GNANI}/api/v1/tts/inference",headers=gnani_key(),json=payload)
    return Response(result.content,media_type="audio/wav")
