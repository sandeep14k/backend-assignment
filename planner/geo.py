"""Vectorized geometry helpers (NumPy only; no per-point Python loops)."""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def decode_polyline(encoded, precision=6):
    """Decode a Google-encoded polyline into an (n, 2) float array of [lon, lat].

    Each value is a run of 5-bit chunks (the last chunk has bit 0x20 clear),
    zig-zag encoded, and stored as a delta from the previous point. All steps
    are array operations: ~1 ms for a 40,000-point cross-country route.
    """
    if not encoded:
        return np.empty((0, 2))
    data = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    if data.min() < 0 or data.max() > 63:
        raise ValueError("Invalid polyline characters.")
    is_last = data < 0x20
    if not is_last[-1]:
        raise ValueError("Truncated polyline.")
    ends = np.flatnonzero(is_last)
    starts = np.concatenate(([0], ends[:-1] + 1))
    position = np.arange(data.size) - np.repeat(starts, ends - starts + 1)
    values = np.add.reduceat((data & 0x1F) << (5 * position), starts)
    deltas = (values >> 1) ^ -(values & 1)
    if deltas.size % 2:
        raise ValueError("Polyline has an odd number of values.")
    lat_lon = np.cumsum(deltas.reshape(-1, 2), axis=0)
    return lat_lon[:, ::-1] / 10**precision


def segment_miles(coordinates):
    """Great-circle (haversine) length of each consecutive segment of [lon, lat] points."""
    lon, lat = np.radians(coordinates[:, 0]), np.radians(coordinates[:, 1])
    a = np.sin(np.diff(lat) / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def to_xyz(coordinates):
    """[lon, lat] degrees -> 3-D points on a sphere of Earth's radius, in miles.

    Straight-line (chord) distance between nearby points equals the surface
    distance to well under 0.1%, at any latitude, so one KD-tree works for the
    whole country without a map projection.
    """
    coordinates = np.atleast_2d(np.asarray(coordinates, dtype=float))
    lon, lat = np.radians(coordinates[:, 0]), np.radians(coordinates[:, 1])
    return EARTH_RADIUS_MILES * np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def simplify(coordinates, tolerance_miles=0.03):
    """Douglas-Peucker simplification (default tolerance ~50 m), processed level by level.

    Classic recursive implementations make one call per kept point. Here every
    open segment is handled in the same array pass, so the loop runs once per
    recursion depth (~25 passes) instead of once per point (~2,000 calls).

    Distances are measured in 3-D, so a surface arc counts as "off" its chord by
    its sag. Segments are therefore split until they are at most ~31 miles long
    (sag <= tolerance), which bounds chord-based station offsets to the tolerance too.
    """
    n = len(coordinates)
    if n < 3:
        return coordinates
    x, y, z = (np.ascontiguousarray(axis) for axis in to_xyz(coordinates).T)
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    undecided = np.ones(n, dtype=bool)
    undecided[[0, -1]] = False
    while undecided.any():
        kept = np.flatnonzero(keep)
        index = np.flatnonzero(undecided)
        segment = np.searchsorted(kept, index) - 1
        first, last = kept[segment], kept[segment + 1]
        # Distance to the finite segment: clipping preserves reversals beyond either endpoint.
        dx, dy, dz = x[last] - x[first], y[last] - y[first], z[last] - z[first]
        rx, ry, rz = x[index] - x[first], y[index] - y[first], z[index] - z[first]
        dd = dx * dx + dy * dy + dz * dz
        rd = rx * dx + ry * dy + rz * dz
        rr = rx * rx + ry * ry + rz * rz
        fraction = np.clip(rd / np.maximum(dd, 1e-24), 0, 1)
        distance = np.maximum(rr - 2 * fraction * rd + fraction * fraction * dd, 0)

        group_starts = np.concatenate(([0], np.flatnonzero(np.diff(segment)) + 1))
        group = np.repeat(np.arange(len(group_starts)), np.diff(np.append(group_starts, len(index))))
        group_max = np.maximum.reduceat(distance, group_starts)
        split = group_max > tolerance_miles**2

        undecided[index[~split[group]]] = False  # Whole segment within tolerance: drop its points.
        farthest = np.flatnonzero((distance == group_max[group]) & split[group])
        if farthest.size:
            farthest = farthest[np.concatenate(([True], np.diff(group[farthest]) > 0))]  # First per segment.
            keep[index[farthest]] = True
            undecided[index[farthest]] = False
    return coordinates[keep]


class Boundary:
    """Point-in-polygon test (even-odd rule) over all rings at once, plus distance to the border."""

    def __init__(self, rings):
        starts, ends = [], []
        for ring in rings:
            ring = np.asarray(ring, dtype=float)
            starts.append(ring)
            ends.append(np.roll(ring, -1, axis=0))
        self.a, self.b = np.concatenate(starts), np.concatenate(ends)

    def contains(self, lon, lat):
        (ax, ay), (bx, by) = self.a.T, self.b.T
        crosses = (ay > lat) != (by > lat)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at_lat = ax + (lat - ay) * (bx - ax) / (by - ay)
        return bool(np.count_nonzero(crosses & (lon < x_at_lat)) % 2)

    def distance_miles(self, lon, lat):
        """Approximate distance from the point to the nearest border edge."""
        scale = np.array([69.172 * np.cos(np.radians(lat)), 69.0])
        a, b, p = self.a * scale, self.b * scale, np.array([lon, lat]) * scale
        d = b - a
        t = np.clip(((p - a) * d).sum(axis=1) / np.maximum((d * d).sum(axis=1), 1e-12), 0, 1)
        return float(np.linalg.norm(a + t[:, None] * d - p, axis=1).min())


def heading_at(coordinates, segment_lengths, mile):
    """Compass heading (degrees, 0 = north) of the route segment containing ``mile``."""
    cumulative = np.cumsum(segment_lengths)
    i = int(min(np.searchsorted(cumulative, mile, side="right"), len(segment_lengths) - 1))
    (lon1, lat1), (lon2, lat2) = np.radians(coordinates[i]), np.radians(coordinates[i + 1])
    x = np.sin(lon2 - lon1) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(lon2 - lon1)
    return float(np.degrees(np.arctan2(x, y)) % 360)
