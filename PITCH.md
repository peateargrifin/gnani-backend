# Mandi Saathi

**A voice agent that tells a farmer where to sell, and how to get there, for the most money in hand.**

Live app: [gnani-backend.vercel.app](https://gnani-backend.vercel.app) · Pitch page: [gnani-backend.vercel.app/pitch](https://gnani-backend.vercel.app/pitch) · Code: [github.com/peateargrifin/gnani-backend](https://github.com/peateargrifin/gnani-backend)

## The problem

A smallholder with 500 kg of potatoes usually sells at the nearest mandi, or to whoever is buying. Prices for the same crop differ widely between mandis a few hours apart, but the farmer can't easily compare them, and the comparison only matters **after transport**: a higher price 100 km away can leave less money than a lower one nearby. The data exists (AGMARKNET publishes daily mandi prices), but it sits in English web tables, not in a form a farmer can ask a question of in their own language.

## The solution

> **Farmer:** "मेरे 500 किलो आलू कहाँ बेचूँ?"
> **Saathi:** "क्या आपके पास अपना वाहन है?"
> **Farmer:** "नहीं।"
> **Saathi:** "500 किलो के लिए तिपहिया टेम्पो चाहिए होगा… Porter के टेम्पो से Shamsabad मंडी ले जाने पर सबसे ज़्यादा, लगभग ₹14,937 बचेंगे।"

🎙️ Speech → text (**Gnani Prisma v3.0**) → crop + quantity (49 crops; Hindi, Hinglish, Kannada, Tamil) → today's mandi prices (AGMARKNET, near the farm) → road graph × transport options (cheapest route per option) → 🔊 answer aloud (**Gnani Timbre v3.0**)

## How the answer is computed

- The farm and nearby mandis form one road graph, using real road distances and travel times.
- Each transport option, such as a Porter 3-wheeler, a WheelsEye Tata Ace, a Delhivery shared truck or the farmer's own tractor, puts its **own rupee cost** on every road link.
- For each option, the cheapest route is found. Then **you keep = price × quantity − transport** is compared across every mandi × option, and the best combination wins.
- Own vehicle: round-trip fuel, tolls, and a per-trip share of permit and insurance costs.

## Who it is for

- Smallholder farmers selling a few quintals at a time, on a basic smartphone, who are more comfortable speaking than typing.
- FPOs and village agents who advise many farmers.

## Gnani models used

- **Prisma v3.0** speech-to-text: language auto-detect, plus mandi and crop key terms for accuracy.
- **Timbre v3.0** text-to-speech: every question and answer is spoken (voice Shivani).

## What is real, and what is estimated

**Real:** today's AGMARKNET mandi prices (via public aggregators, or data.gov.in with a key), mandi locations, road distances and the farmer's location.

**Estimated:** transporter fares come from rate cards (Porter, WheelsEye and Delhivery have no public quote APIs), and tolls and permit costs are averages. The app labels these and asks the farmer to confirm before booking. A truck request is saved for a dispatcher to match manually; no vehicle is booked automatically.

## Next steps

Transporter business APIs for live quotes · live traffic (supported, needs a TomTom key) · a phone-call (IVR) version for feature phones · price alerts when a nearby mandi pays more.

---

Built for the Great Indian AI Internship Challenge · Voice by Gnani Prisma v3.0 and Timbre v3.0 · Prices: AGMARKNET · Maps and roads © OpenStreetMap contributors, OSRM
