from django.test import SimpleTestCase

from planner.enrichment import match_stations
from planner.tests.helpers import make_station


def osm(identity, name, reference, lon=-87.6):
    return {
        "type": "node",
        "id": identity,
        "lat": 41.9,
        "lon": lon,
        "tags": {"amenity": "fuel", "name": name, "ref": reference},
    }


class EnrichmentTests(SimpleTestCase):
    def setUp(self):
        self.station = {**make_station("1", -87.6, 41.9), "name": "LOVES TRAVEL STOP #007", "state": "IL"}

    def test_brand_and_reference_match_has_source(self):
        found, report = match_stations([self.station], [osm(100, "Love's", "7")])
        self.assertEqual(found[0]["source"], "https://www.openstreetmap.org/node/100")
        self.assertEqual(report[0]["status"], "matched")

    def test_nearby_same_brand_is_not_enough(self):
        found, _ = match_stations([self.station], [osm(100, "Love's", "8")])
        self.assertEqual(found, [])

    def test_far_away_reference_match_is_rejected(self):
        found, _ = match_stations([self.station], [osm(100, "Love's", "7", -100)])
        self.assertEqual(found, [])

    def test_two_distinct_locations_are_ambiguous(self):
        found, report = match_stations([self.station], [osm(100, "Love's", "7"), osm(101, "Love's", "7", -87.61)])
        self.assertEqual(found, [])
        self.assertEqual(report[0]["status"], "ambiguous")

    def test_duplicate_opis_ids_for_same_facility_are_rejected(self):
        found, report = match_stations([self.station, {**self.station, "id": "2"}], [osm(100, "Love's", "7")])
        self.assertEqual(found, [])
        self.assertTrue(all(r["status"] == "conflicting_station_ids" for r in report))

    def test_exact_brand_address_without_reference(self):
        candidate = osm(100, "Love's", "")
        candidate["tags"].update({"addr:housenumber": "123", "addr:street": "North Main Street"})
        station = {**self.station, "address": "123 N Main St"}
        found, _ = match_stations([station], [candidate], source_date="2026-11-13")
        self.assertEqual(found[0]["match_method"], "brand_street_address")
        self.assertEqual(found[0]["source_date"], "2026-11-13")
        self.assertEqual(found[0]["confidence"], "high")

    def test_address_cannot_override_conflicting_store_reference(self):
        candidate = osm(100, "Love's", "8")
        candidate["tags"].update({"addr:housenumber": "123", "addr:street": "Main Street"})
        found, _ = match_stations([{**self.station, "address": "123 Main St"}], [candidate])
        self.assertEqual(found, [])

    def test_distinctive_name_requires_corroboration(self):
        station = {**self.station, "name": "Unique Truck Plaza", "city": "Chicago"}
        candidate = osm(100, "Unique Truck Plaza", "")
        self.assertEqual(match_stations([station], [candidate])[0], [])
        candidate["tags"]["addr:city"] = "Chicago"
        self.assertEqual(len(match_stations([station], [candidate])[0]), 1)
