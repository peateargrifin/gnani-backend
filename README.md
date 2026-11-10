# Mandi Saathi — backend

A farmer-facing mandi comparison API with Indian-language speech. Gnani Prisma v3.0 transcribes an audio question; deterministic extraction identifies a supported commodity and quantity; published quotes are ranked by estimated net proceeds; Gnani Timbre v3.0 reads the result aloud. The API also saves freight requests for a dispatcher to match manually.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set environment variables using your own secret manager or export them in the shell.
uvicorn app.main:app --reload
```

`/docs` is the live OpenAPI reference. `.env` is ignored by git and loaded locally at startup. Set `GNANI_API_KEY` server-side before voice use and `ADMIN_TOKEN` before imports. Configure `ALLOWED_ORIGINS` for the frontend. Do not place keys in frontend code, URLs, git, or browser requests.

### Live mandi prices

With `LIVE_PRICES=1` and a free `DATA_GOV_API_KEY` (data.gov.in → sign up → My Account), every comparison:

1. Reverse-geocodes the farm to its state and district (OpenStreetMap Nominatim, max 1 request/second, cached).
2. Pulls today's AGMARKNET quotes for **all commodities in that state** from data.gov.in. The server caches this for `FEED_TTL_SECONDS` (default 300). The page auto-refreshes every 5 seconds from that cache, so the government API is never hammered.
3. Locates each mandi in the feed: the farmer's district first, then the rest of the state, then mandis in neighbouring-state districts within the search radius. Coordinates are cached in SQLite, so each mandi is located once. Results grow while this runs, and the 5-second refresh shows them.
4. Builds the road graph (OSRM, cached), runs Dijkstra, and ranks by price × quantity − transport.

**No data.gov.in key?** The app then reads the same AGMARKNET daily quotes from the public aggregator **commodityonline.com** ([app/aggregator.py](app/aggregator.py)). It reads the crop's state page for the latest quotes, then individual mandi pages in the background. It sends one request at a time with a 1.5 s pause, uses an honest User-Agent, keeps results cached for `FEED_TTL_SECONDS`, and only fetches pages robots.txt allows. The aggregator publishes an *average* price, which is used as the quote. Force a source with `PRICE_SOURCE=datagov` or `PRICE_SOURCE=commodityonline` (default `auto`).

If no source answers, the app says prices are unavailable rather than inventing any. About 49 commodities are recognised, using AGMARKNET names, Hindi, Hinglish, and some Kannada/Tamil/Telugu.

### Transport: own vehicle or transporters

The farmer is asked whether they have their own vehicle (by voice, with tap-able answers too).

- **No vehicle:** the load picks a vehicle class (e.g. 500 kg needs a 3-wheeler tempo; 750 kg a Tata Ace). The app then compares Porter, WheelsEye and Delhivery options. Each transporter has a rate card in [app/transport.py](app/transport.py): base fare, per-km and per-minute charges, minimum fare, service range, plus estimated tolls. **These are estimates, not live quotes.** None of these companies publishes a public quote API, so the rate card is where a business API would plug in.
- **Own vehicle** (bike, car, tractor-trolley, tempo, Tata Ace, pickup, truck): round-trip fuel (mileage × `DIESEL_PRICE`/`PETROL_PRICE`), estimated tolls, and a per-trip share of permit, fitness and insurance costs. Local RTO rules for carrying goods should be checked.

Routing: the farm and the mandis form one physical road graph. Each transport option puts its own rupee cost on every link (per-km, per-minute and toll charges), runs Dijkstra from the farm, then adds fixed fares and minimums. The result is a matrix of *you keep = price × quantity − transport* for every mandi × option, and the best cell wins. With `TOMTOM_API_KEY`, farm-to-mandi legs use live traffic travel times, and the map shows a live traffic layer (relayed through the server, so the key never reaches the browser). Without it, travel times are OSRM free-flow estimates.

### Quick start (Windows)

1. Put your key in `.env`: `GNANI_API_KEY=your-key-here`. Keep `LIVE_PRICES=1`.
2. `pip install -r requirements.txt`
3. `python -m uvicorn app.main:app --port 8000`
4. Open http://localhost:8000. Tap the mic, speak (e.g. "मेरे 500 किलो आलू कहाँ बेचूँ?"), tap again to stop.

The web app lives in `app/static/index.html` and is served at `/` by the same server, so it needs no extra setup.

## Frontend contract

1. `GET /v1/languages`, `GET /v1/commodities`, `GET /v1/markets` for selectors and map markers.
2. `POST /v1/voice/transcribe` multipart `file` (WAV/MP3/OGG/FLAC/AAC/M4A, at most 30 seconds), optional `language` (`indic-auto` or a listed locale). Response includes `transcript`, detected `language`, and `intent` with commodity, quantity in kilograms, and a clarification flag. The frontend asks for missing fields and allows correction before comparison.
3. `POST /v1/compare` JSON: `{"commodity":"potato","quantity_kg":500,"latitude":12.97,"longitude":77.59,"radius_km":150,"language":"hi-IN"}`. Response includes sorted `ranking`, `best`, and `spoken_summary`. Call `POST /v1/voice/speak` with `{ "text": spoken_summary, "language": "hi-IN", "voice": "Shivani" }` to receive `audio/wav`.
4. `POST /v1/dispatch-requests` with `market_id`, `commodity`, `quantity_kg`, `contact`, optional `note`. Response is `pending_manual_match`; an operator can see requests via `GET /v1/admin/dispatch-requests` with `X-Admin-Token`.

## Data and pricing

Quotes are **INR per quintal**, as published by the source. `gross = modal_price × quantity_kg / 100`. `estimated_net = gross − (base_fee + road_distance_km × rate_per_km)`. Request fields `transport_base_fee` and `transport_rate_per_km` override deployment defaults; show them alongside the result. Road distance uses OSRM when reachable; an explicit `straight_line_estimate` flag marks fallback distances. This is a comparison, not a binding buyer offer or true arbitrage calculation. Prices older than seven days are excluded by default; use `max_age_days` for another threshold. Empty quotes yield an empty ranking, never invented prices.

`POST /v1/admin/markets` adds a market with verified coordinates and name/state/district. `POST /v1/admin/quotes/import` accepts a CSV/JSON upload with `market,state,district,commodity,modal_price,arrival_date` and optional `variety,min_price,max_price`. Date is ISO `YYYY-MM-DD` or `DD/MM/YYYY`. Quotes whose market identity does not match the coordinate registry are skipped. `POST /v1/admin/quotes/sync` pages through an official data.gov.in resource when `DATA_GOV_RESOURCE_URL` (restricted to `https://api.data.gov.in/resource/…`) and `DATA_GOV_API_KEY` are configured. The caller must choose the **AGMARKNET daily prices** resource URL in their account; resource availability, fields, updates, and quotas vary. Both write endpoints require `X-Admin-Token`. Import is idempotent per market, commodity, variety, day, and source. The `source` and `observed_on` fields travel with every recommendation.

