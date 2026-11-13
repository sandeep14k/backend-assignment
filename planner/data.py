"""Offline datasets: US city coordinates and the supplied fuel-price CSV.

Everything here is loaded once per process (see ``config/wsgi.py``), so request
handling never geocodes over the network.
"""

import csv
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from planner.geo import Boundary, to_xyz

US_COASTAL_WATERS_MILES = 25  # Offshore points this close to US land are accepted (islands, piers, harbours).
CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
LOCATION_HELP = "Use 'City, ST' (e.g. 'Chicago, IL'), a street address, or 'latitude, longitude'."
COUNTRY_NAMES = {"usa", "us", "unitedstates", "unitedstatesofamerica", "america"}
INPUT_ALIASES = {"nyc": ("New York", "NY"), "newyorkcity": ("New York", "NY")}
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
    ("New York City", "NY"): "New York",
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


@lru_cache(maxsize=1)
def _boundaries():
    with (settings.BASE_DIR / "data/us_boundary.json").open(encoding="utf-8") as source:
        data = json.load(source)
    return Boundary(data["us"]), Boundary(data["neighbors"])


def in_us(latitude, longitude):
    """True inside US land (incl. territories), or within 25 miles offshore and not in another country.

    Uses Natural Earth 1:10m borders: every one of the 29,880 bundled US city
    coordinates except 8 border hamlets passes, and points in Canada, Mexico,
    Cuba, the Bahamas and the British Virgin Islands (e.g. Windsor, 1 mile from
    Detroit) are rejected.
    """
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    us, neighbors = _boundaries()
    if us.contains(longitude, latitude):
        return True
    if neighbors.contains(longitude, latitude):
        return False
    return us.distance_miles(longitude, latitude) <= US_COASTAL_WATERS_MILES


def _point(latitude, longitude, matched):
    if not in_us(latitude, longitude):
        raise ValueError(f"Coordinates {latitude}, {longitude} are not within the USA.")
    return {
        "name": f"{latitude:.5f}, {longitude:.5f}",
        "latitude": latitude,
        "longitude": longitude,
        "matched": matched,
    }


def _city_point(row):
    return {
        "name": f"{row['CITY']}, {row['STATE_CODE']}",
        "latitude": float(row["LATITUDE"]),
        "longitude": float(row["LONGITUDE"]),
        "matched": "city",
    }


@lru_cache(maxsize=1)
def _cities_by_name():
    """Normalized city name -> sorted state codes that have it (for state-less input and suggestions)."""
    names = {}
    for name, state in cities():
        names.setdefault(name, []).append(state)
    return {name: sorted(states) for name, states in names.items()}


def _suggest(name, state=None):
    """Up to three close spellings, e.g. 'Chicgo, IL' -> 'Chicago, IL'."""
    pool = [key for key in cities() if state is None or key[1] == state]
    close = difflib.get_close_matches(normalize(name), [key[0] for key in pool], n=3, cutoff=0.8)
    found = [cities()[key] for name_key in close for key in pool if key[0] == name_key]
    return [f"{row['CITY']}, {row['STATE_CODE']}" for row in found[:3]]


def _split_state(text):
    """'Austin TX' / 'Salt Lake City Utah' / 'Chicago, IL 60606' -> (city, 'ST'), or (text, None)."""
    words = text.replace(",", " ").split()
    for size in (3, 2, 1):  # Longest state name first ("new hampshire", "district of columbia").
        if len(words) > size:
            state = state_codes().get(_state_key(" ".join(words[-size:])))
            if state:
                return " ".join(words[:-size]), state
    return text, None


def _numbers(text):
    try:
        return [float(part) for part in re.split(r"[,\s]+", text.strip()) if part]
    except ValueError:
        return None


