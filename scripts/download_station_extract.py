"""Download state PBFs from Geofabrik and extract fuel objects locally.

Offline preparation only. Install requirements-enrichment.txt first. Existing
PBFs are reused; delete a specific file deliberately to refresh its snapshot.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import osmium
import requests


class FuelHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.elements = []

    def node(self, node):
        if node.tags.get("amenity") == "fuel" and node.location.valid():
            self.elements.append(
                {
                    "type": "node",
                    "id": node.id,
                    "lat": node.location.lat,
                    "lon": node.location.lon,
                    "tags": dict(node.tags),
                }
            )

    def way(self, way):
        if way.tags.get("amenity") != "fuel":
            return
        points = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(points) != len(way.nodes) or not points:
            return
        # Bounding-box centre, same convention as Overpass out center.
        self.elements.append(
            {
                "type": "way",
                "id": way.id,
                "tags": dict(way.tags),
                "center": {
                    "lon": (min(p[0] for p in points) + max(p[0] for p in points)) / 2,
                    "lat": (min(p[1] for p in points) + max(p[1] for p in points)) / 2,
                },
            }
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("states", nargs="+", help="Geofabrik state slugs, e.g. washington montana arizona")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "data/enrichment"
    directory.mkdir(parents=True, exist_ok=True)
    for state in args.states:
        if not state.replace("-", "").isalpha():
            parser.error("State must be an alphabetic Geofabrik slug")
        url = f"https://download.geofabrik.de/north-america/us/{state}-latest.osm.pbf"
        path = directory / f"{state}.osm.pbf"
        if not path.exists():
            with requests.get(url, stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                temporary = path.with_suffix(".part")
                with temporary.open("wb") as output:
                    for block in response.iter_content(1024 * 1024):
                        output.write(block)
                temporary.replace(path)
        with osmium.io.Reader(str(path)) as reader:
            timestamp = reader.header().get("osmosis_replication_timestamp") or "unknown"
        handler = FuelHandler()
        handler.apply_file(str(path), locations=True, idx="sparse_file_array")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        payload = {
            "osm3s": {"timestamp_osm_base": timestamp},
            "source": url,
            "sha256": digest,
            "processed_at": datetime.now(UTC).isoformat(),
            "limitations": "Fuel nodes and ways only; relations excluded.",
            "elements": handler.elements,
        }
        (directory / f"{state}-fuel.json").write_text(json.dumps(payload))
        print(state, len(handler.elements), flush=True)


if __name__ == "__main__":
    main()
