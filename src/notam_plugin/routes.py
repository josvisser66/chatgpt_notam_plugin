from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .altitude import AltitudeFilter, filter_features
from .client import NmsClient
from .errors import NmsError
from .geo import Corridor, covering_circles
from .models import NotamQuery
from .navigation import Navigation, RoutePoint


def atomic_json(path: Path, data: dict) -> None:
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class RouteSearches:
    def __init__(self, api: NmsClient, navigation: Navigation):
        self.api = api
        self.navigation = navigation
        self.directory = api.settings.service.route_search_directory

    async def start(
        self,
        route: list[RoutePoint],
        width_nm: float,
        altitude: AltitudeFilter | None = None,
    ) -> dict:
        resolved = [await self.navigation.resolve(point) for point in route]
        circles = covering_circles(resolved, width_nm)
        identifier = uuid.uuid4().hex
        job = {
            "search_id": identifier,
            "account": self.api.limiter.namespace,
            "environment": self.api.environment,
            "route": resolved,
            "corridor_half_width_nm": width_nm,
            "circles": circles,
            "completed": 0,
            "records": {},
            "started_at": datetime.now(UTC).isoformat(),
            "retrieval_times": [],
            "altitude": altitude.model_dump() if altitude else None,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / f"{identifier}.json", job)
        try:
            return await self.resume(identifier, starting=True)
        except NmsError:
            # No successful circle exists when an availability failure is raised here.
            (self.directory / f"{identifier}.json").unlink(missing_ok=True)
            raise

    @staticmethod
    def read_job(directory: Path, identifier: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise NmsError("Invalid route search ID.")
        path = directory / f"{identifier}.json"
        if not path.is_file():
            raise NmsError("Route search was not found. Start a new route search.")
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or "account" not in job:
                raise ValueError
            return job
        except (ValueError, UnicodeError):
            raise NmsError("Saved route search is invalid. Start a new route search.") from None

    async def resume(self, identifier: str, *, starting: bool = False) -> dict:
        self.read_job(self.directory, identifier)
        path = self.directory / f"{identifier}.json"
        with path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NmsError("This route search is already running.", retry_after=2) from None
            try:
                job = self.read_job(self.directory, identifier)
                if job["account"] != self.api.limiter.namespace:
                    raise NmsError("This search belongs to a different FAA environment or account.")
                return await self._advance(path, job, starting=starting)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def _advance(self, path: Path, job: dict, *, starting: bool = False) -> dict:
        error = None
        retry_after = None
        request_count = 0
        deadline = time.monotonic() + 8
        while job["completed"] < len(job["circles"]):
            if request_count >= self.api.settings.service.route_requests_per_call:
                break
            try:
                circle = job["circles"][job["completed"]]
                payload = await self.api.notams(NotamQuery(**circle), "GEOJSON")
                features = payload["data"].get("geojson")
                if not isinstance(features, list) or any(not isinstance(f, dict) for f in features):
                    raise NmsError("FAA did not return a GeoJSON list for a route search circle.")
            except NmsError as exc:
                if starting and job["completed"] == 0 and exc.fallback_allowed:
                    raise
                if (
                    exc.retry_after
                    and exc.retry_after <= 2
                    and time.monotonic() + exc.retry_after < deadline
                ):
                    await asyncio.sleep(exc.retry_after)
                    continue
                error, retry_after = str(exc), exc.retry_after
                break
            for feature in features:
                core = (feature.get("properties") or {}).get("coreNOTAMData") or {}
                notam = core.get("notam") or {}
                key = notam.get("id") or feature.get("id")
                if not key:
                    key = hashlib.sha256(json.dumps(feature, sort_keys=True).encode()).hexdigest()
                # Later responses replace earlier observations of the same NOTAM.
                job["records"][str(key)] = feature
            job["completed"] += 1
            request_count += 1
            job["retrieval_times"].append(datetime.now(UTC).isoformat())
            atomic_json(path, job)
        if job["completed"] < len(job["circles"]) and not error:
            retry_after = max(1, int(self.api.settings.data_interval))
        corridor = Corridor(job["route"], job["corridor_half_width_nm"])
        matching, unclassified = [], []
        for feature in job["records"].values():
            intersects = corridor.intersects(feature)
            if intersects is True:
                matching.append(feature)
            elif intersects is None:
                unclassified.append(feature)
        altitude_summary = None
        if job.get("altitude"):
            altitude = AltitudeFilter.model_validate(job["altitude"])
            matching, matched_summary = filter_features(matching, altitude)
            unclassified, unknown_summary = filter_features(unclassified, altitude)
            altitude_summary = {
                "matching_notams": matched_summary,
                "unclassified_notams": unknown_summary,
            }
        output = {
            "search_id": job["search_id"],
            "environment": job.get("environment", self.api.environment),
            "route": job["route"],
            "corridor_half_width_nm": job["corridor_half_width_nm"],
            "coverage_complete": job["completed"] == len(job["circles"]),
            "circles_completed": job["completed"],
            "circles_total": len(job["circles"]),
            "started_at": job["started_at"],
            "first_retrieval_at": job["retrieval_times"][0] if job["retrieval_times"] else None,
            "last_retrieval_at": job["retrieval_times"][-1] if job["retrieval_times"] else None,
            "notams": matching,
            "unclassified_notams": unclassified,
            "matching_count": len(matching),
            "unclassified_count": len(unclassified),
            "spatial_tolerance_m": 100,
            "scope": "FAA geospatial candidates intersecting the WGS84 route corridor, including "
            "end caps. Missing/invalid geometries remain unclassified. Results combine "
            "observations at the listed retrieval times; this is not an atomic snapshot.",
        }
        if error:
            output["error"] = error
        if altitude_summary:
            output["altitude_filter"] = altitude_summary
        if retry_after:
            output["retry_after_seconds"] = retry_after
        if not output["coverage_complete"]:
            output["next_action"] = (
                "Call continue_notam_route_search with this search_id after "
                "retry_after_seconds (if supplied). Do not report all NOTAMs yet."
            )
        return output
