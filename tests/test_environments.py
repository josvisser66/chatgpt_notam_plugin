import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from notam_plugin.cli import TEMPLATE
from notam_plugin.client import NmsClient
from notam_plugin.config import ConfigurationError, load_settings
from notam_plugin.server import build_server

PRODUCTION = "api-nms.aim.faa.gov"
STAGING = "api-staging.cgifederal-aim.com"


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f"""[faa.production]
key = "production-client"
secret = "production-secret"
environment_url = "https://{PRODUCTION}"
[faa.staging]
key = "staging-client"
secret = "staging-secret"
environment_url = "https://{STAGING}"
[limits]
data_interval_seconds = 0
content_interval_seconds = 0
bulk_interval_seconds = 0
[service]
route_requests_per_call = 1
""")
    return path


@pytest.fixture
async def harness(config_file):
    clients, requests = [], []

    def create(handler=None):
        def respond(request):
            requests.append(request)
            name = "production" if request.url.host == PRODUCTION else "staging"
            if request.method == "POST":
                credential = base64.b64encode(f"{name}-client:{name}-secret".encode()).decode()
                assert request.headers["authorization"] == f"Basic {credential}"
            else:
                assert request.headers["authorization"] == f"Bearer {name}-token"
            if handler:
                response = handler(request)
                if response is not None:
                    return response
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"{name}-token",
                        "expires_in": 3600,
                    },
                )
            return httpx.Response(200, json={"status": "Success", "data": {"geojson": []}})

        def factory(settings):
            client = NmsClient(settings, transport=httpx.MockTransport(respond))
            clients.append(client)
            return client

        return build_server(config_file, client_factory=factory)

    yield create, requests
    for client in clients:
        await client.http.aclose()


def test_dual_profiles_secrets_defaults_and_status(config_file):
    config_file.write_text(config_file.read_text().replace("data_interval_seconds = 0\n", ""))
    settings = load_settings(config_file)
    assert settings.candidates() == ["production", "staging"]
    assert settings.for_environment("production").data_interval == 180
    assert settings.for_environment("staging").data_interval == 1
    assert (
        settings.for_environment("production").faa.token_url
        == f"https://{PRODUCTION}/v1/auth/token"
    )
    assert settings.for_environment("staging").faa.token_url == f"https://{STAGING}/v1/auth/token"
    status = settings.status()
    assert status["environment"] == "production"
    assert status["environments"]["staging"]["configured"]
    assert "production-secret" not in json.dumps(status)
    assert "staging-secret" not in repr(settings)


@pytest.mark.parametrize("credential", ["", "REPLACE_WITH_PRODUCTION_FAA_KEY"])
def test_unfilled_production_is_skipped(config_file, credential):
    config_file.write_text(config_file.read_text().replace("production-client", credential))
    settings = load_settings(config_file)
    assert settings.candidates() == ["staging"]
    assert not settings.status()["environments"]["production"]["configured"]
    with pytest.raises(ConfigurationError, match="production is not configured"):
        settings.candidates("production")


def test_missing_production_url_is_unconfigured(config_file):
    config_file.write_text(
        config_file.read_text().replace(f'environment_url = "https://{PRODUCTION}"', "")
    )
    settings = load_settings(config_file)
    assert settings.candidates() == ["staging"]
    assert not settings.status()["environments"]["production"]["configured"]


def test_per_profile_urls_and_custom_staging_role(config_file):
    config_file.write_text(
        config_file.read_text()
        .replace(f"https://{STAGING}", "https://custom-test.example")
        .replace("[faa.staging]", '[faa.staging]\nauth_url = "https://login.example/token"')
    )
    settings = load_settings(config_file)
    staging = settings.for_environment("staging")
    assert staging.faa.token_url == "https://login.example/token"
    assert settings.candidates("staging") == ["staging"]


