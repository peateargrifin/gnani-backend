"""Transport options and their costs.

Every option turns a road leg (km, minutes) into rupees, so each option gives the
same mandi graph its own edge weights. Transporter prices are ESTIMATES from rate
cards (base fare + per km + per minute + minimum fare). Porter, WheelsEye and
Delhivery do not publish live-quote APIs; replace a card with an API call here
once business API access is available. All numbers can be edited in RATE_CARDS
or overridden with environment variables for fuel prices.
"""
import math
import os
import re

# Vehicle classes: payload in kg, and how they are named in the reply.
VEHICLES = {
    "two_wheeler": {"label": "2-wheeler", "hi": "दोपहिया", "cap": 20},
    "three_wheeler": {"label": "3-wheeler tempo", "hi": "तिपहिया टेम्पो", "cap": 500},
    "tata_ace": {"label": "Tata Ace (mini truck)", "hi": "टाटा ऐस छोटा हाथी", "cap": 750},
    "pickup": {"label": "Pickup 8 ft", "hi": "पिकअप", "cap": 1250},
    "tata_407": {"label": "Tata 407 (14 ft)", "hi": "टाटा 407", "cap": 2500},
    "truck_17ft": {"label": "17 ft truck", "hi": "17 फ़ुट ट्रक", "cap": 5000},
    "truck_22ft": {"label": "22 ft truck", "hi": "22 फ़ुट ट्रक", "cap": 9000},
}

# Transporter rate cards (INR). base = booking fare, per_km/per_min = distance and
# time charges (time makes traffic matter), min_fare = minimum trip charge,
# max_km = typical service range. Tolls are added separately.
RATE_CARDS = {
    "Porter": {
        "note": "Intra/near-city goods transport. Estimate from fare structure; confirm the fare in the Porter app.",
        "vehicles": {
            "two_wheeler": {"base": 60, "per_km": 9, "per_min": 0.5, "max_km": 40},
            "three_wheeler": {"base": 200, "per_km": 18, "per_min": 1.0, "max_km": 60},
            "tata_ace": {"base": 350, "per_km": 22, "per_min": 1.5, "max_km": 150},
            "pickup": {"base": 450, "per_km": 26, "per_min": 1.5, "max_km": 150},
            "tata_407": {"base": 1000, "per_km": 34, "per_min": 2.0, "max_km": 300},
        },
    },
    "WheelsEye": {
        "note": "Intercity full-truck loads. Estimate from per-km trip pricing with a minimum fare; confirm with WheelsEye.",
        "vehicles": {
            "tata_ace": {"base": 0, "per_km": 20, "per_min": 0, "min_fare": 1500, "max_km": 1500},
            "tata_407": {"base": 0, "per_km": 28, "per_min": 0, "min_fare": 2500, "max_km": 2000},
            "truck_17ft": {"base": 0, "per_km": 36, "per_min": 0, "min_fare": 4000, "max_km": 2500},
            "truck_22ft": {"base": 0, "per_km": 48, "per_min": 0, "min_fare": 6000, "max_km": 3000},
        },
    },
    "Delhivery": {
        "note": "Part-truck-load (shared truck), charged per kg. Estimate; confirm with Delhivery freight.",
        "per_kg": {"per_kg": 3.0, "per_kg_km": 0.012, "min_fare": 750, "max_km": 3000, "min_kg": 50},
    },
}

