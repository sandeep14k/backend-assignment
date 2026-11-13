"""Checks that the supplied fuel-price CSV is loaded completely and correctly."""

import csv
from collections import defaultdict
from decimal import Decimal

import numpy as np
from django.conf import settings
from django.test import SimpleTestCase

from planner.data import CANADIAN_PROVINCES, cities, find_city, is_sourced, location, station_index, stations


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

    def test_each_station_sits_at_its_city_unless_overridden_nearby(self):
        from planner.geo import segment_miles

        for row in self.rows:
            station = self.by_id.get(row["OPIS Truckstop ID"].strip())
            if station is None:
                continue
            city = find_city(row["City"].strip(), row["State"].strip())
            centre = (float(city["LATITUDE"]), float(city["LONGITUDE"]))
            position = (station["latitude"], station["longitude"])
            if station["coordinate_precision"] == "city_centroid":
                self.assertEqual(position, centre)
            else:  # An entry in data/station_coordinates.csv: must still be near its own city.
                distance = segment_miles(np.array([[centre[1], centre[0]], [position[1], position[0]]]))[0]
                self.assertLessEqual(distance, 25.0)

    def test_names_and_addresses_are_trimmed(self):
        for station in self.inventory:
            self.assertEqual(station["name"], station["name"].strip())
            self.assertEqual(station["city"], station["city"].strip())
            self.assertTrue(Decimal("1") < Decimal(station["price_per_gallon"]) < Decimal("10"))

    def test_index_matches_inventory(self):
        index = station_index()
        self.assertGreater(len(index.stations), 0, "Bundled station coordinates must support real fuel plans")
        # Every priced US station is a candidate: no trip fails for want of eligible stations.
        self.assertEqual(len(index.stations), len(self.inventory))
        self.assertEqual(index.xyz.shape, (len(self.inventory), 3))
        sourced = [s for s in index.stations if is_sourced(s)]
        self.assertGreater(len(sourced), 250)
        # Every position is labelled by its real source: identity match, OSM exit, or town centre.
        self.assertTrue(
            all(is_sourced(s) or s["coordinate_precision"] in {"exit_match", "city_centroid"} for s in index.stations)
        )
        exits = [s for s in index.stations if s["coordinate_precision"] == "exit_match"]
        self.assertGreater(len(exits), 3000)
        # "AMPM, I-10, EXIT 140" had been name-matched to a store 15 miles from I-10; its exit overrules that.
        ampm = next(s for s in index.stations if s["id"] == "69912")
        self.assertEqual(ampm["coordinate_precision"], "exit_match")
        for station in index.stations:
            if station["coordinate_precision"] == "exit_match":
                self.assertTrue(station["coordinate_source"].startswith("https://www.openstreetmap.org/node/"))


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

    def test_coordinates_must_be_in_the_us_not_just_a_bounding_box(self):
        inside = {
            "Detroit": (42.3314, -83.0458),
            "Key West": (24.5551, -81.78),
            "San Juan PR": (18.4655, -66.1057),
            "Honolulu": (21.3069, -157.8583),
            "El Paso": (31.7619, -106.485),
            "Anchorage": (61.2181, -149.9003),
        }
        outside = {
            "Toronto": (43.6532, -79.3832),
            "Windsor ON (1 mi from Detroit)": (42.3149, -83.0364),
            "Nuevo Laredo": (27.4864, -99.5075),
            "Tijuana": (32.5149, -117.0382),
            "Vancouver": (49.2827, -123.1207),
            "Havana": (23.1136, -82.3666),
            "Bimini": (25.7270, -79.2970),
            "Tortola BVI": (18.4207, -64.64),
        }
        for name, (lat, lon) in inside.items():
            self.assertEqual(location(f"{lat}, {lon}")["matched"], "coordinates", name)
        for name, (lat, lon) in outside.items():
            with self.assertRaisesMessage(ValueError, "not within the USA", msg=name):
                location(f"{lat}, {lon}")

    def test_every_bundled_us_city_coordinate_passes_except_border_hamlets(self):
        from planner.data import in_us

        rejected = {
            (r["CITY"], r["STATE_CODE"])
            for r in cities().values()
            if not in_us(float(r["LATITUDE"]), float(r["LONGITUDE"]))
        }
        self.assertLessEqual(len(rejected), 10, rejected)

    def test_helpful_errors(self):
        with self.assertRaisesMessage(ValueError, "Did you mean Chicago, IL?"):
            location("Chicgo, IL")
        with self.assertRaisesMessage(ValueError, "Please add the state"):
            location("Kansas City")
        with self.assertRaisesMessage(ValueError, "not within the USA"):
            location("48.85, 2.35")
        for bad in ["Paris, FR", "", ",,,", " , ", None, 5, "x" * 201]:
            with self.assertRaises(ValueError):
                location(bad)

    def test_cities_in_several_counties_resolve_to_the_city_itself(self):
        self.assertAlmostEqual(location("Orlando, FL")["longitude"], -81.38, delta=0.15)  # not Brevard County
        self.assertAlmostEqual(location("Denver, CO")["latitude"], 39.74, delta=0.1)
        self.assertEqual(location("Henrico, VA")["name"], "Richmond, VA")
