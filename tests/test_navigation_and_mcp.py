import csv
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from notam_plugin.client import NmsClient
from notam_plugin.errors import NmsError
from notam_plugin.navigation import Coordinate, Navigation
from notam_plugin.server import build_server


def archive(name, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    zipped = io.BytesIO()
    with ZipFile(zipped, "w") as z:
        z.writestr(name, buffer.getvalue())
    return zipped.getvalue()


def make_navigation(path):
    effective = datetime.now(UTC).date()
    common = {"EFF_DATE": effective.strftime("%Y/%m/%d"), "STATE_CODE": "CA"}
    airports = archive(
        "APT_BASE.csv",
        [
            {
                **common,
                "ARPT_ID": "SFO",
                "ICAO_ID": "KSFO",
                "LAT_DECIMAL": "37.618",
                "LONG_DECIMAL": "-122.375",
                "ARPT_NAME": "SAN FRANCISCO",
            }
        ],
    )
    fixes = archive(
        "FIX_BASE.csv",
        [
            {
                **common,
                "FIX_ID": "VPAAA",
                "FIX_USE_CODE": "VFR",
                "LAT_DECIMAL": "37.7",
                "LONG_DECIMAL": "-122.4",
            }
        ],
    )
    nav = Navigation(path)
    nav.import_archives(airports, fixes, effective)
    return nav


async def test_faa_and_icao_aliases_and_vfr_fixes(tmp_path):
    nav = make_navigation(tmp_path / "nav.json")
    assert await nav.resolve("KSFO") == await nav.resolve("sfo")
    assert (await nav.resolve("VPAAA"))["use"] == "VFR"
    assert (await nav.resolve(Coordinate(latitude=0, longitude=0)))["latitude"] == 0
    with pytest.raises(NmsError, match="Unknown"):
        await nav.resolve("MISSING")
    nav._data["records"]["SFO"].append({"latitude": 30, "longitude": -100})
    with pytest.raises(NmsError, match="Ambiguous"):
        await nav.resolve("SFO")


async def test_airport_radius_uses_reference_coordinate(settings):
    make_navigation(settings.service.navigation_file)

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        assert float(request.url.params["latitude"]) == 37.618
        assert float(request.url.params["longitude"]) == -122.375
        assert float(request.url.params["radius"]) == 25
        assert "location" not in request.url.params
        return httpx.Response(200, json={"status": "Success", "data": {"geojson": []}})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        server = build_server(client=client)
        result = await server.call_tool("notams_near_airport", {"airport": "KSFO", "radius_nm": 25})
        assert not result.isError
        assert result.structuredContent["response"]["center"]["identifier"] == "SFO"


async def test_large_output_saved_not_truncated(settings):
    settings.service.max_tool_characters = 1000
    payload = {"status": "Success", "data": {"geojson": [{"text": "x" * 5000}]}}

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token", "expires_in": "1799"})
        return httpx.Response(200, json=payload)

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        result = await build_server(client=client).call_tool(
            "search_notams",
            {
                "filters": {"location": "KSEA"},
            },
        )
        result = result.structuredContent
        assert result["response_included"] is False
        saved = json.loads(Path(result["saved_file"]).read_text())
        assert saved["response"] == payload


async def test_stdio_handshake_tools_and_safe_status_from_unrelated_cwd(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("""[faa]
key = "test-client"
secret = "test-secret"
environment_url = "https://api-staging.cgifederal-aim.com"
""")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "notam_plugin",
            "--config",
            str(config),
            "serve",
        ],
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {
                "notams_near_airport",
                "notams_along_route",
                "continue_notam_route_search",
                "get_notam_status",
            } <= names
            result = await session.call_tool("get_notam_status", {})
            assert result.structuredContent["configured"] is True
            assert "test-secret" not in result.model_dump_json()
            bad = await session.call_tool("search_notams", {"filters": {"latitude": 0}})
            assert bad.isError


async def test_stdio_starts_without_credentials_and_explains_setup(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "notam_plugin",
            "--config",
            str(tmp_path / "absent.toml"),
            "serve",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_notam_status", {})
            assert result.structuredContent["configured"] is False
            assert "configuration" in result.structuredContent["error"]
