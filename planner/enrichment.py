"""Conservative, offline identity matching against an OSM fuel extract.

A nearby station of the same brand is NOT sufficient evidence. Accept an exact
store reference + brand, or an exact distinctive name, within 25 miles of the
listed city. Ambiguous and conflicting references are excluded for review.
"""

import re
from collections import Counter

import numpy as np

from planner.data import normalize
from planner.geo import segment_miles

BRANDS = {
    "loves": ("love",),
    "pilot": ("pilot",),
    "flyingj": ("flyingj",),
    "circlek": ("circlek",),
    "kwiktrip": ("kwiktrip",),
    "kwikstar": ("kwikstar",),
    "speedway": ("speedway",),
    "caseys": ("casey",),
    "quiktrip": ("quiktrip",),
    "7eleven": ("7eleven", "seveneleven"),
    "maverik": ("maverik",),
    "racetrac": ("racetrac",),
    "roadranger": ("roadranger",),
    "sheetz": ("sheetz",),
    "kumandgo": ("kumgo", "kumandgo"),
    "allsups": ("allsup",),
    "quarles": ("quarles",),
    "petro": (
        "petrostopping",
        "petrotravel",
    ),
    "ta": ("travelcentersofamerica",),
    "one9": ("one9",),
    "rutter": ("rutter",),
    "thorntons": ("thornton",),
    "cefco": ("cefco",),
    "stripes": ("stripes",),
}
GENERIC = {"shell", "bp", "chevron", "exxon", "mobil", "sunoco", "marathon", "citgo", "valero", "gas", "fuel"}


def brand(text):
    key = normalize(text)
    for family, variants in BRANDS.items():
        if any(v in key for v in variants):
            return family
    if re.match(r"^TA(?:\s|$)", text.upper()):
        return "ta"
    if text.upper().strip() == "PETRO":
        return "petro"
    return None


def store_ref(text):
    found = re.search(r"#\s*0*(\d+)\b", text)
    return str(int(found[1])) if found else None


def osm_ref(tags):
    explicit = tags.get("ref") or tags.get("ref:store") or tags.get("branch")
    if explicit and re.fullmatch(r"#?\s*\d+", explicit):
        return str(int(explicit.strip().lstrip("#").strip()))
    website = tags.get("website", "")
    found = re.fullmatch(r"https?://(?:www\.)?loves\.com/locations/(\d+)/?", website)
    if found and brand(tags.get("brand", tags.get("name", ""))) == "loves":
        return str(int(found[1]))
    return store_ref(tags.get("name", ""))


def distance(a, b):
    return float(segment_miles(np.array([[a["longitude"], a["latitude"]], [b["longitude"], b["latitude"]]]))[0])


def street_key(value):
    words = re.findall(r"[a-z0-9]+", value.lower())
    aliases = {
        "street": "st",
        "road": "rd",
        "avenue": "ave",
        "boulevard": "blvd",
        "drive": "dr",
        "highway": "hwy",
        "north": "n",
        "south": "s",
        "east": "e",
        "west": "w",
        "lane": "ln",
    }
    # Exit descriptions are useful context, but are not exact street addresses.
    if not words or not words[0].isdigit() or "exit" in words:
        return None
    return " ".join(aliases.get(w, w) for w in words)


def match_stations(stations, elements, source_date="unknown"):
    by_identity, by_name, by_address = {}, {}, {}
    for element in elements:
        tags = element.get("tags", {})
        if tags.get("amenity") != "fuel" or tags.get("disused") == "yes" or tags.get("access") == "private":
            continue
        point = element.get("center", element)
        if "lat" not in point or "lon" not in point:
            continue
        item = {
            "latitude": point["lat"],
            "longitude": point["lon"],
            "source": f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
            "source_name": tags.get("name", ""),
            "source_date": element.get("source_date", source_date),
            "source_ref": osm_ref(tags),
            "source_state": tags.get("addr:state", ""),
            "source_city": tags.get("addr:city", ""),
            "source_address": " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
        }
        families = {brand(tags.get(key, "")) for key in ("name", "brand", "operator")} - {None}
        for family in families:
            if item["source_ref"]:
                by_identity.setdefault((family, item["source_ref"]), []).append(item)
            address = street_key(item["source_address"])
            if address:
                by_address.setdefault((family, address), []).append(item)
        name = normalize(tags.get("name", ""))
        if name and name not in GENERIC and not families:
            by_name.setdefault(name, []).append(item)
    matched, report = [], []
    for station in stations:
        family, reference = brand(station["name"]), store_ref(station["name"])
        method = "brand_store_ref" if family and reference else "distinctive_name"
        possibilities = (
            by_identity.get((family, reference), [])
            if family and reference
            else by_name.get(normalize(station["name"]), [])
        )
        if not possibilities and family and street_key(station.get("address", "")):
            possibilities = by_address.get((family, street_key(station["address"])), [])
            possibilities = [
                p for p in possibilities if not reference or not p["source_ref"] or p["source_ref"] == reference
            ]
            method = "brand_street_address"
        if method == "distinctive_name":
            possibilities = [
                p
                for p in possibilities
                if (p["source_city"] and normalize(p["source_city"]) == normalize(station["city"]))
                or (
                    street_key(p["source_address"])
                    and street_key(p["source_address"]) == street_key(station.get("address", ""))
                )
            ]
        nearby = [
            p
            for p in possibilities
            if distance(station, p) <= 25 and (not p["source_state"] or p["source_state"].upper() == station["state"])
        ]
        # Multiple OSM objects can describe the same forecourt; require them all
        # to be within 100 m. Two separate nearby branches remain ambiguous.
        if nearby and all(distance(a, b) <= 0.06214 for a in nearby for b in nearby):
            chosen = min(nearby, key=lambda p: ("/node/" not in p["source"], p["source"]))
            matched.append(
                {
                    "id": station["id"],
                    **chosen,
                    "precision": "osm_identity_match",
                    "match_method": method,
                    "confidence": "high" if method != "distinctive_name" else "moderate",
                    "city_distance_miles": round(distance(station, chosen), 3),
                }
            )
            status = "matched"
        else:
            status = "ambiguous" if nearby else "unmatched"
        report.append(
            {
                "id": station["id"],
                "name": station["name"],
                "city": station["city"],
                "state": station["state"],
                "status": status,
            }
        )
    # Never silently assign two OPIS IDs to one OSM facility.
    uses = Counter(row["source"] for row in matched)
    conflicts = {row["id"] for row in matched if uses[row["source"]] > 1}
    for row in report:
        if row["id"] in conflicts:
            row["status"] = "conflicting_station_ids"
    return [row for row in matched if row["id"] not in conflicts], report
