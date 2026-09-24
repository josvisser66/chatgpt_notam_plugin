import httpx
import pytest

from notam_plugin.altitude import AltitudeFilter, filter_features, parse_upper_limit
from notam_plugin.client import NmsClient
from notam_plugin.navigation import Coordinate, Navigation
from notam_plugin.routes import RouteSearches
from notam_plugin.server import build_server


def feature(identifier, **data):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1, 0]},
        "properties": {"coreNOTAMData": {"notam": {"id": identifier, **data}}},
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1200FT", (1200, "MSL")),
        ("1200 FT AMSL", (1200, "MSL")),
        ("1200FT AGL", (1200, "AGL")),
        ("FL180", (18000, "FL")),
        ("SFC", (0, "AGL")),
        ("304.8M MSL", (1000, "MSL")),
        ("UNL", (float("inf"), "UNLIMITED")),
        ("280M", None),
        ("UNKNOWN", None),
    ],
)
def test_upper_limit_parsing(value, expected):
    assert parse_upper_limit(value) == expected


def test_only_strictly_lower_comparable_limits_excluded():
    features = [
        feature("below", upperLimit="3000FT"),
        feature("equal", upperLimit="5000FT"),
        feature("above", upperLimit="6000FT"),
        feature("agl", upperLimit="1200FT AGL"),
        feature("fl", upperLimit="FL040"),
        feature("unknown"),
        feature("unlimited", upperLimit="UNL"),
        feature("default-q-line", maximumFl="999"),
    ]
    kept, summary = filter_features(features, AltitudeFilter(feet=5000))
    assert len(kept) == 7
    assert summary["excluded_below_count"] == 1
    assert summary["excluded_below"][0]["id"] == "below"
    assert summary["retained_uncertain_count"] == 4


def test_text_ranges_and_flight_levels():
    features = [
        feature("low", text="AIRSPACE UAS SFC-1200FT AGL DLY 1200-1500"),
        feature("high", text="AIRSPACE SFC-1200FT AGL AND SFC-3000FT AGL"),
        feature("unknown", text="RWY 12/30 CLOSED"),
        feature("fl", maximumFl="150"),
    ]
    kept, summary = filter_features(features, AltitudeFilter(feet=2000, reference="AGL"))
    assert len(kept) == 3
    assert summary["excluded_below"][0]["id"] == "low"
    kept, summary = filter_features(features, AltitudeFilter(feet=18000, reference="FL"))
    assert summary["excluded_below"][0]["id"] == "fl"


async def test_altitude_survives_route_continuation(settings):
    features = [feature("low", upperLimit="3000FT"), feature("high", upperLimit="6000FT")]

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        return httpx.Response(200, json={"status": "Success", "data": {"geojson": features}})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        search = RouteSearches(client, Navigation(settings.service.navigation_file))
        output = await search.start(
            [Coordinate(longitude=0, latitude=0), Coordinate(longitude=2, latitude=0)],
            5,
            AltitudeFilter(feet=5000),
        )
        again = await search.resume(output["search_id"])
        assert again["matching_count"] == 1
        summary = again["altitude_filter"]["matching_notams"]
        assert summary["excluded_below_count"] == 1
        assert summary["requested"]["reference"] == "MSL"


async def test_mcp_altitude_filter_forces_geojson(settings):
    settings.faa.response_format = "AIXM"

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        assert request.headers["nmsResponseFormat"] == "GEOJSON"
        return httpx.Response(
            200,
            json={
                "status": "Success",
                "data": {
                    "geojson": [
                        feature("low", upperLimit="3000FT"),
                        feature("high", upperLimit="5000FT"),
                    ]
                },
            },
        )

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        result = await build_server(client=client).call_tool(
            "search_notams",
            {
                "filters": {"location": "KSEA"},
                "altitude": {"feet": 5000},
            },
        )
        assert not result.isError
        data = result.structuredContent["response"]
        assert len(data["data"]["geojson"]) == 1
        assert data["altitude_filter"]["excluded_below_count"] == 1