@pytest.mark.parametrize(
    "bad",
    [
        'environment_url = "http://unsafe.example"',
        'unknown = "private-value"',
    ],
)
def test_invalid_profile_does_not_hide_configuration_errors(config_file, bad):
    config_file.write_text(
        config_file.read_text().replace(f'environment_url = "https://{PRODUCTION}"', bad)
    )
    with pytest.raises(ConfigurationError) as exc:
        load_settings(config_file)
    assert "private-value" not in str(exc.value)
    assert "production-secret" not in str(exc.value)


def test_legacy_and_nested_configuration_cannot_mix(config_file):
    config_file.write_text('[faa]\nkey = "legacy-secret"\n' + config_file.read_text())
    with pytest.raises(ConfigurationError):
        load_settings(config_file)


@pytest.mark.parametrize("template", [TEMPLATE, Path("config.example.toml").read_text()])
def test_templates_allow_either_or_both_profiles(tmp_path, template):
    config = tmp_path / "config.toml"
    config.write_text(template)
    with pytest.raises(ConfigurationError):
        load_settings(config)
    for name in ("PRODUCTION", "STAGING"):
        configured = template.replace(f"REPLACE_WITH_{name}_FAA_KEY", f"{name}-key").replace(
            f"REPLACE_WITH_{name}_FAA_SECRET", f"{name}-secret"
        )
        config.write_text(configured)
        assert load_settings(config).candidates() == [name.lower()]


async def search(server, environment=None):
    arguments = {"filters": {"location": "KPAO"}}
    if environment is not None:
        arguments["environment"] = environment
    return await server.call_tool("search_notams", arguments)


async def test_auto_prefers_production_and_keeps_empty_success(harness):
    create, requests = harness
    result = await search(create())
    assert not result.isError
    assert result.structuredContent["environment"] == "production"
    assert "fallback" not in result.structuredContent
    assert {r.url.host for r in requests} == {PRODUCTION}


async def test_missing_production_falls_back_without_network_probe(config_file, harness):
    config_file.write_text(config_file.read_text().replace("production-client", ""))
    create, requests = harness
    result = await search(create())
    assert not result.isError
    payload = result.structuredContent
    assert payload["environment"] == "staging"
    assert "not configured" in payload["fallback"]["reason"]
    assert {r.url.host for r in requests} == {STAGING}


@pytest.mark.parametrize(
    "failure", ["auth401", "auth400", "data403", "data503", "timeout", "malformed"]
)
async def test_unavailable_production_falls_back_with_provenance(harness, failure):
    create, requests = harness

    def handle(request):
        if request.url.host != PRODUCTION:
            return None
        if failure.startswith("auth") and request.method == "POST":
            return httpx.Response(int(failure[4:]), text="production-secret")
        if request.method != "POST":
            if failure == "timeout":
                raise httpx.ReadTimeout("production-secret", request=request)
            if failure == "malformed":
                return httpx.Response(200, content=b"private malformed response")
            if failure.startswith("data"):
                return httpx.Response(int(failure[4:]), text="production-secret")
        return None

    result = await search(create(handle))
    assert not result.isError
    payload = result.structuredContent
    assert payload["environment"] == "staging"
    assert payload["requested_environment"] == "auto"
    assert payload["environment_url"] == f"https://{STAGING}"
    assert payload["fallback"]["from"] == "production"
    assert requests[0].url.host == PRODUCTION
    assert requests[-1].url.host == STAGING
    assert "production-secret" not in result.model_dump_json()


@pytest.mark.parametrize("environment,host", [("production", PRODUCTION), ("staging", STAGING)])
async def test_explicit_environment_never_falls_back(harness, environment, host):
    create, requests = harness
    result = await search(create(lambda _: httpx.Response(503)), environment)
    assert result.isError
    assert {r.url.host for r in requests} == {host}
    assert result.structuredContent["environment"] == environment
    assert "fallback" not in result.structuredContent


