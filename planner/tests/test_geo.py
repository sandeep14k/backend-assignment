"""Vectorized geometry is checked against simple reference implementations."""

import random

import numpy as np
from django.test import SimpleTestCase

from planner.geo import decode_polyline, segment_miles, simplify, to_xyz
from planner.optimizer import candidates
from planner.tests.helpers import encode, fake_route, index_of, make_station


def reference_simplify(points, tolerance):
    """Textbook recursive Douglas-Peucker on the same 3-D coordinates."""
    xyz = to_xyz(points)
    keep = {0, len(points) - 1}

    def recurse(first, last):
        if last - first < 2:
            return
        a, b = xyz[first], xyz[last]
        direction = b - a
        length = np.linalg.norm(direction)
        inner = xyz[first + 1 : last]
        if length:
            distances = np.linalg.norm(np.cross(direction, inner - a), axis=1) / length
        else:
            distances = np.linalg.norm(inner - a, axis=1)
        farthest = int(distances.argmax())
        if distances[farthest] > tolerance:
            split = first + 1 + farthest
            keep.add(split)
            recurse(first, split)
            recurse(split, last)

    recurse(0, len(points) - 1)
    return points[sorted(keep)]


def random_walk(rng, n, start=(-100.0, 40.0), step=0.01):
    points = [start]
    heading = rng.uniform(0, 2 * np.pi)
    for _ in range(n - 1):
        heading += rng.gauss(0, 0.3)
        lon, lat = points[-1]
        points.append((lon + step * np.cos(heading), lat + step * np.sin(heading)))
    return np.round(np.array(points), 6)


class PolylineTests(SimpleTestCase):
    def test_round_trip_matches_reference_encoder(self):
        rng = random.Random(3)
        for _ in range(20):
            points = random_walk(rng, rng.randint(2, 3000), step=rng.choice([0.0001, 0.01, 2.0]))
            np.testing.assert_allclose(decode_polyline(encode(points)), points, atol=1e-6)

    def test_rejects_malformed_input(self):
        for bad in ["_p~iF~ps|U_", "\x10abc"]:
            with self.assertRaises(ValueError):
                decode_polyline(bad)
        self.assertEqual(decode_polyline("").shape, (0, 2))

    def test_haversine_known_distance(self):
        chicago_la = np.array([(-87.629798, 41.878114), (-118.243685, 34.052234)])
        self.assertAlmostEqual(segment_miles(chicago_la)[0], 1745, delta=5)


class SimplifyTests(SimpleTestCase):
    def test_matches_textbook_recursive_douglas_peucker(self):
        rng = random.Random(5)
        for _ in range(15):
            points = random_walk(rng, rng.randint(3, 2500))
            np.testing.assert_array_equal(simplify(points, 0.03), reference_simplify(points, 0.03))

    def test_keeps_endpoints_and_drops_collinear_points(self):
        line = np.array([[-90 + i / 10000, 40.0] for i in range(1001)])  # 5 miles: straight to < 1 m
        self.assertEqual(len(simplify(line)), 2)

    def test_long_arcs_are_split_so_chords_stay_within_tolerance(self):
        # A 69-mile meridian arc sags 0.15 mi below its chord; segments are split until the sag is <= 0.03 mi.
        arc = np.array([[-90.0, 40 + i / 1000] for i in range(1001)])
        simplified = simplify(arc, 0.03)
        self.assertGreater(len(simplified), 2)
        self.assertTrue((segment_miles(simplified) <= 31.0).all())


class CandidateTests(SimpleTestCase):
    def brute_force(self, route, stations, corridor):
        """Distance from every station to every route segment, no spatial index."""
        xyz, result = to_xyz(route["coordinates"]), {}
        cumulative = np.concatenate(([0], np.cumsum(route["segment_miles"])))
        for s in stations:
            p = to_xyz([[s["longitude"], s["latitude"]]])[0]
            best = None
            for i in range(len(xyz) - 1):
                a, d = xyz[i], xyz[i + 1] - xyz[i]
                t = float(np.clip(np.dot(p - a, d) / max(np.dot(d, d), 1e-12), 0, 1))
                offset = float(np.linalg.norm(a + t * d - p))
                if best is None or offset < best[0]:
                    best = (offset, cumulative[i] + t * route["segment_miles"][i])
            if best[0] <= corridor:
                result[s["id"]] = best[1]
        return result

    def test_matches_brute_force_on_random_routes(self):
        rng = random.Random(9)
        for _ in range(10):
            coordinates = random_walk(rng, 60, step=0.2)
            route = fake_route(0, tuple(map(tuple, coordinates)))
            route["segment_miles"] = segment_miles(coordinates)
            route["distance_miles"] = float(route["segment_miles"].sum())
            low, high = coordinates.min(axis=0) - 0.2, coordinates.max(axis=0) + 0.2
            stations = [
                make_station(i, rng.uniform(low[0], high[0]), rng.uniform(low[1], high[1]), rng.uniform(2.5, 4))
                for i in range(400)
            ]
            found = {s["id"]: s["route_mile"] for s in candidates(route, index_of(stations), 5.0)}
            expected = self.brute_force(route, stations, 5.0)
            # Co-located stations collapse to the cheapest, so found is a subset with identical miles.
            self.assertTrue(set(found) <= set(expected))
            self.assertGreater(len(found), 0.9 * len({round(m, 6) for m in expected.values()}))
            for station_id, mile in found.items():
                self.assertAlmostEqual(mile, expected[station_id], places=6)

    def test_projection_and_corridor(self):
        route = fake_route(100, ((-100, 40), (-99, 40)))
        stations = [make_station("on", -99.5, 40.01, 3), make_station("off", -99.5, 41.0, 1)]
        result = candidates(route, index_of(stations), corridor_miles=5)
        self.assertEqual([s["id"] for s in result], ["on"])
        self.assertAlmostEqual(result[0]["route_mile"], 50, delta=0.1)
        # 0.69 mi along the 40th parallel; measured to the great-circle chord it is ~0.62 mi.
        self.assertAlmostEqual(result[0]["offset_miles"], 0.69, delta=0.1)
