import asyncio
import base64
import gzip
import json

import httpx
import pytest

from notam_plugin.client import NmsClient
from notam_plugin.errors import NmsError
from notam_plugin.models import ChecklistQuery, NotamQuery


def token(value="token-one", expires="1799"):
    return httpx.Response(200, json={"access_token": value, "expires_in": expires})


def success():
    return httpx.Response(200, json={"status": "Success", "data": {"geojson": []}})


async def test_authentication_paths_headers_encoding_and_token_reuse(settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            assert str(request.url) == "https://api-staging.cgifederal-aim.com/v1/auth/token"
            encoded = base64.b64encode(b"test-client:test-secret").decode()
            assert request.headers["authorization"] == f"Basic {encoded}"
            assert request.content == b"grant_type=client_credentials"
            assert "application/x-www-form-urlencoded" in request.headers["content-type"]
            return token()
        assert request.headers["authorization"] == "Bearer token-one"
        assert request.headers["nmsResponseFormat"] == "GEOJSON"
        assert request.url.path == "/nmsapi/v1/notams"
        assert request.url.params["notamNumber"] == "10/108"
        return success()

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        query = NotamQuery(location="KDFW", notamNumber="10/108")
        await client.notams(query)
        await client.notams(query)
    assert len(requests) == 3


async def test_refresh_before_expiry_and_on_401(settings):
    auth_calls = 0
    get_calls = 0

    def handle(request):
        nonlocal auth_calls, get_calls
        if request.method == "POST":
            auth_calls += 1
            return token(f"token-{auth_calls}")
        get_calls += 1
        if get_calls == 2:
            return httpx.Response(401)
        return success()

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        await client.notams(NotamQuery(location="KSEA"))
        await client.notams(NotamQuery(location="KSEA"))
        assert auth_calls == 2
        client._token_expiry = 0
        await client.notams(NotamQuery(location="KSEA"))
    assert auth_calls == 3
    assert get_calls == 4


async def test_concurrent_requests_share_one_token(settings):
    auth_calls = 0

    async def handle(request):
        nonlocal auth_calls
        if request.method == "POST":
            auth_calls += 1
            await asyncio.sleep(0.01)
            return token()
        return success()

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        await asyncio.gather(*[client.notams(NotamQuery(location="KSEA")) for _ in range(8)])
    assert auth_calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"access_token": "bad\nvalue", "expires_in": 10},
        {"access_token": "ok", "expires_in": "NaN"},
    ],
)
async def test_malformed_authentication_is_safe(settings, payload):
    async with NmsClient(
        settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(NmsError, match="invalid authentication") as exc:
            await client.notams(NotamQuery(location="KSEA"))
    assert "test-secret" not in str(exc.value)


async def test_errors_do_not_expose_upstream_body(settings):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(401, text="secret test-secret token-one")
        pytest.fail("Must not query after failed authentication")

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NmsError) as exc:
            await client.notams(NotamQuery(location="KSEA"))
    assert "test-secret" not in str(exc.value)
    assert "token-one" not in str(exc.value)


async def test_faa_429_is_persisted(settings):
    calls = 0

    def handle(request):
        nonlocal calls
        if request.method == "POST":
            return token()
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    for _ in range(2):
        async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(NmsError) as exc:
                await client.notams(NotamQuery(location="KSEA"))
            assert exc.value.retry_after == 60
    assert calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "Failure", "errors": [{"message": "private upstream diagnostics"}]},
        {"status": "Success", "errors": [{"message": "partial"}], "data": {"geojson": []}},
        {"status": "Success"},
        [],
    ],
)
async def test_incomplete_response_is_not_empty_success(settings, payload):
    def handle(request):
        return token() if request.method == "POST" else httpx.Response(200, json=payload)

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NmsError):
            await client.notams(NotamQuery(location="KSEA"))


async def test_classification_only_disables_redirect_and_bulk_limit_shared(settings):
    settings.limits.bulk_interval_seconds = 86400

    def handle(request):
        if request.method == "POST":
            return token()
        assert request.url.params["allowRedirect"] == "false"
        return httpx.Response(307, headers={"location": "/nmsapi/v1/content/YWJj"})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        payload = await client.notams(NotamQuery(classification="DOMESTIC"))
        assert payload["data"]["url"] == "/nmsapi/v1/content/YWJj"
        with pytest.raises(NmsError) as exc:
            await client.initial_load("FDC")
        assert exc.value.retry_after == 86400


@pytest.mark.parametrize(
    "path",
    [
        "/v1/content/YWJj",
        "/nmsapi/v1/content/YWJj",
        "https://api-staging.cgifederal-aim.com/nmsapi/v1/content/YWJj",
    ],
)
async def test_content_download_authentication(settings, path):
    content = gzip.compress(b"<NOTAM>example</NOTAM>")

    def handle(request):
        if request.method == "POST":
            return token()
        assert request.url.path == "/nmsapi/v1/content/YWJj"
        assert request.headers["authorization"] == "Bearer token-one"
        return httpx.Response(200, content=content)

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        payload = await client.download_content(path)
        from pathlib import Path

        assert Path(payload["file"]).read_bytes() == content


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/nmsapi/v1/content/YWJj",
        "//evil.example/v1/content/YWJj",
        "/v1/content/../auth",
        "/v1/content/%2e%2e",
        "/v1/content/id?secret=1",
        "http://api-staging.cgifederal-aim.com/nmsapi/v1/content/YWJj",
        "/v1/notams",
    ],
)
async def test_content_rejects_off_origin_and_traversal_before_network(settings, path):
    def handle(request):
        pytest.fail("An invalid content path must not make an HTTP request")

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NmsError):
            await client.download_content(path)


async def test_content_redirect_never_forwards_token(settings):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if request.method == "POST":
            return token()
        return httpx.Response(307, headers={"location": "https://evil.example/"})

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NmsError):
            await client.download_content("/v1/content/YWJj")
    assert len(seen) == 2
    assert all("evil.example" not in url for url in seen)


async def test_size_limits_and_cleanup(settings):
    settings.service.max_download_bytes = 1000

    def handle(request):
        return token() if request.method == "POST" else httpx.Response(200, content=b"x" * 2000)

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NmsError, match="size limit"):
            await client.download_content("/v1/content/YWJj")
    assert list(settings.service.downloads_directory.iterdir()) == []


async def test_checklist_preserved(settings):
    fixture = {"status": "Success", "data": {"checklist": [{"id": "1234567812345678"}]}}

    def handle(request):
        if request.method == "POST":
            return token()
        assert request.url.path == "/nmsapi/v1/notams/checklist"
        return httpx.Response(200, content=json.dumps(fixture))

    async with NmsClient(settings, transport=httpx.MockTransport(handle)) as client:
        assert await client.checklist(ChecklistQuery(location="KSEA")) == fixture