def location(value):
    """Resolve a US location offline (no network calls).

    Accepts 'City, ST', 'City, State', 'City ST', 'City, ST 60606', 'City, ST, USA',
    'lat, lon', and a city name alone when only one state has it. Extra leading
    parts ('233 S Wacker Dr, Chicago, IL') resolve to the city; street-level
    geocoding of such addresses happens in ``planner.geocoding``.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Start and finish are required. {LOCATION_HELP}")
    if len(value) > 200:
        raise ValueError("Location is too long (200 characters max).")

    numbers = _numbers(value)
    if numbers is not None and len(numbers) == 2:
        return _point(numbers[0], numbers[1], "coordinates")

    last = re.split(r"[,\s]+", value.strip())[-1].upper()
    if last == "CANADA" or last in CANADIAN_PROVINCES:
        raise ValueError(f"'{value}' is in Canada. Start and finish must be in the USA. {LOCATION_HELP}")

    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"Could not read location '{value}'. {LOCATION_HELP}")
    while len(parts) > 1 and _state_key(parts[-1]) in COUNTRY_NAMES:
        parts.pop()
    alias = INPUT_ALIASES.get(normalize(parts[0])) if len(parts) == 1 else None
    if alias:
        parts = list(alias)

    if len(parts) >= 2:
        city, state = parts[-2], state_codes().get(_state_key(parts[-1]))
        if state is None:  # e.g. 'Chicago IL, USA' left as one part plus noise
            city, state = _split_state(", ".join(parts))
    else:
        city, state = _split_state(parts[0])

    if state:
        row = find_city(city, state)
        if row is None:
            hint = _suggest(city, state)
            raise ValueError(
                f"Unknown US city: '{value}'."
                + (f" Did you mean {' or '.join(hint)}?" if hint else f" {LOCATION_HELP}")
            )
        return _city_point(row)

    states = _cities_by_name().get(normalize(city), [])
    if len(states) == 1:
        return _city_point(cities()[(normalize(city), states[0])])
    if states:
        options = "; ".join(f"{cities()[(normalize(city), st)]['CITY']}, {st}" for st in states[:6])
        raise ValueError(
            f"'{value}' exists in {len(states)} states ({options}{'; ...' if len(states) > 6 else ''}). "
            "Please add the state."
        )
    hint = _suggest(city)
    raise ValueError(
        f"Could not find '{value}' in the USA."
        + (f" Did you mean {' or '.join(hint)}?" if hint else f" {LOCATION_HELP}")
    )


def _price(text):
    try:
        price = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        return None
    return price if price.is_finite() and price > 0 else None


def _coordinate_file(name):
    path = settings.BASE_DIR / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        return {row["id"].strip(): row for row in csv.DictReader(source)}


@lru_cache(maxsize=1)
def stations():
    """Return ``(stations, unlocated_count)`` from the supplied fuel-price CSV.

    Each station's position, best source first: an identity match in
    ``data/station_coordinates.csv`` (OSM or operator), the OSM interstate exit
    its address names (``data/exit_coordinates.csv``, see ``planner.exits``),
    else its town's coordinates. ``coordinate_precision`` says which. An identity
    match more than 3 miles from the exit its own address names is overruled.
    Some truck stops are listed several times with different prices; they get the mean.
    """
    overrides = _coordinate_file("data/station_coordinates.csv")
    exits = _coordinate_file("data/exit_coordinates.csv")

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
            # An exit row exists for an identity-matched station only when the two disagree by
            # over 3 miles (see locate_exits): the store matched is then not the one listed.
            evidence = exits.get(station_id) or overrides.get(station_id)
            if evidence:
                latitude, longitude = float(evidence["latitude"]), float(evidence["longitude"])
                precision = "exit_match" if station_id in exits else evidence.get("precision") or "unverified"
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError(f"Invalid station coordinates: {station_id}")
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
                "coordinate_source": (evidence or {}).get("source", ""),
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
    fingerprint: str

    @classmethod
    def build(cls, stations):
        xyz = to_xyz([[s["longitude"], s["latitude"]] for s in stations]) if stations else np.empty((0, 3))
        fingerprint = hashlib.sha256(json.dumps(stations, sort_keys=True).encode()).hexdigest()[:24]
        return cls(stations, xyz, cKDTree(xyz), [Decimal(s["price_per_gallon"]) for s in stations], fingerprint)


SOURCED_PRECISIONS = {
    "osm_identity_match": "https://www.openstreetmap.org/",
    "operator_identity_match": "https://www.loves.com/",
}


def is_sourced(station):
    """True when the station's coordinates come from a cited OSM/operator match, not its city centre."""
    prefix = SOURCED_PRECISIONS.get(station.get("coordinate_precision"))
    return bool(prefix) and station.get("coordinate_source", "").startswith(prefix)


@lru_cache(maxsize=1)
def station_index():
    """Every priced US station is a candidate, so any US trip can be planned.

    Identity matches use their exact coordinates, exit matches the OSM exit
    named in their address ("exit_match"), and the rest their town's
    coordinates ("city_centroid"). The optimizer charges estimated detour fuel
    for every stop. A position is never labelled more precise than its source.
    """
    return StationIndex.build(
        [s for s in stations()[0] if is_sourced(s) or s["coordinate_precision"] in {"exit_match", "city_centroid"}]
    )


def warm():
    """Load datasets and build the station KD-tree at startup (~100 ms), not on the first request."""
    state_codes()
    station_index()
    _boundaries()
