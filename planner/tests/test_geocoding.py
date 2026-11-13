from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from planner import cache
from planner.geocoding import resolve, resolve_pair

WHITE_HOUSE = "1600 Pennsylvania Ave NW, Washington, DC 20500"


def census(matches):
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"result": {"addressMatches": matches}}
    return response


FOUND = census(
    [
        {
            "matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
            "coordinates": {"x": -77.0352, "y": 38.8987},
        }
    ]
)


@patch("planner.geocoding._session.get")
class GeocodingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_cities_and_coordinates_never_call_the_geocoder(self, get):
        self.assertEqual(resolve("Chicago, IL")[1], 0)
        self.assertEqual(resolve("41.88, -87.63")[1], 0)
        get.assert_not_called()

    def test_street_address_is_geocoded_once_then_cached(self, get):
        get.return_value = FOUND
        point, calls = resolve(WHITE_HOUSE)
        self.assertEqual((point["matched"], calls), ("address", 1))
        self.assertAlmostEqual(point["latitude"], 38.8987)
        self.assertEqual(point["name"], "1600 Pennsylvania Ave NW, Washington, DC, 20500")
        self.assertEqual(resolve(WHITE_HOUSE.upper())[1], 0)  # Case/whitespace-insensitive cache key.
        get.assert_called_once()

    def test_unknown_address_falls_back_to_its_city(self, get):
        get.return_value = census([])
        point, calls = resolve("99999 Nowhere Rd, Denver, CO")
        self.assertEqual((point["name"], point["matched"], calls), ("Denver, CO", "city (address not found)", 1))

    def test_geocoder_outage_falls_back_to_its_city(self, get):
        get.side_effect = requests.ConnectionError("down")
        point, _ = resolve(WHITE_HOUSE)
        self.assertEqual((point["name"], point["matched"]), ("Washington, DC", "city (geocoder unavailable)"))

    def test_pair_counts_calls(self, get):
        get.return_value = FOUND
        start, finish, calls = resolve_pair(WHITE_HOUSE, "Chicago, IL")
        self.assertEqual((start["matched"], finish["name"], calls), ("address", "Chicago, IL", 1))

    def test_oversize_address_rejected_before_network(self, get):
        with self.assertRaises(ValueError):
            resolve("123 " + "x" * 201)
        get.assert_not_called()

    def test_malformed_match_falls_back_to_city(self, get):
        get.return_value = census([{}])
        point, calls = resolve(WHITE_HOUSE)
        self.assertEqual((point["matched"], calls), ("city (geocoder unavailable)", 1))

    @patch("planner.geocoding._executor.submit")
    def test_city_pair_does_not_schedule_threads(self, submit, get):
        start, finish, calls = resolve_pair("Chicago, IL", "Los Angeles, CA")
        self.assertEqual((start["name"], finish["name"], calls), ("Chicago, IL", "Los Angeles, CA", 0))
        submit.assert_not_called()
        get.assert_not_called()