The market coordinate registry is required because AGMARKNET quote rows do not reliably include latitude/longitude. Add the markets relevant to the pilot before syncing. For production, use a managed routing provider, a scheduled incremental data ingestion process, a database with concurrent writes, verified buyer quotations, and a contracted logistics provider. The public OSRM demo server is a development convenience; do not depend on it for production traffic. A dispatch request is never represented as an actual booked vehicle.

## Scope and limits

- Prisma REST handles short clips only. The live microphone WebSocket protocol is documented by Gnani but is not exposed through this backend; the browser can record short utterances for the REST flow.
- Timbre v3.0 supports English, Hindi, Kannada, Tamil, Telugu, Malayalam, Marathi, Punjabi, Bengali and Gujarati. Spoken comparison phrasing is hand-written for English, Hindi, Kannada and Tamil; other languages currently fall back to English text while TTS accepts their locale. Expand reviewed translations before presenting those locales as fully localized.
- Voice cloning is intentionally not in the default farmer journey: it requires an authorized speaker recording and a consent/storage policy. It does not improve mandi price accuracy. The buildathon's useful model usage is Prisma v3.0 and Timbre v3.0.
- Commodity aliases cover potato, sugarcane, onion and tomato in a subset of languages; typed input accepts other commodity names if the quote database contains them. Confirm voice intent before calculation.
- No telephony, SMS, real vehicle booking, buyer matching, or buyer payment integration has been connected.

## Tests

```bash
pip install pytest-asyncio
pytest -q
```

No paid Gnani call occurs in tests. Docs: [Prisma v3 REST](https://docs.gnani.ai/api/STT/speech-to-text), [Timbre v3 REST](https://docs.gnani.ai/api/TTS/tts-inference), [Gnani documentation index](https://docs.gnani.ai/llms.txt).