# Farmer's own vehicle: payload, fuel, mileage (km/l) and per-trip compliance cost
# (goods permit, fitness, insurance and road tax spread over trips; estimate).
OWN_VEHICLES = {
    "bike": {"label": "Bike", "hi": "बाइक", "cap": 60, "fuel": "petrol", "kmpl": 45, "compliance": 0, "toll_class": "none"},
    "car": {"label": "Car", "hi": "कार", "cap": 250, "fuel": "petrol", "kmpl": 14, "compliance": 0, "toll_class": "car"},
    "tractor": {"label": "Tractor-trolley", "hi": "ट्रैक्टर-ट्रॉली", "cap": 3000, "fuel": "diesel", "kmpl": 4, "compliance": 60, "toll_class": "lcv"},
    "three_wheeler": {"label": "3-wheeler tempo", "hi": "टेम्पो", "cap": 500, "fuel": "diesel", "kmpl": 25, "compliance": 120, "toll_class": "none"},
    "tata_ace": {"label": "Tata Ace / chhota haathi", "hi": "छोटा हाथी", "cap": 750, "fuel": "diesel", "kmpl": 18, "compliance": 180, "toll_class": "lcv"},
    "pickup": {"label": "Pickup / Bolero", "hi": "पिकअप", "cap": 1250, "fuel": "diesel", "kmpl": 13, "compliance": 220, "toll_class": "lcv"},
    "truck": {"label": "Truck", "hi": "ट्रक", "cap": 5000, "fuel": "diesel", "kmpl": 6, "compliance": 450, "toll_class": "truck"},
}
VEHICLE_TOLL_CLASS = {"two_wheeler": "none", "three_wheeler": "none", "tata_ace": "lcv", "pickup": "lcv", "tata_407": "lcv",
                      "truck_17ft": "truck", "truck_22ft": "truck", "per_kg": "truck"}
# Rough toll per km on tolled highways, and the share of a trip on them.
TOLL_PER_KM = {"none": 0.0, "car": 1.6, "lcv": 2.6, "truck": 5.5}

def fuel_price(fuel):
    return float(os.getenv("DIESEL_PRICE" if fuel == "diesel" else "PETROL_PRICE", "88" if fuel == "diesel" else "95"))

def tolled_share(km):
    return 0.0 if km < 25 else 0.35 if km < 80 else 0.55

def recommended_vehicle(qty_kg):
    """Smallest vehicle class that carries the load in one trip."""
    for vid, v in VEHICLES.items():
        if v["cap"] >= qty_kg:
            return vid
    return "truck_22ft"

def options(qty_kg, own_vehicle=None):
    """Transport options for a load. With own_vehicle set, only that vehicle;
    otherwise the cheapest-fitting vehicle classes at every transporter."""
    out = []
    if own_vehicle:
        v = OWN_VEHICLES[own_vehicle]
        out.append({"id": f"own:{own_vehicle}", "kind": "own", "provider": "Own vehicle", "vehicle": own_vehicle,
                    "vehicle_label": v["label"], "trips": math.ceil(qty_kg / v["cap"]), "card": v,
                    "note": "Fuel for the round trip, tolls and per-trip permit/insurance costs. Check local RTO rules for carrying goods."})
        return out
    for provider, card in RATE_CARDS.items():
        if "per_kg" in card:
            pk = card["per_kg"]
            if qty_kg >= pk["min_kg"]:
                out.append({"id": f"{provider}:shared", "kind": "per_kg", "provider": provider, "vehicle": "per_kg",
                            "vehicle_label": "Shared truck (per kg)", "trips": 1, "card": pk, "note": card["note"]})
            continue
        # every class that fits in one trip, plus the biggest one with several trips
        fits = [vid for vid in card["vehicles"] if VEHICLES[vid]["cap"] >= qty_kg]
        chosen = fits[:2] or [max(card["vehicles"], key=lambda vid: VEHICLES[vid]["cap"])]
        for vid in chosen:
            out.append({"id": f"{provider}:{vid}", "kind": "transporter", "provider": provider, "vehicle": vid,
                        "vehicle_label": VEHICLES[vid]["label"], "trips": math.ceil(qty_kg / VEHICLES[vid]["cap"]),
                        "card": card["vehicles"][vid], "note": card["note"]})
    return out

