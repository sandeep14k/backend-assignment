"""Checks that the supplied fuel-price CSV is loaded completely and correctly."""

import csv
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.test import SimpleTestCase

from planner.data import CANADIAN_PROVINCES, find_city, location, station_index, stations


def csv_rows():
    with (settings.BASE_DIR / "fuel-prices-for-be-assessment.csv").open(encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


class FuelDataTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rows = csv_rows()
        cls.inventory, cls.unlocated = stations()
        cls.by_id = {s["id"]: s for s in cls.inventory}

    def test_every_us_station_is_loaded_and_only_canadian_ones_are_skipped(self):
        ids = {r["OPIS Truckstop ID"].strip() for r in self.rows}
        canadian = {r["OPIS Truckstop ID"].strip() for r in self.rows if r["State"].strip() in CANADIAN_PROVINCES}
        self.assertEqual(len(self.rows), 8151)
        self.assertEqual(set(self.by_id), ids - canadian)
        self.assertEqual(self.unlocated, len(canadian))

    def test_prices_are_the_mean_of_each_stations_listings(self):
        listed = defaultdict(list)
        for row in self.rows:
            listed[row["OPIS Truckstop ID"].strip()].append(Decimal(row["Retail Price"]))
        for station_id, station in self.by_id.items():
            mean = sum(listed[station_id]) / len(listed[station_id])
            self.assertAlmostEqual(Decimal(station["price_per_gallon"]), mean, places=7)
        self.assertEqual(self.by_id["7"]["price_per_gallon"], "3.00733333")

    def test_each_station_sits_at_its_own_city(self):
        for row in self.rows:
            station = self.by_id.get(row["OPIS Truckstop ID"].strip())
            if station is None:
                continue
            city = find_city(row["City"].strip(), row["State"].strip())
            expected = (float(city["LATITUDE"]), float(city["LONGITUDE"]))
            self.assertEqual((station["latitude"], station["longitude"]), expected)

    def test_names_and_addresses_are_trimmed(self):
        for station in self.inventory:
            self.assertEqual(station["name"], station["name"].strip())
            self.assertEqual(station["city"], station["city"].strip())
            self.assertTrue(Decimal("1") < Decimal(station["price_per_gallon"]) < Decimal("10"))

    def test_index_matches_inventory(self):
        index = station_index()
        self.assertEqual(len(index.stations), len(self.inventory))
        self.assertEqual(index.xyz.shape, (len(self.inventory), 3))


class LocationTests(SimpleTestCase):
    def test_accepted_formats(self):
        cases = {
            "Chicago, IL": "Chicago, IL",
            "chicago , illinois": "Chicago, IL",
            "Chicago IL": "Chicago, IL",
            "Chicago, Illinois, USA": "Chicago, IL",
            "Los Angeles, CA 90012": "Los Angeles, CA",
            "Salt Lake City Utah": "Salt Lake City, UT",
            "Washington DC": "Washington, DC",
            "New York City, NY": "New York, NY",
            "NYC": "New York, NY",
            "St. Louis, MO": "Saint Louis, MO",
            "Albuquerque": "Albuquerque, NM",  # only one state has it
            "233 S Wacker Dr, Chicago, IL 60606": "Chicago, IL",  # offline fallback to the city
        }
        for text, expected in cases.items():
            self.assertEqual(location(text)["name"], expected, text)
        self.assertEqual(location("41.88 -87.63")["matched"], "coordinates")
        self.assertAlmostEqual(location("41.88, -87.63")["latitude"], 41.88)

    def test_helpful_errors(self):
        with self.assertRaisesMessage(ValueError, "Did you mean Chicago, IL?"):
            location("Chicgo, IL")
        with self.assertRaisesMessage(ValueError, "Please add the state"):
            location("Kansas City")
        with self.assertRaisesMessage(ValueError, "not within the USA"):
            location("48.85, 2.35")
        for bad in ["Paris, FR", "", None, 5, "x" * 201]:
            with self.assertRaises(ValueError):
                location(bad)

    def test_cities_in_several_counties_resolve_to_the_city_itself(self):
        self.assertAlmostEqual(location("Orlando, FL")["longitude"], -81.38, delta=0.15)  # not Brevard County
        self.assertAlmostEqual(location("Denver, CO")["latitude"], 39.74, delta=0.1)
        self.assertEqual(location("Henrico, VA")["name"], "Richmond, VA")
