from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

EnvironmentSelection = Annotated[
    Literal["auto", "production", "staging"],
    Field(
        description=(
            "Auto prefers production with configured staging fallback for new requests. "
            "Choose staging when the user requests test data, or production for production only. "
            "Explicit selections never fall back. Continuations/downloads retain their origin."
        )
    ),
]
TEST_HOSTS = {
    "api-staging.cgifederal-aim.com",
    "api-fit.cgifederal-aim.com",
    "api-sit.cgifederal-aim.com",
}


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


class FAAProfile(FAAConfig):
    """An unfilled profile is allowed alongside another configured environment."""

    key: SecretStr | None = None
    secret: SecretStr | None = None
    environment_url: str | None = None

    @field_validator("environment_url")
    @classmethod
    def environment_is_origin(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return super().environment_is_origin(value)

    @field_validator("key", "secret")
    @classmethod
    def real_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not raw.strip() or raw.startswith("REPLACE_"):
            return None
        return super().real_secret(value)

    @property
    def configured(self) -> bool:
        return self.key is not None and self.secret is not None and self.environment_url is not None


class FAAEnvironments(StrictModel):
    production: FAAProfile | None = None
    staging: FAAProfile | None = None


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
    faa: FAAConfig | FAAEnvironments
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    def profiles(self) -> dict[str, FAAConfig | FAAProfile]:
        if isinstance(self.faa, FAAConfig):
            name = (
                "staging"
                if urlsplit(self.faa.environment_url).hostname in TEST_HOSTS
                else "production"
            )
            return {name: self.faa}
        return {
            name: profile
            for name in ("production", "staging")
            if (profile := getattr(self.faa, name)) is not None
        }

    def candidates(self, environment: EnvironmentSelection = "auto") -> list[str]:
        if environment not in {"auto", "production", "staging"}:
            raise ConfigurationError("Environment must be auto, production, or staging.")
        configured = {
            name
            for name, p in self.profiles().items()
            if not isinstance(p, FAAProfile) or p.configured
        }
        if environment != "auto":
            if environment not in configured:
                raise ConfigurationError(
                    f"FAA {environment} is not configured with a key, secret, and URL."
                )
            return [environment]
        names = [name for name in ("production", "staging") if name in configured]
        if not names:
            raise ConfigurationError("Configure FAA credentials in faa.production or faa.staging.")
        return names

    def for_environment(self, environment: str) -> Settings:
        self.candidates(environment)
        profile = self.profiles()[environment]
        return Settings(
            faa=FAAConfig.model_validate(profile.model_dump()),
            service=self.service,
            limits=self.limits,
        )

    def status(self, environment: EnvironmentSelection = "auto") -> dict:
        selected = self.candidates(environment)[0]
        profiles = self.profiles()
        environments = {}
        for name in ("production", "staging"):
            profile = profiles.get(name)
            ready = profile is not None and (
                not isinstance(profile, FAAProfile) or profile.configured
            )
            environments[name] = {"configured": ready}
            if profile:
                environments[name].update(
                    {
                        "environment_url": profile.environment_url,
                        "auth_url": profile.token_url
                        if profile.environment_url
                        else profile.auth_url,
                        "credentials": "present; not tested with FAA"
                        if profile.key and profile.secret
                        else "missing or placeholders",
                    }
                )
            if ready:
                environments[name]["data_interval_seconds"] = self.for_environment(
                    name
                ).data_interval
        active = self.for_environment(selected)
        return {
            "configured": True,
            "transport": "stdio",
            "requested_environment": environment,
            "environment": selected,
            "environment_url": active.faa.environment_url,
            "response_format": active.faa.response_format,
            "data_interval_seconds": active.data_interval,
            "credentials": "present; not tested with FAA",
            "availability": "not tested; selection is based on local configuration only",
            "environments": environments,
        }

    @property
    def data_interval(self) -> float:
        if self.limits.data_interval_seconds is not None:
            return self.limits.data_interval_seconds
        if isinstance(self.faa, FAAEnvironments):
            return self.for_environment(self.candidates()[0]).data_interval
        return 1.0 if urlsplit(self.faa.environment_url).hostname in TEST_HOSTS else 180.0


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.environ.get("NOTAM_CONFIG", "config.toml")).expanduser()
    try:
        with config_path.open("rb") as stream:
            settings = Settings.model_validate(tomllib.load(stream))
            settings.candidates()
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
