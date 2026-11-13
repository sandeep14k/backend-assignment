"""Download every numbered US motorway exit from OpenStreetMap (Overpass API), one state at a time.

Offline data preparation only; the API never calls Overpass. Each state's
``motorway_junction`` nodes are saved with the refs of the motorways they sit
on (e.g. "I 44"), to ``data/enrichment/junctions/<STATE>.json``. Existing
files are reused, so an interrupted run resumes. Then run
``python manage.py locate_exits`` to write ``data/exit_coordinates.csv``.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/enrichment/junctions"
URL = "https://overpass-api.de/api/interpreter"
STATES = [
    "AL",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "AK",
    "HI",
]
QUERY = """[out:json][timeout:600];
area["ISO3166-2"="US-{state}"]->.s;
node["highway"="motorway_junction"]["ref"](area.s)->.j;
.j out;
way(bn.j)["highway"~"^(motorway|trunk)$"]["ref"];
out body;"""


def fetch(state, session):
    for attempt in range(6):
        try:
            response = session.post(URL, data={"data": QUERY.format(state=state)}, timeout=700)
            if response.status_code == 200 and response.text.lstrip().startswith("{"):
                return response.json()
            print(f"{state}: HTTP {response.status_code}, retrying", flush=True)
        except requests.RequestException as exc:
            print(f"{state}: {exc}, retrying", flush=True)
        time.sleep(30 * 2**attempt)
    raise RuntimeError(f"{state}: Overpass kept failing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("states", nargs="*", default=STATES)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "fuel-route-planner/1.0 (assessment data preparation)"
    for state in args.states:
        path = OUT / f"{state}.json"
        if path.exists():
            continue
        started = time.monotonic()
        data = fetch(state, session)
        path.write_text(json.dumps(data))
        nodes = sum(e["type"] == "node" for e in data["elements"])
        print(f"{state}: {nodes} exits in {time.monotonic() - started:.0f} s", flush=True)
        time.sleep(5)  # Be polite to the shared public server.
    return 0


if __name__ == "__main__":
    sys.exit(main())