def edge_cost(opt, km, minutes, qty_kg):
    """Rupees for one road leg under this option: this is the option's edge weight."""
    c = opt["card"]
    if opt["kind"] == "own":
        fuel = 2 * km / c["kmpl"] * fuel_price(c["fuel"])  # there and back
        toll = 2 * km * tolled_share(km) * TOLL_PER_KM[c["toll_class"]]
        return opt["trips"] * (fuel + toll)
    toll = km * tolled_share(km) * TOLL_PER_KM[VEHICLE_TOLL_CLASS[opt["vehicle"]]]
    if opt["kind"] == "per_kg":
        return qty_kg * c["per_kg_km"] * km + toll
    return opt["trips"] * (c["per_km"] * km + c["per_min"] * minutes + toll)

def trip_cost(opt, path_cost, total_km, qty_kg):
    """Fixed charges added once per trip, and minimum fares."""
    c = opt["card"]
    if total_km > c.get("max_km", 1e9):
        return None  # outside this transporter's service range
    if opt["kind"] == "own":
        return path_cost + opt["trips"] * c["compliance"]
    if opt["kind"] == "per_kg":
        return max(c["min_fare"], qty_kg * c["per_kg"] + path_cost)
    return max(opt["trips"] * c.get("min_fare", 0), opt["trips"] * c["base"] + path_cost)

def breakdown(opt, km, minutes, qty_kg):
    c = opt["card"]
    if opt["kind"] == "own":
        litres = 2 * km / c["kmpl"] * opt["trips"]
        return {"fuel_litres": round(litres, 1), "fuel_inr": round(litres * fuel_price(c["fuel"])),
                "toll_inr": round(2 * km * tolled_share(km) * TOLL_PER_KM[c["toll_class"]] * opt["trips"]),
                "permit_insurance_inr": opt["trips"] * c["compliance"]}
    return {"toll_inr": round(km * tolled_share(km) * TOLL_PER_KM[VEHICLE_TOLL_CLASS[opt["vehicle"]]] * opt["trips"])}

# ---------- understanding the farmer's replies ----------
YES = r"\b(yes|yeah|haan|han|ha|haa|ji haan|hai|hain|ho|houdu|aamaa|avunu)\b|हाँ|हां|है|ಹೌದು|ಇದೆ|ஆமா|ஆம்|இருக்கு|అవును"
NO = r"\b(no|nahi|nahin|nai|na|illa|illai|ledu)\b|नहीं|नही|ಇಲ್ಲ|இல்லை|లేదు"
OWN_ALIASES = {
    "bike": ["bike", "motorcycle", "scooter", "बाइक", "मोटरसाइकिल", "ಬೈಕ್", "பைக்"],
    "car": ["car", "gaadi", "कार", "ಕಾರು", "கார்"],
    "tractor": ["tractor", "trolley", "ट्रैक्टर", "ट्रॉली", "ಟ್ರ್ಯಾಕ್ಟರ್", "டிராக்டர்"],
    "tata_ace": ["tata ace", "chhota hathi", "chota hathi", "chhota haathi", "mini truck", "छोटा हाथी", "टाटा ऐस"],
    "pickup": ["pickup", "pick up", "bolero", "पिकअप", "बोलेरो"],
    "three_wheeler": ["tempo", "3 wheeler", "three wheeler", "auto", "टेम्पो", "ऑटो", "ಟೆಂಪೋ", "டெம்போ"],
    "truck": ["truck", "lorry", "ट्रक", "ಲಾರಿ", "லாரி"],
}

def parse_reply(text):
    """Yes/no and a vehicle type from a short spoken answer."""
    t = (text or "").casefold()
    vehicle = next((v for v, names in OWN_ALIASES.items() if any((re.search(rf"\b{re.escape(n)}\b", t) if n.isascii() else n in t) for n in names)), None)
    no = re.search(NO, t) is not None
    yes = re.search(YES, t) is not None and not no
    return {"yes": yes or bool(vehicle), "no": no and not vehicle, "own_vehicle": vehicle}
