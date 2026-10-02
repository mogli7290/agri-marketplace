"""Geometric helpers and route optimisation.

The optimiser is intentionally dependency-free: it uses a nearest-neighbour
construction followed by 2-opt improvement. For the handful of stops in a
typical farm-to-buyer shipment this is fast and close to optimal, and it keeps
the app functional even when no external AI provider is configured.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: Point, b: Point) -> float:
    """Great-circle distance between two ``(lat, lng)`` points in kilometres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def path_distance(points: Sequence[Point]) -> float:
    """Total distance along an ordered sequence of points."""
    return sum(haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


def nearest_neighbor_route(origin: Point, stops: Sequence[tuple[int, Point]]) -> list[int]:
    """Greedily visit the closest unvisited stop from the current position.

    ``stops`` is a sequence of ``(stop_id, point)``. Returns stop ids in visit
    order.
    """
    remaining = list(stops)
    route: list[int] = []
    current = origin
    while remaining:
        idx = min(
            range(len(remaining)),
            key=lambda i: haversine_km(current, remaining[i][1]),
        )
        stop_id, point = remaining.pop(idx)
        route.append(stop_id)
        current = point
    return route


def two_opt_improve(origin: Point, stops: Sequence[tuple[int, Point]], order: Sequence[int]) -> list[int]:
    """Improve a route with 2-opt swaps until no improvement is found."""
    points = dict(stops)
    route = list(order)
    if len(route) < 3:
        return route

    def total(seq: Sequence[int]) -> float:
        pts = [origin] + [points[i] for i in seq]
        return path_distance(pts)

    improved = True
    best = total(route)
    while improved:
        improved = False
        for i in range(len(route) - 1):
            for j in range(i + 1, len(route)):
                candidate = route[:i] + route[i : j + 1][::-1] + route[j + 1 :]
                candidate_cost = total(candidate)
                if candidate_cost + 1e-9 < best:
                    route, best = candidate, candidate_cost
                    improved = True
    return route


def optimize_stops(origin: Point, stops: Iterable[tuple[int, Point]]) -> dict:
    """Return an optimised visiting order and the resulting distance.

    Keys: ``order`` (list of stop ids), ``distance_km`` (float), ``legs`` (list
    of leg distances in km).
    """
    stops = list(stops)
    if not stops:
        return {"order": [], "distance_km": 0.0, "legs": []}

    initial = nearest_neighbor_route(origin, stops)
    order = two_opt_improve(origin, stops, initial)

    points = dict(stops)
    pts = [origin] + [points[i] for i in order]
    legs = [round(haversine_km(pts[i], pts[i + 1]), 2) for i in range(len(pts) - 1)]
    return {
        "order": order,
        "distance_km": round(sum(legs), 2),
        "legs": legs,
    }
