from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from notam_plugin.config import ConfigurationError, FAAConfig, LimitsConfig, load_settings
from notam_plugin.errors import NmsError
from notam_plugin.models import LocationSeriesQuery, NotamQuery
from notam_plugin.rate_limit import RateLimiter


def test_config_relative_paths_and_no_secret_output(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""[faa]
key = "my-client"
secret = "my-private-value"
environment_url = "https://api-nms.aim.faa.gov"
""")
    config = load_settings(path)
    assert config.data_interval == 180
    assert config.faa.token_url == "https://api-nms.aim.faa.gov/v1/auth/token"
    assert config.faa.api_url == "https://api-nms.aim.faa.gov/nmsapi"
    assert config.service.state_file == tmp_path / "state/rate-limits.sqlite3"
    assert "my-private-value" not in repr(config)
    path.write_text(path.read_text() + 'unknown = "my-private-value"\n')
    with pytest.raises(ConfigurationError) as exc:
        load_settings(path)
    assert "my-private-value" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example",
        "https://user:password@api.example",
        "https://api.example/nmsapi",
        "https://api.example?query=1",
    ],
)
def test_reject_invalid_environment_urls(url):
    with pytest.raises(ValidationError):
        FAAConfig(key="client", secret="secret", environment_url=url)


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"latitude": 20},
        {"latitude": 20, "longitude": 0},
        {"latitude": 91, "longitude": 0, "radius": 10},
        {"latitude": 0, "longitude": 0, "radius": 101},
        {"notamNumber": "10/108"},
        {"nmsId": "123"},
        {"location": "KSEA", "effectiveStartDate": "2026-09-23T12:00:00Z"},
        {"effectiveStartDate": "2026-09-23T12:00:00Z", "effectiveEndDate": "2026-09-22T12:00:00Z"},
        {"location": "KSEA", "badFilter": "value"},
    ],
)
def test_invalid_filters(query):
    with pytest.raises(ValidationError):
        NotamQuery(**query)


def test_zero_coordinates_are_present():
    assert NotamQuery(latitude=0, longitude=0, radius=0).radius == 0


def test_delta_windows():
    stamp = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValidationError):
        NotamQuery(lastUpdatedDate=stamp)
    assert LocationSeriesQuery(lastUpdatedDate=stamp).lastUpdatedDate == stamp


def test_rate_limit_persists_and_uses_safe_environment_defaults(settings):
    settings.limits = LimitsConfig()
    assert settings.data_interval == 1
    settings.faa.environment_url = "https://api-nms.aim.faa.gov"
    assert settings.data_interval == 180
    RateLimiter(settings).reserve("data")
    with pytest.raises(NmsError) as exc:
        RateLimiter(settings).reserve("data")
    assert exc.value.retry_after == 180


def test_environment_and_account_separate_limits(settings):
    settings.limits = LimitsConfig()
    RateLimiter(settings).reserve("data")
    settings.faa.environment_url = "https://api-nms.aim.faa.gov"
    RateLimiter(settings).reserve("data")
