from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import Field

from .config import StrictModel
from .errors import NmsError

NASR_INDEX = "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/"


class Coordinate(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


RoutePoint = Annotated[str, Field(min_length=2, max_length=12)] | Coordinate


class Links(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.links: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)


async def fetch_public_faa(client: httpx.AsyncClient, url: str, limit: int = 60_000_000) -> bytes:
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".faa.gov")
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise NmsError("Navigation downloads must stay on public FAA HTTPS hosts.")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > limit:
                    raise NmsError("FAA navigation download exceeds the size limit.")
                data.extend(chunk)
            return bytes(data)
    raise NmsError("Too many redirects from the FAA navigation service.")


class Navigation:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict | None = None

    def import_archives(self, airports: bytes, fixes: bytes, effective: date) -> dict:
        records: dict[str, list[dict]] = {}
        count = 0
        for archive, member, kind in (
            (airports, "APT_BASE.csv", "airport"),
            (fixes, "FIX_BASE.csv", "waypoint"),
        ):
            with ZipFile(io.BytesIO(archive)) as bundle:
                names = [n for n in bundle.namelist() if n.split("/")[-1] == member]
                if len(names) != 1 or bundle.getinfo(names[0]).file_size > 150_000_000:
                    raise NmsError(f"FAA navigation archive is missing a usable {member}.")
                with bundle.open(names[0]) as stream:
                    reader = csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
                    required = {"LAT_DECIMAL", "LONG_DECIMAL", "EFF_DATE"}
                    required.add("ARPT_ID" if kind == "airport" else "FIX_ID")
                    if not required.issubset(reader.fieldnames or []):
                        raise NmsError(
                            "FAA navigation CSV fields have changed; update the importer."
                        )
                    for row in reader:
                        if row["EFF_DATE"].replace("/", "-") != effective.isoformat():
                            raise NmsError("FAA navigation files do not match the selected cycle.")
                        lat, lon = float(row["LAT_DECIMAL"]), float(row["LONG_DECIMAL"])
                        if not (
                            math.isfinite(lat)
                            and math.isfinite(lon)
                            and -90 <= lat <= 90
                            and -180 <= lon <= 180
                        ):
                            raise NmsError("FAA navigation data contains an invalid coordinate.")
                        identifier = row["ARPT_ID" if kind == "airport" else "FIX_ID"].strip()
                        item = {
                            "identifier": identifier,
                            "latitude": lat,
                            "longitude": lon,
                            "kind": kind,
                            "state": row.get("STATE_CODE", ""),
                            "name": row.get("ARPT_NAME", identifier),
                            "use": row.get("FIX_USE_CODE", "").strip(),
                            "cycle_effective": effective.isoformat(),
                        }
                        aliases = {identifier, row.get("ICAO_ID", "").strip()}
                        for alias in aliases - {""}:
                            records.setdefault(alias.upper(), []).append(item)
                        count += 1
        data = {
            "source": NASR_INDEX,
            "effective": effective.isoformat(),
            "expires": (effective + timedelta(days=28)).isoformat(),
            "downloaded_at": datetime.now(UTC).isoformat(),
            "record_count": count,
            "records": records,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(data), encoding="utf-8")
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self._data = data
        return {k: v for k, v in data.items() if k != "records"}

    async def refresh(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=True) as client:
                html = (await fetch_public_faa(client, NASR_INDEX)).decode()
                today = datetime.now(UTC).date()
                cycles = []
                for href in Links(html).links:
                    match = re.search(r"NASR_Subscription[/_](\d{4}-\d{2}-\d{2})", href)
                    if match and date.fromisoformat(match[1]) <= today:
                        cycles.append((date.fromisoformat(match[1]), urljoin(NASR_INDEX, href)))
                if not cycles:
                    raise NmsError("Cannot locate the current FAA NASR cycle.")
                effective, cycle_url = max(cycles)
                if effective + timedelta(days=28) <= today:
                    raise NmsError("FAA has not published a current navigation cycle at the index.")
                page = (await fetch_public_faa(client, cycle_url)).decode()
                groups = {}
                for href in Links(page).links:
                    for kind in ("APT", "FIX"):
                        if href.upper().endswith(f"_{kind}_CSV.ZIP"):
                            groups[kind] = urljoin(cycle_url, href)
                if set(groups) != {"APT", "FIX"}:
                    raise NmsError("Cannot locate FAA airport and waypoint CSV groups.")
                airports = await fetch_public_faa(client, groups["APT"])
                fixes = await fetch_public_faa(client, groups["FIX"])
                return self.import_archives(airports, fixes, effective)
        except (httpx.HTTPError, ValueError, BadZipFile, KeyError):
            raise NmsError("Could not load the current public FAA navigation data.") from None

    async def ensure_current(self) -> dict:
        async with self._lock:
            if self._data is None and self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    self._data = None
            if not self._data or not (
                self._data.get("effective", "9999")
                <= datetime.now(UTC).date().isoformat()
                < self._data.get("expires", "0000")
            ):
                await self.refresh()
            assert self._data is not None
            return self._data

    async def lookup(self, identifier: str) -> list[dict]:
        data = await self.ensure_current()
        return data["records"].get(identifier.strip().upper(), [])

    async def resolve(self, point: RoutePoint) -> dict:
        if isinstance(point, Coordinate):
            return {**point.model_dump(), "kind": "coordinate"}
        matches = await self.lookup(point)
        if not matches:
            raise NmsError(
                f"Unknown FAA airport or waypoint: {point}. Supply explicit coordinates."
            )
        positions = {(p["latitude"], p["longitude"]) for p in matches}
        if len(positions) > 1:
            raise NmsError(
                f"Ambiguous identifier {point}. Use resolve_notam_location to see candidates, "
                "then supply the intended latitude/longitude."
            )
        return matches[0]
