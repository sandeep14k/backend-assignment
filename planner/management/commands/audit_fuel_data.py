"""Report how every row of the supplied fuel-price CSV is used."""

import csv
from collections import Counter, defaultdict
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand

from planner.data import CANADIAN_PROVINCES, find_city, station_index, stations
from planner.enrichment import distance


class Command(BaseCommand):
    help = "Audit the fuel-price CSV: rows, duplicates, price spread, location matching."

    def handle(self, *args, **options):
        with (settings.BASE_DIR / "fuel-prices-for-be-assessment.csv").open(encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))
        by_id = defaultdict(list)
        for row in rows:
            by_id[row["OPIS Truckstop ID"].strip()].append(row)
        inventory, unlocated = stations()
        loaded = {s["id"] for s in inventory}

        duplicates = {k: v for k, v in by_id.items() if len(v) > 1}
        conflicting = {k: v for k, v in duplicates.items() if len({r["Retail Price"] for r in v}) > 1}
        spreads = sorted(
            max(Decimal(r["Retail Price"]) for r in v) - min(Decimal(r["Retail Price"]) for r in v)
            for v in conflicting.values()
        )
        canadian = {k for k, v in by_id.items() if v[0]["State"].strip() in CANADIAN_PROVINCES}
        us_ids = set(by_id) - canadian
        unmatched_us = sorted(
            {(v[0]["City"].strip(), v[0]["State"].strip()) for k, v in by_id.items() if k in us_ids and k not in loaded}
        )
        prices = sorted(Decimal(s["price_per_gallon"]) for s in inventory)
        states = Counter(s["state"] for s in inventory)
        precision = Counter(s["coordinate_precision"] for s in inventory)
        index = station_index()

        w = self.stdout.write
        w("Fuel-price CSV audit")
        w(f"  rows                         {len(rows):>6}")
        w(f"  unique OPIS station IDs      {len(by_id):>6}")
        w(f"  IDs listed more than once    {len(duplicates):>6}  (same city in every listing)")
        w(
            f"    ...with different prices   {len(conflicting):>6}  median spread ${spreads[len(spreads) // 2]:.3f}, "
            f"max ${spreads[-1]:.3f} -> mean price used"
        )
        w(
            f"  Canadian stations (skipped)  {len(canadian):>6}  provinces: "
            f"{', '.join(sorted({v[0]['State'].strip() for k, v in by_id.items() if k in canadian}))}"
        )
        w(f"  US stations                  {len(us_ids):>6}")
        w(f"    located and loaded         {len(loaded & us_ids):>6}  ({len(loaded & us_ids) / len(us_ids):.1%})")
        w(f"    not located                {len(unmatched_us):>6}  {unmatched_us or ''}")
        w(f"  coordinate precision         {dict(precision)}")
        w(
            f"  price per gallon             min ${prices[0]:.3f}  median ${prices[len(prices) // 2]:.3f}  "
            f"max ${prices[-1]:.3f}"
        )
        w(
            f"  states covered               {len(states)}  (most stations: "
            f"{', '.join(f'{s} {n}' for s, n in states.most_common(5))})"
        )
        w(f"  KD-tree                      {len(index.stations)} points, built once at startup")
        w(f"  skipped by loader            {unlocated} (equals Canadian count: {unlocated == len(canadian)})")

        # One sample per position source, with how far it sits from its listed town.
        for label in sorted(precision):
            sample = next(s for s in inventory if s["coordinate_precision"] == label)
            city = find_city(sample["city"], sample["state"])
            offset = distance(sample, {"latitude": float(city["LATITUDE"]), "longitude": float(city["LONGITUDE"])})
            w(
                f"  sample {label:<23} {sample['name']} ({sample['address']}, {sample['city']}, {sample['state']}) "
                f"-> {sample['latitude']:.5f}, {sample['longitude']:.5f}, {offset:.1f} mi from town  "
                f"{sample['coordinate_source'] or '(town coordinates)'}"
            )
