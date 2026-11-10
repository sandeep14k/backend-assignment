import csv
import re
from functools import lru_cache
from pathlib import Path
from decimal import Decimal
from django.conf import settings


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


@lru_cache(maxsize=1)
def cities():
    result = {}
    with (settings.BASE_DIR / "data/us_cities.csv").open() as source:
        for row in csv.DictReader(source):
            result[(normalize(row["CITY"]), row["STATE_CODE"])] = row
    return result


def location(value):
    if not isinstance(value, str) or len(value) > 150 or "," not in value:
        raise ValueError("Locations must use 'City, ST', for example 'Chicago, IL'.")
    city, state = value.rsplit(",", 1)
    row = cities().get((normalize(city), state.strip().upper()))
    if row is None:
        raise ValueError(f"Unknown US city/state: {value}")
    return {"name": f'{row["CITY"]}, {row["STATE_CODE"]}', "latitude": float(row["LATITUDE"]), "longitude": float(row["LONGITUDE"])}


@lru_cache(maxsize=1)
def stations():
    result, seen, skipped = [], set(), 0
    overrides = {}
    path = settings.BASE_DIR / "data/station_coordinates.csv"
    if path.exists():
        with path.open() as source:
            overrides = {r["id"]: (float(r["latitude"]), float(r["longitude"])) for r in csv.DictReader(source)}
    with (settings.BASE_DIR / "fuel-prices-for-be-assessment.csv").open(encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            station_id = row["OPIS Truckstop ID"]
            if station_id in seen:
                continue
            seen.add(station_id)
            city = cities().get((normalize(row["City"]), row["State"].strip().upper()))
            if station_id in overrides:
                lat, lon = overrides[station_id]
                precision = "verified"
            elif city:
                lat, lon = float(city["LATITUDE"]), float(city["LONGITUDE"])
                precision = "city_centroid"
            else:
                skipped += 1
                continue
            price = Decimal(row["Retail Price"])
            if not price.is_finite() or price <= 0:
                skipped += 1
                continue
            result.append({"id": station_id, "name": row["Truckstop Name"], "address": row["Address"], "city": row["City"], "state": row["State"], "latitude": lat, "longitude": lon, "price_per_gallon": str(price), "coordinate_precision": precision})
    return result, skipped