async def test_explicit_staging_is_request_scoped_and_tokens_are_isolated(harness):
    create, requests = harness
    server = create()
    results = await asyncio.gather(search(server, "staging"), search(server))
    assert [r.structuredContent["environment"] for r in results] == ["staging", "production"]
    await search(server, "staging")
    await search(server)
    assert len([r for r in requests if r.method == "POST"]) == 2


@pytest.mark.parametrize("status", [400, 404, 429])
async def test_query_errors_and_rate_limits_do_not_fall_back(harness, status):
    create, requests = harness

    def handle(request):
        if request.method != "POST":
            return httpx.Response(status, headers={"Retry-After": "90"})

    result = await search(create(handle))
    assert result.isError
    assert {r.url.host for r in requests} == {PRODUCTION}
    if status == 429:
        assert result.structuredContent["retry_after_seconds"] == 90


async def test_local_rate_limit_does_not_fall_back(config_file, harness):
    config_file.write_text(
        config_file.read_text().replace("data_interval_seconds = 0", "data_interval_seconds = 180")
    )
    create, requests = harness
    server = create()
    assert not (await search(server)).isError
    result = await search(server)
    assert result.isError
    assert result.structuredContent["retry_after_seconds"] > 0
    assert {r.url.host for r in requests} == {PRODUCTION}


async def test_both_failures_are_reported_without_credentials(harness):
    create, requests = harness
    result = await search(create(lambda _: httpx.Response(503, text="staging-secret")))
    assert result.isError
    assert result.structuredContent["fallback"]["from"] == "production"
    assert result.structuredContent["environment"] == "staging"
    assert {r.url.host for r in requests} == {PRODUCTION, STAGING}
    assert "staging-secret" not in result.model_dump_json()


async def test_later_auto_request_retries_production_after_fallback(harness):
    create, requests = harness
    failed = False

    def handle(request):
        nonlocal failed
        if request.url.host == PRODUCTION and not failed:
            failed = True
            return httpx.Response(503)

    server = create(handle)
    assert (await search(server)).structuredContent["environment"] == "staging"
    requests.clear()
    assert (await search(server)).structuredContent["environment"] == "production"
    assert {r.url.host for r in requests} == {PRODUCTION}


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("get_notam_checklist", {"filters": {"location": "KPAO"}}),
        ("get_location_series", {"filters": {}}),
        ("get_notam_initial_load", {}),
    ],
)
async def test_other_faa_tools_honor_explicit_staging(harness, name, arguments):
    create, requests = harness
    result = await create().call_tool(name, {**arguments, "environment": "staging"})
    assert not result.isError
    assert result.structuredContent["environment"] == "staging"
    assert {r.url.host for r in requests} == {STAGING}


async def test_status_checks_both_profiles_without_contacting_faa(harness):
    create, requests = harness
    result = await create().call_tool("get_notam_status", {})
    assert result.structuredContent["environment"] == "production"
    assert result.structuredContent["environments"]["staging"]["configured"]
    assert requests == []


async def test_missing_explicit_staging_never_contacts_production(config_file, harness):
    config_file.write_text(config_file.read_text().replace("staging-client", ""))
    create, requests = harness
    result = await search(create(), "staging")
    assert result.isError
    assert "staging is not configured" in result.structuredContent["error"]
    assert requests == []


async def test_invalid_environment_rejected_before_network(harness):
    create, requests = harness
    with pytest.raises(ToolError, match="environment"):
        await search(create(), "prodution")
    assert requests == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"content_path": "/v1/content/YWJj", "environment": "staging"},
        {"content_path": f"https://{STAGING}/nmsapi/v1/content/YWJj"},
    ],
)
async def test_content_download_stays_on_origin_environment(harness, arguments):
    create, requests = harness

    def handle(request):
        if request.method != "POST":
            return httpx.Response(200, content=b"compressed-source")

    result = await create(handle).call_tool("download_notam_content", arguments)
    assert not result.isError
    assert result.structuredContent["environment"] == "staging"
    assert {r.url.host for r in requests} == {STAGING}


