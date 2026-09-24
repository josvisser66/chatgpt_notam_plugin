from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from .config import Settings
from .errors import NmsError
from .models import ChecklistQuery, Classification, LocationSeriesQuery, NotamQuery, ResponseFormat
from .rate_limit import RateLimiter


class NmsClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=settings.faa.timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )
        self.limiter = RateLimiter(settings)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> NmsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.http.aclose()

    async def _read_limited(self, response: httpx.Response, limit: int) -> bytes:
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            if len(chunks) + len(chunk) > limit:
                raise NmsError(
                    "FAA response is too large. Narrow the filters or use a bulk download."
                )
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        value = response.headers.get("retry-after", "180")
        try:
            return max(1, math.ceil(float(value)))
        except (ValueError, OverflowError):
            try:
                delta = parsedate_to_datetime(value) - datetime.now(UTC)
                return max(1, math.ceil(delta.total_seconds()))
            except (ValueError, TypeError, OverflowError):
                return 180

    def _check_status(self, response: httpx.Response, *, authentication: bool = False) -> None:
        status = response.status_code
        if status == 429:
            retry = self._retry_after(response)
            self.limiter.defer(retry)
            raise NmsError("FAA rate limit reached.", status_code=429, retry_after=retry)
        if status in {401, 403}:
            message = (
                "FAA authentication failed. Check the configured key, secret, and environment."
                if authentication
                else "FAA rejected access to this endpoint."
            )
            raise NmsError(message)
        if status >= 400 or 300 <= status < 400:
            raise NmsError(
                f"FAA {'authentication' if authentication else 'request'} failed (HTTP {status})."
            )

    async def _access_token(self, rejected_token: str | None = None) -> str:
        async with self._token_lock:
            if rejected_token and self._token == rejected_token:
                self._token = None
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            try:
                async with self.http.stream(
                    "POST",
                    self.settings.faa.token_url,
                    auth=httpx.BasicAuth(
                        self.settings.faa.key.get_secret_value(),
                        self.settings.faa.secret.get_secret_value(),
                    ),
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                ) as response:
                    self._check_status(response, authentication=True)
                    payload = json.loads(await self._read_limited(response, 64000))
                token = payload["access_token"]
                expires = float(payload["expires_in"])
                if (
                    not isinstance(token, str)
                    or not token
                    or not token.isascii()
                    or not token.isprintable()
                    or not math.isfinite(expires)
                    or expires <= 0
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise NmsError("FAA returned an invalid authentication response.") from None
            except httpx.HTTPError:
                raise NmsError("Could not reach the FAA authentication endpoint.") from None
            self._token = token
            self._token_expiry = time.monotonic() + expires - min(60, expires * 0.1)
            return token

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        response_format: ResponseFormat | None = None,
        bulk: bool = False,
        delta: bool = False,
    ) -> dict[str, Any]:
        token = await self._access_token()
        buckets = ["data"] + (["bulk"] if bulk else []) + (["delta"] if delta else [])
        for attempt in range(2):
            self.limiter.reserve(*(buckets if attempt == 0 else ["data"]))
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if response_format:
                headers["nmsResponseFormat"] = response_format
            try:
                async with self.http.stream(
                    "GET",
                    self.settings.faa.api_url + path,
                    params=params,
                    headers=headers,
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        token = await self._access_token(rejected_token=token)
                        continue
                    if response.status_code == 307 and bulk:
                        content_path = response.headers.get("location")
                        if not content_path:
                            raise NmsError("FAA returned a redirect without a content path.")
                        self.content_url(content_path)
                        return {"status": "Success", "data": {"url": content_path}}
                    self._check_status(response)
                    payload = json.loads(
                        await self._read_limited(
                            response,
                            self.settings.faa.max_response_bytes,
                        )
                    )
            except (ValueError, UnicodeError):
                raise NmsError("FAA returned invalid JSON.") from None
            except httpx.HTTPError:
                raise NmsError("FAA request timed out or could not be completed.") from None
            if not isinstance(payload, dict) or payload.get("status") not in {"Success", "Failure"}:
                raise NmsError("FAA returned an unexpected response envelope.")
            if payload["status"] == "Failure" or payload.get("errors"):
                raise NmsError(
                    "FAA reported a failed or incomplete query; no complete result is available."
                )
            if not isinstance(payload.get("data"), dict):
                raise NmsError("FAA response is missing its data object.")
            return payload
        raise NmsError("FAA rejected the renewed access token.")

    async def notams(
        self,
        query: NotamQuery,
        response_format: ResponseFormat | None = None,
    ) -> dict[str, Any]:
        params = query.model_dump(exclude_none=True)
        if query.is_bulk:
            params["allowRedirect"] = "false"
        return await self._get_json(
            "/v1/notams",
            params=params,
            response_format=response_format or self.settings.faa.response_format,
            bulk=query.is_bulk,
            delta=query.lastUpdatedDate is not None,
        )

    async def checklist(self, query: ChecklistQuery) -> dict[str, Any]:
        return await self._get_json(
            "/v1/notams/checklist",
            params=query.model_dump(exclude_none=True),
        )

    async def location_series(self, query: LocationSeriesQuery) -> dict[str, Any]:
        return await self._get_json(
            "/v1/locationseries",
            params=query.model_dump(exclude_none=True),
            delta=query.lastUpdatedDate is not None,
        )

    async def initial_load(self, classification: Classification | None = None) -> dict[str, Any]:
        path = "/v1/notams/il" + (f"/{classification}" if classification else "")
        return await self._get_json(path, params={"allowRedirect": "false"}, bulk=True)

    def content_url(self, content_path: str) -> str:
        """Resolve the two relative forms documented by FAA; never forward auth off-host."""
        parsed = urlsplit(content_path)
        origin = urlsplit(self.settings.faa.environment_url)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise NmsError("Invalid FAA content path.")
        if parsed.scheme or parsed.netloc:
            if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                raise NmsError("Content downloads must use the configured FAA environment.")
        path = parsed.path
        if path.startswith("/nmsapi/"):
            path = path[len("/nmsapi") :]
        prefix = "/v1/content/"
        token = unquote(path[len(prefix) :]) if path.startswith(prefix) else ""
        if not re.fullmatch(r"[A-Za-z0-9_+/=-]+", token):
            raise NmsError("Expected an FAA /nmsapi/v1/content/{token} path.")
        return self.settings.faa.api_url + prefix + quote(token, safe="")

    async def download_content(self, content_path: str) -> dict[str, Any]:
        url = self.content_url(content_path)
        token = await self._access_token()
        directory = self.settings.service.downloads_directory
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"notams-{uuid.uuid4().hex}.gz"
        try:
            for attempt in range(2):
                self.limiter.reserve("content")
                async with self.http.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        token = await self._access_token(rejected_token=token)
                        continue
                    self._check_status(response)
                    size = 0
                    with target.open("xb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.settings.service.max_download_bytes:
                                raise NmsError(
                                    "FAA bulk download exceeded the configured size limit."
                                )
                            stream.write(chunk)
                    return {"file": str(target.resolve()), "bytes": size, "compressed": True}
        except (httpx.HTTPError, OSError):
            target.unlink(missing_ok=True)
            raise NmsError("Could not download or save FAA content.") from None
        except NmsError:
            target.unlink(missing_ok=True)
            raise
        raise NmsError("FAA rejected the renewed access token.")
