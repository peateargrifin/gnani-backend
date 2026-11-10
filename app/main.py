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

from . import live
from .core import CROP_LABELS, LANGUAGES, MESSAGES, build_graph, connect, crop_name, dijkstra, distance_km, ingest, init_db, now, parse_intent, path_to, read_rows, road_matrix, route_geometry, seed_demo

load_dotenv()
GNANI = "https://api.vachana.ai"
STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app):
    init_db()
    live.init_cache()
    if os.getenv("DEMO_DATA", "0").lower() in ("1", "true", "yes"):
        seed_demo()
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
    transport_rate_per_km: float | None = Field(default=None, ge=0)
    transport_base_fee: float | None = Field(default=None, ge=0)
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

class IntentIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)

@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "index.html")

@app.get("/health")
def health():
    return {"status":"ok", "gnani_configured":bool(os.getenv("GNANI_API_KEY")), "live_prices":live.enabled(),
        "data_gov_key":"own" if os.getenv("DATA_GOV_API_KEY") else "public_sample_10_rows"}

@app.get("/v1/languages")
def languages():
    return LANGUAGES

@app.get("/v1/commodities")
def commodities():
    return ["potato", "sugarcane", "onion", "tomato"]

@app.get("/v1/markets")
def markets():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM markets ORDER BY state,district,name")]

@app.post("/v1/intent")
def intent(item: IntentIn):
    return {"transcript": item.text, "intent": parse_intent(item.text)}

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

