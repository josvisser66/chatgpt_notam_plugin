from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class ConfigurationError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def validate_url(value: str, *, origin_only: bool = False, local_http: bool = False) -> str:
    url = urlsplit(value)
    try:
        _ = url.port
    except ValueError:
        raise ValueError("URL has an invalid port") from None
    secure = url.scheme == "https" or (
        local_http and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if not secure or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Use an HTTPS URL without credentials, query, or fragment")
    if origin_only and url.path not in {"", "/"}:
        raise ValueError("Use only the environment host, without /nmsapi or /v1")
    return value.rstrip("/")


class FAAConfig(StrictModel):
    key: SecretStr
    secret: SecretStr
    environment_url: str
    auth_url: str | None = None
    response_format: Literal["GEOJSON", "AIXM"] = "GEOJSON"
    timeout_seconds: float = Field(default=12, gt=0, le=15)
    max_response_bytes: int = Field(default=20_000_000, ge=1000, le=100_000_000)

    @field_validator("key", "secret")
    @classmethod
    def real_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or raw.startswith("REPLACE_") or any(ord(c) < 32 for c in raw):
            raise ValueError("Replace the placeholder with your FAA credential")
        return value

    @field_validator("environment_url")
    @classmethod
    def environment_is_origin(cls, value: str) -> str:
        return validate_url(value, origin_only=True)

    @field_validator("auth_url")
    @classmethod
    def secure_auth(cls, value: str | None) -> str | None:
        return validate_url(value) if value else None

    @property
    def token_url(self) -> str:
        return self.auth_url or f"{self.environment_url}/v1/auth/token"

    @property
    def api_url(self) -> str:
        return f"{self.environment_url}/nmsapi"


class ServiceConfig(StrictModel):
    state_file: Path = Path("state/rate-limits.sqlite3")
    downloads_directory: Path = Path("state/downloads")
    max_tool_characters: int = Field(default=90000, ge=1000, le=1_000_000)
    max_download_bytes: int = Field(default=500_000_000, ge=1000)
    navigation_file: Path = Path("state/navigation.json")
    route_search_directory: Path = Path("state/route-searches")
    route_requests_per_call: int = Field(default=5, ge=1, le=20)


class LimitsConfig(StrictModel):
    data_interval_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    content_interval_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    delta_interval_seconds: float = Field(default=180, ge=0, allow_inf_nan=False)
    bulk_interval_seconds: float = Field(default=86400, ge=0, allow_inf_nan=False)


class Settings(StrictModel):
    faa: FAAConfig
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @property
    def data_interval(self) -> float:
        if self.limits.data_interval_seconds is not None:
            return self.limits.data_interval_seconds
        test_hosts = {
            "api-staging.cgifederal-aim.com",
            "api-fit.cgifederal-aim.com",
            "api-sit.cgifederal-aim.com",
        }
        return 1.0 if urlsplit(self.faa.environment_url).hostname in test_hosts else 180.0


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("NOTAM_CONFIG", "config.toml")).expanduser()
    try:
        with config_path.open("rb") as stream:
            settings = Settings.model_validate(tomllib.load(stream))
    except OSError:
        raise ConfigurationError(
            "Cannot read configuration file. Run notam-plugin init first."
        ) from None
    except tomllib.TOMLDecodeError:
        raise ConfigurationError("Invalid TOML in configuration file.") from None
    except ValidationError as exc:
        # Do not include raw inputs or nested config dictionaries containing credentials.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        raise ConfigurationError(f"Invalid configuration fields: {fields}") from None
    for field in ("state_file", "downloads_directory", "navigation_file", "route_search_directory"):
        value = getattr(settings.service, field).expanduser()
        if not value.is_absolute():
            value = config_path.resolve().parent / value
        setattr(settings.service, field, value)
    return settings
