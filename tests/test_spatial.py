import math
import time

import httpx
import pytest

from notam_plugin.client import NmsClient
from notam_plugin.geo import GEOD, NM, Corridor, covering_circles
from notam_plugin.navigation import Coordinate, Navigation
from notam_plugin.routes import RouteSearches


def point(lon, lat, identifier="1"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"coreNOTAMData": {"notam": {"id": identifier, "text": "SOURCE"}}},
    }


def route(*points):
    return [{"longitude": lon, "latitude": lat} for lon, lat in points]


def test_corridor_middle_outside_endcaps_and_unknown():
    corridor = Corridor(route((0, 0), (2, 0)), 5)
    assert corridor.intersects(point(1, 0.05)) is True
    assert corridor.intersects(point(1, 0.2)) is False
    assert corridor.intersects(point(-0.05, 0)) is True
    assert corridor.intersects(point(-0.2, 0)) is False
    assert corridor.intersects({"geometry": None}) is None
    assert corridor.intersects({"geometry": {"type": "Point", "coordinates": []}}) is None


def test_polygon_intersection_not_just_centroid():
    corridor = Corridor(route((0, 0), (2, 0)), 5)
    polygon = {
        "type": "Polygon",
        "coordinates": [[[0.9, -0.1], [1.1, -0.1], [1.1, 3], [0.9, 3], [0.9, -0.1]]],
    }
    assert corridor.intersects({"geometry": polygon}) is True
    line = {"type": "LineString", "coordinates": [[1, -2], [1, 2]]}
    assert corridor.intersects({"geometry": line}) is True


def test_polygon_hole_and_geometry_collection():
    corridor = Corridor(route((0, 0), (1, 0)), 2)
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[-3, -3], [3, -3], [3, 3], [-3, 3], [-3, -3]],
            [[-2, -2], [-2, 2], [2, 2], [2, -2], [-2, -2]],
        ],
    }
    assert corridor.intersects({"geometry": polygon}) is False
    geometry = {
        "type": "GeometryCollection",
        "geometries": [
            polygon,
            {"type": "Point", "coordinates": [0.5, 0.01]},
        ],
    }
    assert corridor.intersects({"geometry": geometry}) is True


def test_dateline_and_high_latitudes():
    corridor = Corridor(route((179.5, 0), (-179.5, 0)), 5)
    assert corridor.intersects(point(180, 0.03)) is True
    assert corridor.intersects(point(178, 0)) is False
    corridor = Corridor(route((-150, 80), (-140, 80)), 10)
    middle = GEOD.npts(-150, 80, -140, 80, 1)[0]
    assert corridor.intersects(point(*middle)) is True


@pytest.mark.parametrize("width", [0.1, 5, 50, 99])
def test_circles_cover_corridor_edges_without_gaps(width):
    points = route((-122.3, 47.4), (-122.4, 37.6))
    circles = covering_circles(points, width)
    azimuth, _, distance = GEOD.inv(-122.3, 47.4, -122.4, 37.6)
    for i in range(41):
        center_lon, center_lat, _ = GEOD.fwd(-122.3, 47.4, azimuth, distance * i / 40)
        for side in [-90, 90]:
            lon, lat, _ = GEOD.fwd(center_lon, center_lat, azimuth + side, width * NM)
            nearest = min(GEOD.inv(lon, lat, c["longitude"], c["latitude"])[2] for c in circles)
            assert nearest <= 100 * NM
    assert all(math.isfinite(c["latitude"]) for c in circles)


async def test_route_queries_deduplicate_and_keep_unknown_geometry(settings):
    calls = []
    unknown = point(1, 0, "unknown")
    unknown["geometry"] = None
    features = [point(1, 0.03, "inside"), point(1, 2, "outside"), unknown]

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        assert "location" not in request.url.params
        calls.append(request)
        return httpx.Response(200, json={"status": "Success", "data": {"geojson": features}})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        searches = RouteSearches(client, Navigation(settings.service.navigation_file))
        result = await searches.start(
            [Coordinate(longitude=0, latitude=0), Coordinate(longitude=2, latitude=0)], 5
        )
        assert result["coverage_complete"] is True
        assert result["matching_count"] == 1
        assert result["unclassified_count"] == 1
        assert result["notams"][0]["properties"]["coreNOTAMData"]["notam"]["text"] == "SOURCE"
        count = len(calls)
        await searches.resume(result["search_id"])
        assert len(calls) == count  # completed searches return their dated snapshot


async def test_route_resumes_after_rate_limit_and_restart(settings, monkeypatch):
    settings.limits.data_interval_seconds = 180
    calls = []

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        calls.append(str(request.url))
        return httpx.Response(200, json={"status": "Success", "data": {"geojson": []}})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        search = RouteSearches(client, Navigation(settings.service.navigation_file))
        result = await search.start(
            [Coordinate(longitude=0, latitude=0), Coordinate(longitude=2, latitude=0)], 5
        )
        assert result["coverage_complete"] is False
        assert result["circles_completed"] == 1
        assert result["retry_after_seconds"] == 180
    future = time.time() + 181
    monkeypatch.setattr("notam_plugin.rate_limit.time.time", lambda: future)
    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        search = RouteSearches(client, Navigation(settings.service.navigation_file))
        result = await search.resume(result["search_id"])
        assert result["coverage_complete"] is True
        assert len(calls) == 2
        assert calls[0] != calls[1]


async def test_missing_geojson_is_not_complete_coverage(settings):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        return httpx.Response(200, json={"status": "Success", "data": {"aixm": []}})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        search = RouteSearches(client, Navigation(settings.service.navigation_file))
        result = await search.start(
            [Coordinate(longitude=0, latitude=0), Coordinate(longitude=2, latitude=0)], 5
        )
        assert result["coverage_complete"] is False
        assert result["circles_completed"] == 0
        assert "GeoJSON" in result["error"]
