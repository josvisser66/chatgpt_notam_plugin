from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .config import StrictModel
from .errors import NmsError


class AltitudeFilter(StrictModel):
    feet: float = Field(ge=-2000, le=100000, description="For FL180 use 18000 feet, reference FL")
    reference: Literal["MSL", "AGL", "FL"] = "MSL"


def parse_upper_limit(value: str) -> tuple[float, str] | None:
    text = re.sub(r"\s+", "", value.upper())
    if text in {"UNL", "UNLIMITED", "UNLIMITEDALTITUDE", "FL999"}:
        return float("inf"), "UNLIMITED"
    if text in {"SFC", "GND"}:
        return 0, "AGL"
    match = re.fullmatch(r"FL(\d{2,3})", text)
    if match:
        return float(match[1]) * 100, "FL"
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(FT|FEET|M)(AMSL|MSL|AGL)?", text)
    if not match:
        return None
    number, unit, reference = match.groups()
    # FAA JO 7930.2: feet without a reference are MSL. Do not assume this for meters.
    if not reference and unit == "M":
        return None
    reference = "MSL" if reference in {None, "AMSL", "MSL"} else "AGL"
    feet = float(number) / 0.3048 if unit == "M" else float(number)
    return feet, reference


_LEVEL = r"(?:SFC|GND|FL\s*\d{2,3}|\d+(?:\.\d+)?\s*(?:FT|M)(?:\s*(?:AMSL|MSL|AGL))?)"
_RANGE = re.compile(rf"(?<!\w){_LEVEL}\s*[-–]\s*({_LEVEL}|UNL)(?!\w)", re.I)


def notam_ceiling(feature: dict) -> tuple[tuple[float, str] | None, str]:
    properties = feature.get("properties") or {}
    notam = (properties.get("coreNOTAMData") or {}).get("notam") or {}
    upper = notam.get("upperLimit")
    if upper is not None and str(upper).strip():
        return parse_upper_limit(str(upper)), "upperLimit"
    # Only parse explicit vertical ranges, never arbitrary obstacle or runway numbers.
    ranges = _RANGE.findall(str(notam.get("text") or ""))
    if ranges:
        parsed = [parse_upper_limit(value) for value in ranges]
        if any(value is None for value in parsed):
            return None, "ambiguous text range"
        if any(value[1] == "UNLIMITED" for value in parsed):
            return (float("inf"), "UNLIMITED"), "text range"
        if len({value[1] for value in parsed}) != 1:
            return None, "mixed altitude references in text"
        return max(parsed, key=lambda value: value[0]), "text range"
    maximum_fl = str(notam.get("maximumFl") or "")
    if re.fullmatch(r"\d{3}", maximum_fl) and maximum_fl != "999":
        return (int(maximum_fl) * 100, "FL"), "maximumFl"
    return None, "no usable upper limit"


def filter_features(features: list[dict], altitude: AltitudeFilter) -> tuple[list[dict], dict]:
    retained, excluded, uncertain = [], [], []
    for feature in features:
        notam = ((feature.get("properties") or {}).get("coreNOTAMData") or {}).get("notam") or {}
        identifier = notam.get("id") or feature.get("id")
        ceiling, source = notam_ceiling(feature)
        if ceiling is None:
            retained.append(feature)
            uncertain.append({"id": identifier, "reason": source})
        elif ceiling[1] == "UNLIMITED":
            retained.append(feature)
        elif ceiling[1] != altitude.reference:
            retained.append(feature)
            uncertain.append(
                {
                    "id": identifier,
                    "reason": f"Upper limit uses {ceiling[1]}; "
                    f"requested altitude uses {altitude.reference}.",
                }
            )
        elif ceiling[0] < altitude.feet:
            excluded.append(
                {
                    "id": identifier,
                    "upper_limit_ft": ceiling[0],
                    "reference": ceiling[1],
                    "source": source,
                }
            )
        else:
            retained.append(feature)
    return retained, {
        "requested": altitude.model_dump(),
        "excluded_below_count": len(excluded),
        "excluded_below": excluded,
        "retained_uncertain_count": len(uncertain),
        "retained_uncertain": uncertain,
        "rule": "Exclude only when the upper limit is strictly below the requested altitude "
        "in the same vertical reference. Equality, unlimited and unknown limits remain. "
        "No terrain or pressure conversion is inferred.",
    }


def filter_response(payload: dict, altitude: AltitudeFilter | None) -> dict:
    if altitude is None:
        return payload
    features = payload.get("data", {}).get("geojson")
    if not isinstance(features, list) or any(not isinstance(f, dict) for f in features):
        raise NmsError("Altitude filtering requires a GeoJSON NOTAM result; use narrower filters.")
    kept, summary = filter_features(features, altitude)
    return {**payload, "data": {**payload["data"], "geojson": kept}, "altitude_filter": summary}
