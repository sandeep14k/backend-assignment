"""Prepare exact OPIS/store matches from official operator pages for demonstration gaps.

No external prices are read. Coordinates come from the operator's JSON-LD.
The bundled evidence file is sufficient for offline reimport.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from planner.data import stations  # noqa: E402
from planner.enrichment import brand, store_ref  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stores", nargs="+")
    args = parser.parse_args()
    inventory = stations()[0]
    destination = ROOT / "data/operator-evidence.json"
    evidence = json.loads(destination.read_text()) if destination.exists() else {}
    for reference in args.stores:
        matches = [s for s in inventory if brand(s["name"]) == "loves" and store_ref(s["name"]) == reference]
        assert len(matches) == 1, f"Ambiguous CSV identity {reference}"
        station = matches[0]
        url = f"https://www.loves.com/locations/{reference}"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        html = response.text
        codes = re.findall(r'"fieldName":"opiscode","fieldValue":"(\d+)"', html)
        assert set(codes) == {station["id"]}, f"OPIS mismatch for {reference}: {codes}"
        documents = [
            json.loads(raw)
            for raw in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
        ]
        place = next(doc for doc in documents if "geo" in doc)
        assert place["address"]["addressRegion"] == station["state"]
        assert reference in place["name"], place["name"]
        evidence[station["id"]] = {
            "id": station["id"],
            "store_reference": reference,
            "source": response.url,
            "source_date": datetime.now(UTC).date().isoformat(),
            "source_sha256": hashlib.sha256(response.content).hexdigest(),
            "source_name": place["name"],
            "source_address": place["address"]["streetAddress"],
            "source_city": place["address"]["addressLocality"],
            "source_state": station["state"],
            "latitude": place["geo"]["latitude"],
            "longitude": place["geo"]["longitude"],
            "match_method": "operator_exact_opis_and_store",
            "confidence": "high",
            "precision": "operator_identity_match",
        }
        destination.write_text(json.dumps(evidence, indent=2) + "\n")
        print(reference, station["id"], place["geo"], flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