@app.post("/v1/compare")
async def compare(item: CompareIn):
    crop = crop_name(item.commodity)
    if item.language not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    live_status, farmer_state = None, None
    if live.enabled():
        async with httpx.AsyncClient() as client:
            farmer_state = await live.state_of(client, item.latitude, item.longitude)
            live_status = await live.refresh(client, crop, farmer_state)
    today = date.today()
    def load(include_demo):
        with connect() as db:
            return [dict(row) for row in db.execute(f"""SELECT q.*,m.name,m.state,m.district,m.latitude,m.longitude FROM quotes q JOIN markets m ON m.id=q.market_id
                WHERE q.commodity=? AND q.observed_on>=? AND q.observed_on<=? {"" if include_demo else "AND q.source<>'demo_synthetic'"}
                ORDER BY q.observed_on DESC,q.id DESC""",
                (crop, date.fromordinal(today.toordinal()-item.max_age_days).isoformat(), today.isoformat()))]
    price_source = "data.gov.in (AGMARKNET, live)" if live.enabled() else "demo_synthetic"
    rows = load(include_demo=not live.enabled())
    if live.enabled() and not rows:
        rows = load(include_demo=True)  # live feed gave nothing: fall back to clearly labelled demo prices
        if rows:
            price_source = "demo_synthetic (live feed unavailable)"
    # Latest quote per market, stable across duplicate varieties/sources.
    latest = {}
    for row in rows:
        latest.setdefault(row["market_id"], row)
    nearby = [r for r in latest.values() if distance_km(item.latitude,item.longitude,r["latitude"],r["longitude"]) <= item.radius_km]
    rate = item.transport_rate_per_km if item.transport_rate_per_km is not None else float(os.getenv("TRANSPORT_RATE_PER_KM","25"))
    base = item.transport_base_fee if item.transport_base_fee is not None else float(os.getenv("TRANSPORT_BASE_FEE","300"))
    neighbours = int(os.getenv("GRAPH_NEIGHBOURS", "4"))
    # Graph: node 0 = farm, nodes 1..n = mandis. Edge weight = road km.
    points = [(item.latitude,item.longitude)] + [(r["latitude"],r["longitude"]) for r in nearby]
    names = ["Your farm"] + [r["name"] for r in nearby]
    results, edges, geometry = [], [], None
    async with httpx.AsyncClient() as client:
        if nearby:
            matrix, method = await road_matrix(client, points)
            adj = build_graph(matrix, neighbours)
            dist, prev = dijkstra(adj, 0)
            edges = sorted({tuple(sorted((i, j))) for i in adj for j in adj[i]})
            for idx, row in enumerate(nearby, start=1):
                km = dist[idx]
                path = path_to(prev, idx)
                gross = round(row["modal_price"]*item.quantity_kg/100,2)   # money in hand = price x quantity
                freight = round(base + km*rate,2)                          # cost of the shortest road path
                results.append({"market_id":row["market_id"],"market":row["name"],"state":row["state"],"district":row["district"],"latitude":row["latitude"],"longitude":row["longitude"],
                    "quote_inr_per_quintal":row["modal_price"],"observed_on":row["observed_on"],"source":row["source"],"variety":row["variety"],
                    "distance_km":round(km,1),"distance_method":method,"route":[names[p] for p in path],"route_nodes":path,
                    "gross_inr":gross,"estimated_transport_inr":freight,"estimated_net_inr":round(gross-freight,2)})
            results.sort(key=lambda r:r["estimated_net_inr"],reverse=True)
            if method == "road_route":
                geometry = await route_geometry(client, [points[p] for p in results[0]["route_nodes"]])
    best = results[0] if results else None
    graph = {"algorithm":"dijkstra","objective":"max(price x quantity - (base_fee + rate x shortest_km))","neighbours_per_node":neighbours,
        "nodes":[{"id":i,"name":names[i],"latitude":p[0],"longitude":p[1]} for i,p in enumerate(points)] if nearby else [],
        "edges":[[i,j,round(adj[i][j],1)] for i,j in edges] if nearby else [],"best_route_geometry":geometry}
    summary_language = item.language if item.language in MESSAGES else "en-IN"
    summary = None
    if best:
        qty = int(item.quantity_kg) if item.quantity_kg == int(item.quantity_kg) else item.quantity_kg
        label = CROP_LABELS.get(summary_language, {}).get(crop, crop)
        summary = MESSAGES[summary_language].format(qty=qty,crop=label,market=best["market"],net=inr(best["estimated_net_inr"]),date=best["observed_on"])
    return {"commodity":crop,"quantity_kg":item.quantity_kg,"ranking":results,"best":best,"spoken_summary":summary,"summary_language":summary_language,
        "transport":{"rate_per_km":rate,"base_fee":base},"graph":graph,
        "price_source":price_source,"farmer_state":farmer_state,"live":live_status,
        "disclaimer":"Published mandi modal prices, not buyer offers. Transport is an estimate; fees, quality, availability, and actual sale price are excluded.","no_quotes":not bool(best)}

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
    result = await upstream("POST", f"{GNANI}/stt/v3", headers=gnani_key(), data={"model":"prisma-v3.0","language_code":language,"format":"transcribe","topic":"Indian agricultural commodity and mandi market price enquiry","keyterms":"potato,sugarcane,onion,tomato,mandi,quintal,मंडी,आलू,गन्ना,प्याज,टमाटर,किलो,क्विंटल"}, files={"audio_file":(file.filename,audio,file.content_type or "application/octet-stream")})
    try:
        data = result.json()
        transcript = data["transcript"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Invalid Gnani transcription response") from exc
    return {"transcript":transcript,"language":data.get("detected_language_code"),"model":data.get("model"),"intent":parse_intent(transcript)}

@app.post("/v1/voice/speak")
async def speak(item: SpeakIn):
    if item.language not in LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    if item.voice not in {"Avinash","Shivani","Lakshmi","Kalpana","Shubham","Prakash","Devansh","Yashraj","Chetana","Vasudha","Riyaa","Tarika","Harshad"}:
        raise HTTPException(400, "Unsupported Timbre v3.0 voice")
    payload = {"text":item.text,"model":"timbre-v3.0","voice":item.voice,"language":item.language,"audio_config":{"encoding":"linear_pcm","container":"wav","num_channels":1,"sample_rate":24000,"sample_width":2}}
    result = await upstream("POST",f"{GNANI}/api/v1/tts/inference",headers=gnani_key(),json=payload)
    return Response(result.content,media_type="audio/wav")
