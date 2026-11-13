from django.test import SimpleTestCase

from planner.exits import junctions_from_overpass, match_exit, osm_exits, osm_routes, parse_exit_address

TOWN = (36.62, -95.22)  # Big Cabin, OK


def junction(node_id, lat, lon, exit_ref, route_ref):
    return junctions_from_overpass(
        [
            {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": {"ref": exit_ref}},
            {"type": "way", "id": node_id * 10, "nodes": [node_id], "tags": {"ref": route_ref}},
        ]
    )[0]


class ParseTests(SimpleTestCase):
    def test_address_formats_from_the_csv(self):
        cases = {
            "I-44, EXIT 283 & US-69": ({("I", "44")}, ("283", "")),
            "I-55/57, EXIT 66A & US-60": ({("I", "55"), ("I", "57")}, ("66", "A")),
            "I-65 EXIT20, US-31 W EXIT 4": ({("I", "65")}, ("20", "")),
            "I-80 Exit 386": ({("I", "80")}, ("386", "")),
            "I-40/US-270, EXIT 166 & SR-3": ({("I", "40"), ("US", "270")}, ("166", "")),
            "SR-29/SR-55, EXIT 234 & SR-47": ({("S", "29"), ("S", "55")}, ("234", "")),
            "I-35 & SR-9, EXIT 108": ({("I", "35"), ("S", "9")}, ("108", "")),
        }
        for address, expected in cases.items():
            self.assertEqual(parse_exit_address(address), expected, address)

    def test_addresses_without_an_exit_number_are_not_guessed(self):
        for address in ["US-69", "US-70 & SR-467", "I-44 & SR-66", "310 East McCoy Boulevard", ""]:
            self.assertIsNone(parse_exit_address(address), address)

    def test_osm_refs(self):
        self.assertEqual(osm_routes("I 44;OK 66"), {("I", "44"), ("S", "66")})
        self.assertEqual(osm_routes("US 69;US 75"), {("US", "69"), ("US", "75")})
        self.assertEqual(osm_exits("66A;66B"), {("66", "A"), ("66", "B")})
        self.assertEqual(osm_exits("283"), {("283", "")})


class MatchTests(SimpleTestCase):
    def test_both_directions_of_one_exit_are_averaged(self):
        found = match_exit(
            "I-44, EXIT 283 & US-69",
            *TOWN,
            [junction(1, 36.600, -95.210, "283", "I 44"), junction(2, 36.602, -95.212, "283", "I 44;OK 66")],
        )
        self.assertAlmostEqual(found["latitude"], 36.601)
        self.assertEqual(found["nodes"], [1, 2])
        self.assertLess(found["town_miles"], 2)

    def test_exit_on_another_highway_is_rejected(self):
        self.assertIsNone(match_exit("I-44, EXIT 283", *TOWN, [junction(1, 36.6, -95.21, "283", "I 35")]))

    def test_exit_far_from_the_town_is_rejected(self):
        self.assertIsNone(match_exit("I-44, EXIT 283", *TOWN, [junction(1, 37.2, -95.2, "283", "I 44")]))

    def test_far_apart_candidates_are_ambiguous(self):
        candidates = [junction(1, 36.60, -95.21, "283", "I 44"), junction(2, 36.70, -95.10, "283", "I 44")]
        self.assertIsNone(match_exit("I-44, EXIT 283", *TOWN, candidates))

    def test_exact_letter_beats_bare_number(self):
        candidates = [junction(1, 36.60, -95.21, "66A", "I 44"), junction(2, 36.70, -95.10, "66", "I 44")]
        self.assertEqual(match_exit("I-44, EXIT 66A", *TOWN, candidates)["nodes"], [1])

    def test_lettered_exits_accept_a_bare_number_station(self):
        candidates = [junction(1, 36.600, -95.210, "66A", "I 44"), junction(2, 36.601, -95.211, "66B", "I 44")]
        self.assertEqual(match_exit("I-44, EXIT 66", *TOWN, candidates)["nodes"], [1, 2])

    def test_junction_without_a_numbered_motorway_is_ignored(self):
        lonely = junctions_from_overpass(
            [{"type": "node", "id": 1, "lat": 36.6, "lon": -95.21, "tags": {"ref": "283"}}]
        )
        self.assertEqual(lonely, [])
