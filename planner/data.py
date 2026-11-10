"""Offline datasets: US city coordinates and the supplied fuel-price CSV.

Everything here is loaded once per process (see ``config/wsgi.py``), so request
handling never geocodes over the network.
"""

import csv
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from planner.geo import to_xyz

# Generous bounding boxes for the contiguous US, Alaska and Hawaii.
US_BOUNDS = [
    (24.0, 49.6, -125.0, -66.5),
    (51.0, 71.6, -180.0, -129.0),
    (18.5, 22.5, -160.5, -154.5),
]
CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
LOCATION_HELP = "Use 'City, ST' (e.g. 'Chicago, IL'), 'City, State' or 'latitude, longitude'."
ABBREVIATIONS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}

# The city file has one row per (city, county). For cities whose rows sit far
# apart, pick the county that holds the city itself.
PRIMARY_COUNTY = {
    ("Atlanta", "GA"): "Fulton",
    ("Austin", "TX"): "Travis",
    ("Cincinnati", "OH"): "Hamilton",
    ("Houston", "TX"): "Harris",
    ("Indianapolis", "IN"): "Marion",
    ("Jacksonville", "FL"): "Duval",
    ("Kansas City", "MO"): "Jackson",
    ("Kissimmee", "FL"): "Osceola",
    ("Omaha", "NE"): "Douglas",
    ("Orlando", "FL"): "Orange",
    ("San Antonio", "TX"): "Bexar",
    ("Trenton", "NJ"): "Mercer",
}
# Fuel-price CSV towns missing from the city file, mapped to the adjacent listed city.
CITY_ALIASES = {
    ("Port Wentworth", "GA"): "Savannah",
    ("University Park", "IL"): "Park Forest",
    ("Henrico", "VA"): "Richmond",
    ("Elizabethport", "NJ"): "Elizabeth",
    ("Evergreen", "AL"): "Castleberry",
}


def normalize(value):
    """Case/punctuation-insensitive city key; 'St. Louis' and 'Saint Louis' match."""
    return "".join(ABBREVIATIONS.get(word, word) for word in re.findall(r"[a-z0-9]+", value.lower()))


def _state_key(value):
    return re.sub(r"[^a-z]", "", value.lower())


def _choose_row(rows):
    """Pick one row for a city listed under several counties."""
    city, state = rows[0]["CITY"], rows[0]["STATE_CODE"]
    preferred = PRIMARY_COUNTY.get((city, state))
    own_county = [r for r in rows if r["COUNTY"] == preferred or r["COUNTY"].removesuffix(" City") == city]
    if own_county:
        return own_county[0]
    if len(rows) >= 3:  # The row closest to all others (medoid).

        def spread(row):
            return sum(
                abs(float(row["LATITUDE"]) - float(o["LATITUDE"]))
                + abs(float(row["LONGITUDE"]) - float(o["LONGITUDE"]))
                for o in rows
            )

        return min(rows, key=spread)
    return rows[-1]


@lru_cache(maxsize=1)
def cities():
    grouped = {}
    with (settings.BASE_DIR / "data/us_cities.csv").open(encoding="utf-8") as source:
        for row in csv.DictReader(source):
            grouped.setdefault((normalize(row["CITY"]), row["STATE_CODE"]), []).append(row)
    return {key: _choose_row(rows) for key, rows in grouped.items()}


def find_city(name, state):
    """Look up a city row by name and two-letter state code, or None."""
    key = normalize(name)
    for (alias, alias_state), target in CITY_ALIASES.items():
        if alias_state == state and normalize(alias) == key:
            key = normalize(target)
    return cities().get((key, state))


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
    return {
        "name": f"{latitude:.5f}, {longitude:.5f}",
        "latitude": latitude,
        "longitude": longitude,
    }


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
    row = find_city(first, state) if state else None
    if row is None:
        raise ValueError(f"Unknown US city: '{value}'. {LOCATION_HELP}")
    return {
        "name": f"{row['CITY']}, {row['STATE_CODE']}",
        "latitude": float(row["LATITUDE"]),
        "longitude": float(row["LONGITUDE"]),
    }


def _price(text):
    try:
        price = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        return None
    return price if price.is_finite() and price > 0 else None


@lru_cache(maxsize=1)
def stations():
    """Return ``(stations, unlocated_count)`` from the supplied fuel-price CSV.

    Stations are placed at their city's coordinates unless verified pump
    coordinates are supplied in ``data/station_coordinates.csv``. Some truck
    stops are listed several times with different prices; they get the mean.
    """
    overrides = {}
    path = settings.BASE_DIR / "data/station_coordinates.csv"
    if path.exists():
        with path.open(encoding="utf-8") as source:
            overrides = {r["id"].strip(): (float(r["latitude"]), float(r["longitude"])) for r in csv.DictReader(source)}

    by_id, prices, unlocated = {}, {}, set()
    with (settings.BASE_DIR / "fuel-prices-for-be-assessment.csv").open(encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            station_id = row["OPIS Truckstop ID"].strip()
            price = _price(row["Retail Price"])
            if price is None or station_id in unlocated:
                continue
            if station_id in by_id:
                prices[station_id].append(price)
                continue
            city_name, state = row["City"].strip(), row["State"].strip().upper()
            if state in CANADIAN_PROVINCES:  # Not reachable on a US route.
                unlocated.add(station_id)
                continue
            city = find_city(city_name, state)
            if station_id in overrides:
                (latitude, longitude), precision = overrides[station_id], "verified"
            elif city:
                latitude, longitude = float(city["LATITUDE"]), float(city["LONGITUDE"])
                precision = "city_centroid"
            else:
                unlocated.add(station_id)  # US town missing from the city file (none at present).
                continue
            prices[station_id] = [price]
            by_id[station_id] = {
                "id": station_id,
                "name": row["Truckstop Name"].strip(),
                "address": row["Address"].strip(),
                "city": city_name,
                "state": state,
                "latitude": latitude,
                "longitude": longitude,
                "coordinate_precision": precision,
            }
    for station_id, station in by_id.items():
        listed = prices[station_id]
        station["price_per_gallon"] = str((sum(listed) / len(listed)).quantize(Decimal("0.00000001")))
    return list(by_id.values()), len(unlocated)


@dataclass(frozen=True)
class StationIndex:
    """Stations plus a KD-tree over their 3-D positions, for corridor and nearest queries."""

    stations: list
    xyz: np.ndarray
    tree: cKDTree
    prices: list

    @classmethod
    def build(cls, stations):
        xyz = to_xyz([[s["longitude"], s["latitude"]] for s in stations]) if stations else np.empty((0, 3))
        return cls(stations, xyz, cKDTree(xyz), [Decimal(s["price_per_gallon"]) for s in stations])


@lru_cache(maxsize=1)
def station_index():
    return StationIndex.build(stations()[0])


def warm():
    """Load datasets and build the station KD-tree at startup (~100 ms), not on the first request."""
    state_codes()
    station_index()
