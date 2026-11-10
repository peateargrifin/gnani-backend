from datetime import date

import httpx
import pytest

from app import live
from app.core import connect, build_graph, dijkstra, ingest, init_db, parse_intent, path_to, seed_demo
from app.main import app, inr

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path/"test.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "test-only")
    monkeypatch.setenv("OSRM_BASE_URL", "http://127.0.0.1:1")
    # Never let a real key from .env trigger a paid Gnani call in tests.
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    monkeypatch.setenv("LIVE_PRICES", "0")
    init_db()
    live.init_cache()

@pytest.mark.asyncio
async def test_live_feed_geocodes_and_ingests(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key")
    today = date.today().strftime("%d/%m/%Y")
    def handler(request):
        if "data.gov.in" in request.url.host:
            assert request.url.params["filters[commodity]"] == "Potato"
            return httpx.Response(200, json={"records": [
                {"state": "Uttar Pradesh", "district": "Hathras", "market": "Haathras APMC", "commodity": "Potato", "variety": "Other", "arrival_date": today, "min_price": 500, "max_price": 520, "modal_price": 510}]})
        return httpx.Response(200, json=[{"lat": "27.59", "lon": "78.05", "address": {"state": "Uttar Pradesh"}}])
    monkeypatch.setattr(live, "_last_geo_call", 0.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await live.refresh(client, "potato", "Uttar Pradesh")
    assert result["accepted"] == 1 and result["markets_still_locating"] == 0
    with connect() as db:
        row = db.execute("SELECT m.name, m.latitude, q.modal_price FROM quotes q JOIN markets m ON m.id=q.market_id").fetchone()
    assert tuple(row) == ("Haathras APMC", 27.59, 510)

@pytest.mark.asyncio
async def test_compare_math_dates_and_dispatch(monkeypatch):
    async def fake_matrix(client, points):
        return [[0 if a == b else 55 for b in points] for a in points], "road_route"
    monkeypatch.setattr("app.main.road_matrix", fake_matrix)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        token={"X-Admin-Token":"test-only"}
        for market,lon,price in [("Near",77.0,2000),("Far",77.5,2500)]:
            response=await client.post("/v1/admin/markets",headers=token,json={"name":market,"state":"Karnataka","district":"Test","latitude":12.0,"longitude":lon})
            assert response.status_code==200
            assert ingest([{"market":market,"state":"Karnataka","district":"Test","commodity":"Potato","modal_price":price,"arrival_date":date.today().isoformat()}],"test")["accepted"]==1
        response=await client.post("/v1/compare",json={"commodity":"potato","quantity_kg":500,"latitude":12.0,"longitude":77.0,"radius_km":100,"transport_rate_per_km":10,"transport_base_fee":100,"language":"kn-IN"})
        assert response.status_code==200
        result=response.json()
        assert len(result["ranking"])==2
        assert result["best"]["market"]=="Far"
        assert result["best"]["gross_inr"]==12500
        assert result["best"]["distance_method"]=="road_route"
        assert "₹" in result["spoken_summary"] and "ಆಲೂಗಡ್ಡೆ" in result["spoken_summary"]
        request=await client.post("/v1/dispatch-requests",json={"market_id":result["best"]["market_id"],"commodity":"potato","quantity_kg":500,"contact":"DEMO-FARMER-001"})
        assert request.status_code==201
        assert request.json()["status"]=="pending_manual_match"
        assert (await client.get("/v1/admin/dispatch-requests",headers=token)).json()[0]["id"]==request.json()["id"]

def test_intent_and_no_fake_quotes():
    assert parse_intent("मेरे 500 किलो आलू कहाँ बेचूँ?")=={"commodity":"potato","quantity_kg":500.0,"needs_clarification":False}
    assert parse_intent("sugarcane 2 tons")["quantity_kg"]==2000
    assert ingest([{"market":"Unknown","state":"XX","district":"YY","commodity":"potato","modal_price":2000,"arrival_date":date.today().isoformat()}],"test")["skipped"]==1

def test_hinglish_units_and_indian_numbers():
    assert parse_intent("5 quintal pyaz ka best bhav kahan milega?")=={"commodity":"onion","quantity_kg":500.0,"needs_clarification":False}
    assert parse_intent("mere paas 800 kilo aloo hai")["commodity"]=="potato"
    assert parse_intent("1,00,000 kg tamatar")["quantity_kg"]==100000
    assert parse_intent("ನನ್ನ 800 ಕೆಜಿ ಟೊಮೆಟೊ")=={"commodity":"tomato","quantity_kg":800.0,"needs_clarification":False}
    assert parse_intent("what is the value of my crop")["commodity"] is None  # "alu" inside "value" must not match
    assert inr(125000)=="1,25,000" and inr(999)=="999"

@pytest.mark.asyncio
async def test_demo_seed_gives_ranking_with_fallback_distance():
    assert seed_demo()["accepted"] > 20
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        result=(await client.post("/v1/compare",json={"commodity":"aloo","quantity_kg":500,"latitude":27.10,"longitude":78.15,"language":"hi-IN"})).json()
        assert len(result["ranking"]) >= 5
        assert result["ranking"][0]["distance_method"]=="straight_line_estimate"  # OSRM unreachable in tests
        assert "आलू" in result["spoken_summary"]
        assert (await client.get("/")).status_code==200

def test_dijkstra_takes_cheaper_multi_hop_route():
    # 0=farm, 1=town, 2=far mandi. Direct farm->mandi is a long detour (100 km); via town is 30+40.
    adj = {0: {1: 30, 2: 100}, 1: {0: 30, 2: 40}, 2: {0: 100, 1: 40}}
    dist, prev = dijkstra(adj, 0)
    assert dist[2] == 70 and path_to(prev, 2) == [0, 1, 2]
    # Sparse graph with 1 neighbour each must still connect every node to the farm.
    matrix = [[0, 10, 50, 52], [10, 0, 45, 47], [50, 45, 0, 3], [52, 47, 3, 0]]
    dist, _ = dijkstra(build_graph(matrix, neighbours=1), 0)
    assert set(dist) == {0, 1, 2, 3}

@pytest.mark.asyncio
async def test_voice_requires_server_secret_and_admin_guard():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.post("/v1/admin/markets",json={"name":"Fake","state":"XX","district":"YY","latitude":0,"longitude":0})).status_code==403
        response=await client.post("/v1/voice/speak",json={"text":"Hello","language":"en-IN"})
        assert response.status_code==503
