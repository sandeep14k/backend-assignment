"""Offline datasets: US city coordinates and the supplied fuel-price CSV.

Everything here is loaded once per process (see ``config/wsgi.py``), so request
handling never geocodes over the network.
"""
import csv
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import numpy as np
from django.conf import settings

# Generous bounding boxes for the contiguous US, Alaska and Hawaii.
US_BOUNDS = [
    (24.0, 49.6, -125.0, -66.5),
    (51.0, 71.6, -180.0, -129.0),
    (18.5, 22.5, -160.5, -154.5),
]
LOCATION_HELP = "Use 'City, ST' (e.g. 'Chicago, IL'), 'City, State' or 'latitude, longitude'."


ABBREVIATIONS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}


def normalize(value):
    """Case/punctuation-insensitive city key; 'St. Louis' and 'Saint Louis' match."""
    return "".join(ABBREVIATIONS.get(word, word) for word in re.findall(r"[a-z0-9]+", value.lower()))


def _state_key(value):
    return re.sub(r"[^a-z]", "", value.lower())


@lru_cache(maxsize=1)
def cities():
    result = {}
    with (settings.BASE_DIR / "data/us_cities.csv").open(encoding="utf-8") as source:
        for row in csv.DictReader(source):
            result.setdefault((normalize(row["CITY"]), row["STATE_CODE"]), row)
    return result


@lru_cache(maxsize=1)
def state_codes():
    """Map normalized state codes and full names ('il', 'illinois') to 'IL'."""
    codes = {}
    for row in cities().values():
        codes[_state_key(row["STATE_CODE"])] = row["STATE_CODE"]
        codes[_state_key(row["STATE_NAME"])] = row["STATE_CODE"]
    return codes


def in_us(latitude, longitude):
    return any(s <= latitude <= n and w <= longitude <= e for s, n, w, e in US_BOUNDS)


def _coordinates(first, second):
    try:
        latitude, longitude = float(first), float(second)
    except ValueError:
        return None
    if not in_us(latitude, longitude):
        raise ValueError(f"Coordinates {latitude}, {longitude} are not within the USA.")
    return {"name": f"{latitude:.5f}, {longitude:.5f}", "latitude": latitude, "longitude": longitude}


def location(value):
    """Resolve 'City, ST', 'City, State Name' or 'lat, lon' without network calls."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Start and finish are required. {LOCATION_HELP}")
    if len(value) > 150 or "," not in value:
        raise ValueError(f"Could not read location '{value[:150]}'. {LOCATION_HELP}")
    first, second = (part.strip() for part in value.rsplit(",", 1))
    point = _coordinates(first, second)
    if point:
        return point
    state = state_codes().get(_state_key(second))
    row = cities().get((normalize(first), state)) if state else None
    if row is None:
        raise ValueError(f"Unknown US city: '{value}'. {LOCATION_HELP}")
    return {"name": f'{row["CITY"]}, {row["STATE_CODE"]}', "latitude": float(row["LATITUDE"]), "longitude": float(row["LONGITUDE"])}


def _price(text):
    try:
        price = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        return None
    return price if price.is_finite() and price > 0 else None


@lru_cache(maxsize=1)
def stations():
    """Return ``(stations, skipped_count)`` from the supplied fuel-price CSV.

    Stations are placed at their city's coordinates unless verified pump
    coordinates are supplied in ``data/station_coordinates.csv``. A truck stop
    listed more than once keeps its lowest price.
    """
    overrides = {}
    path = settings.BASE_DIR / "data/station_coordinates.csv"
    if path.exists():
        with path.open(encoding="utf-8") as source:
            overrides = {r["id"].strip(): (float(r["latitude"]), float(r["longitude"])) for r in csv.DictReader(source)}

    by_id, unlocated = {}, set()
    with (settings.BASE_DIR / "fuel-prices-for-be-assessment.csv").open(encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            station_id = row["OPIS Truckstop ID"].strip()
            price = _price(row["Retail Price"])
            if price is None:
                continue
            if station_id in by_id:
                if price < Decimal(by_id[station_id]["price_per_gallon"]):
                    by_id[station_id]["price_per_gallon"] = str(price)
                continue
            city_name, state = row["City"].strip(), row["State"].strip().upper()
            city = cities().get((normalize(city_name), state))
            if station_id in overrides:
                (latitude, longitude), precision = overrides[station_id], "verified"
            elif city:
                latitude, longitude = float(city["LATITUDE"]), float(city["LONGITUDE"])
                precision = "city_centroid"
            else:
                unlocated.add(station_id)  # Mostly Canadian rows; not on US routes.
                continue
            by_id[station_id] = {
                "id": station_id,
                "name": row["Truckstop Name"].strip(),
                "address": row["Address"].strip(),
                "city": city_name,
                "state": state,
                "latitude": latitude,
                "longitude": longitude,
                "price_per_gallon": str(price),
                "coordinate_precision": precision,
            }
    return list(by_id.values()), len(unlocated)


@lru_cache(maxsize=1)
def station_points():
    """Station [lon, lat] array aligned with ``stations()[0]``."""
    return np.array([[s["longitude"], s["latitude"]] for s in stations()[0]], dtype=float)


def warm():
    """Load all datasets so the first request does not pay the ~70 ms cost."""
    state_codes()
    station_points()
