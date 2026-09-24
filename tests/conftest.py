import pytest

from notam_plugin.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings.model_validate(
        {
            "faa": {
                "key": "test-client",
                "secret": "test-secret",
                "environment_url": "https://api-staging.cgifederal-aim.com",
            },
            "service": {
                "state_file": tmp_path / "limits.sqlite3",
                "downloads_directory": tmp_path / "downloads",
                "navigation_file": tmp_path / "navigation.json",
                "route_search_directory": tmp_path / "routes",
            },
            "limits": {
                "data_interval_seconds": 0,
                "content_interval_seconds": 0,
                "delta_interval_seconds": 0,
                "bulk_interval_seconds": 0,
            },
        }
    )
