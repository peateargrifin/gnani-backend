from datetime import date

import httpx
import pytest

from app import live
from app.core import connect, build_graph, dijkstra, ingest, init_db, parse_intent, path_to
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
    monkeypatch.setattr(live, "_rows", {})
    monkeypatch.setattr(live, "_jobs", {})
    monkeypatch.setattr(live, "_feed_down_until", 0.0)
    monkeypatch.setattr(live, "_last_geo_call", 0.0)

@pytest.mark.asyncio
async def test_live_feed_pulls_whole_state_and_locates_mandis(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "test-key")
    today = date.today().strftime("%d/%m/%Y")
    row = {"state": "Uttar Pradesh", "district": "Hathras", "market": "Haathras APMC", "variety": "Other", "arrival_date": today}
    feed_calls = []
    def handler(request):
        if "data.gov.in" in request.url.host:
            feed_calls.append(request.url.params["filters[state.keyword]"])
            assert "filters[commodity]" not in request.url.params  # one pull covers every crop
            return httpx.Response(200, json={"records": [
                {**row, "commodity": "Potato", "min_price": 500, "max_price": 520, "modal_price": 510},
                {**row, "commodity": "Wheat", "min_price": 2400, "max_price": 2500, "modal_price": 2450}]})
        if "reverse" in request.url.path:
            return httpx.Response(200, json={"address": {"state": "Uttar Pradesh", "state_district": "Agra District"}})
        return httpx.Response(200, json=[{"lat": "27.59", "lon": "78.05", "address": {"state": "Uttar Pradesh"}}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await live.place_of(client, 27.1, 78.15) == ("Uttar Pradesh", "Agra")
        records, _ = await live.state_rows(client, "Uttar Pradesh")
        for ident in live._market_ids(records):
            await live._locate(client, ident)
        await live.state_rows(client, "Uttar Pradesh")  # cached: no second feed call
    ingest(records, "data.gov.in")
    assert feed_calls == ["Uttar Pradesh"]
    with connect() as db:
        rows = {r[0]: r[1] for r in db.execute("SELECT q.commodity, q.modal_price FROM quotes q JOIN markets m ON m.id=q.market_id WHERE m.latitude=27.59")}
    assert rows == {"potato": 510, "wheat": 2450}

AGG_PAGE = """<table><tr><th>Market</th><th>Variety</th><th>Price</th><th>Date</th></tr><tr><td>Agra</td><td>Other</td><td>₹5/Kg</td><td>10 Nov</td></tr></table>
<table><tr><th>Commodity</th><th>Arrival Date</th><th>Variety</th><th>State</th><th>District</th><th>Market</th><th>Min Price</th><th>Max Price</th><th>Avg price</th><th>Mobile App</th></tr>
<tr><td>Potato</td><td>10/11/2026</td><td>Other</td><td>Uttar Pradesh</td><td>Agra</td><td>Agra</td><td>Rs 500 / Quintal</td><td>Rs 600 / Quintal</td><td>Rs 511.63 / Quintal</td><td><a>Get Free Alert</a></td></tr>
<tr><td>Potato</td><td>09/11/2026</td><td>Other</td><td>Uttar Pradesh</td><td>Agra</td><td>Agra</td><td>Rs 450 / Quintal</td><td>Rs 550 / Quintal</td><td>Rs 1,480 / Quintal</td><td></td></tr></table>
<a href="https://www.commodityonline.com/mandiprices/potato/uttar-pradesh/aligarh">x</a>"""

@pytest.mark.asyncio
async def test_aggregator_parses_quotes_and_keeps_latest_day(monkeypatch):
    from app import aggregator
    monkeypatch.setattr(aggregator, "_cache", {})
    monkeypatch.setattr(aggregator, "PAUSE_SECONDS", 0)
    rows = aggregator.parse(AGG_PAGE)
    assert len(rows) == 2 and rows[1]["modal_price"] == 1480.0
    assert aggregator.market_links(AGG_PAGE, "potato", "uttar-pradesh") == ["aligarh"]
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=AGG_PAGE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records, _ = await aggregator.rows(client, "paddy", "Uttar Pradesh")
    assert urls == ["https://www.commodityonline.com/mandiprices/paddy-dhan-common/uttar-pradesh"]
    assert [(r["market"], r["arrival_date"], r["modal_price"]) for r in records] == [("Agra", "10/11/2026", 511.63)]

def test_conversation_replies_and_vehicle_choice():
    from app.transport import parse_reply, recommended_vehicle
    assert parse_reply("nahi mam")["no"] and not parse_reply("nahi mam")["yes"]
    assert parse_reply("नहीं है")["no"]
    assert parse_reply("haan ji")["yes"]
    assert parse_reply("mere paas tractor hai")["own_vehicle"] == "tractor"
    assert parse_reply("chhota hathi hai")["own_vehicle"] == "tata_ace"
    assert recommended_vehicle(15) == "two_wheeler" and recommended_vehicle(500) == "three_wheeler" and recommended_vehicle(2000) == "tata_407"

MB_STATE = """<table><tr><th>Commodity</th><th>Potato</th></tr></table>
<table><tr><th>Date</th><th>Districts</th><th>Markets</th><th>Commodity</th><th>Variety/Grade</th><th>Min Price</th><th>Max Price</th><th>Modal Price</th></tr>
<tr><td>10/11/2026</td><td>Sambhal</td><td>Sambhal</td><td>Potato</td><td>Other FAQ</td><td>₹ 700</td><td>₹ 700</td><td>₹ 700</td></tr></table>
<a href="/en/commodity/aalu/uttar-pradesh/agra">Agra</a><a href="/en/commodity/aalu/uttar-pradesh/mathura">Mathura</a>"""
MB_DISTRICT = """<table><tr><th>Date</th><th>Markets</th><th>Commodity</th><th>Variety/Grade</th><th>Min Price</th><th>Max Price</th><th>Modal Price</th></tr>
<tr><td>10/11/2026</td><td>Agra</td><td>Potato</td><td>Other FAQ</td><td>₹ 500</td><td>₹ 600</td><td>₹ 512</td></tr>
<tr><td>09/11/2026</td><td>Agra</td><td>Potato</td><td>Other FAQ</td><td>₹ 500</td><td>₹ 500</td><td>₹ 1,500</td></tr></table>"""

@pytest.mark.asyncio
async def test_mandibhav_reads_state_and_farmers_district(monkeypatch):
    from app import mandibhav
    monkeypatch.setattr(mandibhav, "_cache", {})
    monkeypatch.setattr(mandibhav, "PAUSE_SECONDS", 0)
    urls = []
    def handler(request):
        urls.append(request.url.path)
        return httpx.Response(200, text=MB_DISTRICT if request.url.path.endswith("/agra") else MB_STATE)
    async def locate(client, district, state):
        return None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records, _ = await mandibhav.rows(client, "potato", "Uttar Pradesh", near=(27.1, 78.1, 150, "Agra", locate))
    assert urls == ["/en/commodity/aalu/uttar-pradesh", "/en/commodity/aalu/uttar-pradesh/agra"]
    got = sorted((r["market"], r["district"], r["arrival_date"], r["modal_price"]) for r in records)
    assert got == [("Agra", "Agra", "10/11/2026", 512.0), ("Sambhal", "Sambhal", "10/11/2026", 700.0)]

def test_processed_products_are_not_raw_crops():
    from app.core import crop_name
    for product in ["Sweet Potato", "Mustard Oil", "Wheat Atta", "Black Gram Dal (Urd Dal)", "Coconut Oil", "Onion Green"]:
        assert crop_name(product) == product.casefold(), product

def test_state_spellings_match_between_osm_and_feed():
    assert live.norm_state("Uttrakhand") == live.norm_state("Uttarakhand")
    assert live.canonical_state("NCT of Delhi") == "Delhi"

def test_more_crops_and_agmarknet_names():
    from app.core import crop_name
    cases = {"Paddy(Dhan)(Common)": "paddy", "Bengal Gram(Gram)(Whole)": "chana", "Arhar (Tur/Red Gram)(Whole)": "tur",
             "Green Gram (Moong)(Whole)": "moong", "Black Gram (Urd Beans)(Whole)": "urad", "Cummin Seed(Jeera)": "cumin",
             "Bhindi(Ladies Finger)": "bhindi", "मूंगफली": "groundnut", "मूंग": "moong", "patta gobhi": "cabbage",
             "phool gobhi": "cauliflower", "Custard Apple (Sharifa)": "custard_apple", "Dry Chillies": "dry chillies"}
    for text, crop in cases.items():
        assert crop_name(text) == crop, text
    assert parse_intent("10 quintal gehu kahan bechu")=={"commodity":"wheat","quantity_kg":1000.0,"needs_clarification":False}

@pytest.mark.asyncio
async def test_compare_math_dates_and_dispatch(monkeypatch):
    async def fake_road(client, points):
        same = lambda a, b: 0 if a == b else 55
        return {"km": [[same(a, b) for b in points] for a in points], "min": [[same(a, b) for b in points] for a in points],
                "delay_min": [[0] * len(points) for _ in points], "method": "road_route", "traffic": False}
    monkeypatch.setattr("app.main.road_data", fake_road)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        token={"X-Admin-Token":"test-only"}
        for market,lon,price in [("Near",77.0,2000),("Far",77.5,2500)]:
            response=await client.post("/v1/admin/markets",headers=token,json={"name":market,"state":"Karnataka","district":"Test","latitude":12.0,"longitude":lon})
            assert response.status_code==200
            assert ingest([{"market":market,"state":"Karnataka","district":"Test","commodity":"Potato","modal_price":price,"arrival_date":date.today().isoformat()}],"test")["accepted"]==1
        response=await client.post("/v1/compare",json={"commodity":"potato","quantity_kg":500,"latitude":12.0,"longitude":77.0,"radius_km":100,"language":"kn-IN"})
        assert response.status_code==200
        result=response.json()
        assert len(result["ranking"])==2
        assert result["transport"]["recommended_vehicle"]=="three_wheeler"
        best=result["best"]
        assert best["market"]=="Far" and best["gross_inr"]==12500 and best["distance_method"]=="road_route"
        # Porter 3-wheeler, 55 km / 55 min: 200 base + 18/km + 1/min = 1245; beats WheelsEye minimum fare 1500
        assert best["best_option"]["option_id"]=="Porter:three_wheeler" and best["estimated_transport_inr"]==1245
        assert {o["provider"] for o in best["options"]}=={"Porter","WheelsEye","Delhivery"}
        assert "₹" in result["spoken_summary"] and "ಆಲೂಗಡ್ಡೆ" in result["spoken_summary"]
        # Own tractor: diesel for 110 km round trip at 4 km/l is costly, so the near mandi wins
        own=(await client.post("/v1/compare",json={"commodity":"potato","quantity_kg":500,"latitude":12.0,"longitude":77.0,"radius_km":100,"language":"hi-IN","own_vehicle":"tractor"})).json()
        assert own["best"]["market"]=="Near" and own["best"]["best_option"]["kind"]=="own"
        assert "ट्रैक्टर" in own["spoken_summary"]
        request=await client.post("/v1/dispatch-requests",json={"market_id":result["best"]["market_id"],"commodity":"potato","quantity_kg":500,"contact":"FARMER-001"})
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
async def test_page_has_no_demo_wording_and_locate_endpoint(monkeypatch):
    async def fake_find(client, query):
        return {"lat": 27.0, "lon": 78.3, "label": "Fatehabad, Agra, Uttar Pradesh"} if "fatehabad" in query.casefold() else None
    monkeypatch.setattr(live, "find_place", fake_find)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        page = (await client.get("/")).text
        assert "demo" not in page.casefold() and "dijkstra" not in page.casefold()
        assert (await client.post("/v1/locate",json={"query":"Fatehabad"})).json()["label"].startswith("Fatehabad")
        assert (await client.post("/v1/locate",json={"query":"Nowhere"})).status_code==404

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