async def test_relative_content_needs_environment_when_both_configured(harness):
    create, requests = harness
    result = await create().call_tool(
        "download_notam_content", {"content_path": "/v1/content/YWJj"}
    )
    assert result.isError
    assert requests == []


async def test_content_download_cannot_fall_back(harness):
    create, requests = harness
    result = await create(lambda _: httpx.Response(503)).call_tool(
        "download_notam_content",
        {
            "content_path": f"https://{PRODUCTION}/nmsapi/v1/content/YWJj",
        },
    )
    assert result.isError
    assert {r.url.host for r in requests} == {PRODUCTION}


async def test_content_host_mismatch_is_rejected_before_auth(harness):
    create, requests = harness
    result = await create().call_tool(
        "download_notam_content",
        {
            "content_path": f"https://{STAGING}/nmsapi/v1/content/YWJj",
            "environment": "production",
        },
    )
    assert result.isError
    assert requests == []


ROUTE = {
    "route": [{"latitude": 0, "longitude": 0}, {"latitude": 0, "longitude": 2}],
    "corridor_half_width_nm": 5,
}


async def test_route_fallback_before_first_circle_and_pinned_resume_after_restart(harness):
    create, requests = harness

    def handle(request):
        if request.url.host == PRODUCTION:
            return httpx.Response(503)

    result = await create(handle).call_tool("notams_along_route", ROUTE)
    payload = result.structuredContent
    assert not result.isError
    assert payload["environment"] == "staging"
    assert payload["fallback"]["from"] == "production"
    assert payload["response"]["circles_completed"] == 1
    search_id = payload["response"]["search_id"]
    requests.clear()
    server = create()
    result = await server.call_tool("continue_notam_route_search", {"search_id": search_id})
    assert result.structuredContent["environment"] == "staging"
    assert result.structuredContent["response"]["coverage_complete"]
    assert {r.url.host for r in requests} == {STAGING}
    result = await server.call_tool(
        "continue_notam_route_search",
        {
            "search_id": search_id,
            "environment": "production",
        },
    )
    assert result.isError
    assert "different FAA environment" in result.structuredContent["error"]


async def test_partial_route_never_mixes_environments(config_file, harness):
    config_file.write_text(
        config_file.read_text().replace(
            "route_requests_per_call = 1", "route_requests_per_call = 5"
        )
    )
    create, requests = harness
    count = 0

    def handle(request):
        nonlocal count
        if request.method != "POST":
            count += 1
            if count > 1:
                return httpx.Response(503)

    result = await create(handle).call_tool("notams_along_route", ROUTE)
    payload = result.structuredContent
    assert not result.isError
    assert payload["environment"] == "production"
    assert payload["response"]["circles_completed"] == 1
    assert not payload["response"]["coverage_complete"]
    assert "error" in payload["response"]
    assert "fallback" not in payload
    assert {r.url.host for r in requests} == {PRODUCTION}


async def test_legacy_saved_route_is_bound_by_account(harness, config_file):
    create, requests = harness
    result = await create().call_tool("notams_along_route", {**ROUTE, "environment": "staging"})
    search_id = result.structuredContent["response"]["search_id"]
    path = config_file.parent / "state/route-searches" / f"{search_id}.json"
    job = json.loads(path.read_text())
    del job["environment"]
    path.write_text(json.dumps(job))
    requests.clear()
    result = await create().call_tool("continue_notam_route_search", {"search_id": search_id})
    assert not result.isError
    assert result.structuredContent["environment"] == "staging"
    assert {r.url.host for r in requests} == {STAGING}


async def test_every_tool_exposes_environment_selection(harness):
    create, _ = harness
    tools = await create().list_tools()
    for tool in tools:
        schema = tool.inputSchema["properties"]["environment"]
        assert schema["enum"] == ["auto", "production", "staging"]
        assert schema["default"] == "auto"
