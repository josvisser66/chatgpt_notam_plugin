from __future__ import annotations

import math
from typing import Any

from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform

from .errors import NmsError

GEOD = Geod(ellps="WGS84")
NM = 1852.0
TOLERANCE_M = 100.0


def route_vertices(points: list[dict], step_m: float) -> list[tuple[float, float]]:
    vertices = [(points[0]["longitude"], points[0]["latitude"])]
    for point in points[1:]:
        start = vertices[-1]
        end = (point["longitude"], point["latitude"])
        _, _, distance = GEOD.inv(*start, *end)
        if distance < 0.01:
            continue
        count = math.ceil(distance / step_m) - 1
        if count > 5000:
            raise NmsError(
                "Route requires too many samples. Use a shorter route or narrower corridor."
            )
        if count:
            vertices.extend(GEOD.npts(*start, *end, count))
        vertices.append(end)
    return vertices


def covering_circles(points: list[dict], width_nm: float) -> list[dict]:
    # Triangle inequality: every corridor point is <= width + half-spacing from a center.
    # A small margin also covers the conservative geometry tolerance at the boundary.
    step = 2 * ((100 - width_nm) * NM - TOLERANCE_M)
    if step <= 0:
        raise NmsError("Corridor half-width must be less than 99.9 NM.")
    vertices = route_vertices(points, step)
    if len(vertices) > 500:
        raise NmsError("Route requires over 500 FAA queries. Split it into shorter routes.")
    return [{"latitude": lat, "longitude": lon, "radius": 100.0} for lon, lat in vertices]


def _densify(coords: list) -> list:
    output = [coords[0][:2]]
    for first, second in zip(coords, coords[1:], strict=False):
        _, _, distance = GEOD.inv(*first[:2], *second[:2])
        if not math.isfinite(distance):
            raise ValueError("Invalid geometry coordinates")
        count = min(5000, max(0, math.ceil(distance / (5 * NM)) - 1))
        if count:
            output.extend(GEOD.npts(*first[:2], *second[:2], count))
        output.append(second[:2])
    return output


def densify_geometry(geometry: dict) -> dict:
    kind = geometry["type"]
    if kind == "GeometryCollection":
        return {"type": kind, "geometries": [densify_geometry(g) for g in geometry["geometries"]]}
    coords = geometry["coordinates"]
    depth = {"LineString": 0, "MultiLineString": 1, "Polygon": 1, "MultiPolygon": 2}.get(kind)
    if depth is None:
        return geometry

    def apply(value: list, level: int) -> list:
        return _densify(value) if level == 0 else [apply(v, level - 1) for v in value]

    return {"type": kind, "coordinates": apply(coords, depth)}


class Corridor:
    def __init__(self, points: list[dict], width_nm: float):
        self.width_m = width_nm * NM
        vertices = route_vertices(points, 25 * NM)
        self.parts = []
        pairs = list(zip(vertices, vertices[1:], strict=False)) or [(vertices[0], vertices[0])]
        for first, second in pairs:
            az, _, length = GEOD.inv(*first, *second)
            lon, lat, _ = GEOD.fwd(*first, az, length / 2)
            crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
            project = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
            line = (
                Point(project(*first))
                if length < 0.01
                else LineString(
                    [
                        project(*first),
                        project(*second),
                    ]
                )
            )
            self.parts.append((project, line))

    def intersects(self, feature: dict[str, Any]) -> bool | None:
        """Conservative 100 m tolerance; missing/invalid geometry remains unclassified."""
        geometry = feature.get("geometry")
        if not geometry:
            return None
        try:
            geom = shape(densify_geometry(geometry))
            if geom.is_empty:
                return None
            uncertain = False
            for project, line in self.parts:
                projected = transform(project, geom)
                if not projected.is_valid or not all(math.isfinite(v) for v in projected.bounds):
                    uncertain = True
                    continue
                if projected.distance(line) <= self.width_m + TOLERANCE_M:
                    return True
            return None if uncertain else False
        except (ValueError, TypeError, KeyError, IndexError, GEOSException, ProjError):
            return None
