from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .config import StrictModel

Classification = Literal["INTERNATIONAL", "MILITARY", "LOCAL_MILITARY", "DOMESTIC", "FDC"]
ResponseFormat = Literal["GEOJSON", "AIXM"]
Feature = Literal[
    "RWY",
    "TWY",
    "APRON",
    "AD",
    "OBST",
    "NAV",
    "COM",
    "SVC",
    "AIRSPACE",
    "ODP",
    "SID",
    "STAR",
    "CHART",
    "DATA",
    "DVA",
    "IAP",
    "VFP",
    "ROUTE",
    "SPECIAL",
    "SECURITY",
]
Location = Annotated[str, Field(pattern=r"^[a-zA-Z0-9]{3,4}$")]
Accountability = Annotated[str, Field(pattern=r"^[a-zA-Z0-9]{1,8}$")]


def utc_time(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("Use a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format") from None
    return parsed


def check_lookback(value: str, days: int) -> str:
    stamp = utc_time(value)
    now = datetime.now(UTC)
    if not now - timedelta(days=days) <= stamp <= now:
        raise ValueError(f"lastUpdatedDate must fall within the previous {days * 24} hours")
    return value


class ChecklistQuery(StrictModel):
    accountability: Accountability | None = None
    classification: Classification | None = None
    location: Location | None = None


class NotamQuery(ChecklistQuery):
    effectiveEndDate: str | None = None
    effectiveStartDate: str | None = None
    feature: Feature | None = None
    freeText: Annotated[str, Field(pattern=r"^[ /.\-()\w]{1,80}$")] | None = None
    nmsId: Annotated[str, Field(pattern=r"^[0-9]{16}$")] | None = None
    lastUpdatedDate: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius: float | None = Field(default=None, ge=0, le=100, description="Nautical miles")
    notamNumber: Annotated[str, Field(pattern=r"^[a-zA-Z0-9/]{1,12}$")] | None = None

    @field_validator("effectiveStartDate", "effectiveEndDate")
    @classmethod
    def valid_time(cls, value: str | None) -> str | None:
        if value is not None:
            utc_time(value)
        return value

    @field_validator("lastUpdatedDate")
    @classmethod
    def recent_delta(cls, value: str | None) -> str | None:
        return check_lookback(value, 1) if value is not None else None

    @model_validator(mode="after")
    def relationships(self) -> NotamQuery:
        if not self.model_dump(exclude_none=True):
            raise ValueError("Provide at least one NOTAM filter")
        geo = [self.latitude, self.longitude, self.radius]
        if any(v is not None for v in geo) and not all(v is not None for v in geo):
            raise ValueError("latitude, longitude, and radius must be provided together")
        start, end = self.effectiveStartDate, self.effectiveEndDate
        if (start is None) != (end is None):
            raise ValueError("effectiveStartDate and effectiveEndDate must be provided together")
        if start and end and utc_time(start) > utc_time(end):
            raise ValueError("effectiveStartDate must be at or before effectiveEndDate")
        if self.notamNumber and not (self.location or self.accountability):
            raise ValueError("notamNumber requires location or accountability")
        return self

    @property
    def is_bulk(self) -> bool:
        return set(self.model_dump(exclude_none=True)) == {"classification"}


class LocationSeriesQuery(StrictModel):
    lastUpdatedDate: str | None = None

    @field_validator("lastUpdatedDate")
    @classmethod
    def recent_delta(cls, value: str | None) -> str | None:
        return check_lookback(value, 5) if value is not None else None
