from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import ConfigurationError, load_settings

TEMPLATE = """# Fill in one or both environments with credentials issued by FAA.
# Missing, empty, or REPLACE_ credentials leave that profile unconfigured.
# Requests prefer production, with staging fallback when production is unavailable.
[faa.production]
key = "REPLACE_WITH_PRODUCTION_FAA_KEY"
secret = "REPLACE_WITH_PRODUCTION_FAA_SECRET"
environment_url = "https://api-nms.aim.faa.gov"
response_format = "GEOJSON"

[faa.staging]
key = "REPLACE_WITH_STAGING_FAA_KEY"
secret = "REPLACE_WITH_STAGING_FAA_SECRET"
environment_url = "https://api-staging.cgifederal-aim.com"
response_format = "GEOJSON"

[service]
state_file = "state/rate-limits.sqlite3"
downloads_directory = "state/downloads"
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="FAA NOTAM local MCP plugin")
    parser.add_argument("--config", type=Path, default=None, help="TOML configuration path")
    parser.add_argument(
        "command", choices=["init", "check-config", "serve"], default="serve", nargs="?"
    )
    args = parser.parse_args()
    # stdout belongs exclusively to MCP while serving. HTTP logs can include content tokens.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.command == "serve":
        from .server import build_server

        build_server(args.config).run(transport="stdio")
        return
    path = (args.config or Path(os.environ.get("NOTAM_CONFIG", "config.toml"))).expanduser()
    if args.command == "init":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(TEMPLATE)
        except OSError:
            parser.exit(
                1, "Could not create configuration. Existing files are never overwritten.\n"
            )
        print(f"Created {path}. Fill in FAA credentials for production and/or staging locally.")
        return
    try:
        settings = load_settings(path)
    except ConfigurationError as exc:
        parser.exit(1, f"{exc}\n")
    status = settings.status()
    print(f"Configuration valid. Preferred environment: {status['environment']}")
    for name, profile in status["environments"].items():
        state = "configured" if profile["configured"] else "not configured"
        print(f"{name}: {state}; {profile.get('environment_url', 'no URL supplied')}")
    print(
        "Availability and credentials have not been tested with FAA; no network request was made."
    )


if __name__ == "__main__":
    main()
