from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import ConfigurationError, load_settings

TEMPLATE = """[faa]
# KEY and SECRET provided by FAA. Enter them locally; do not paste them into chat.
key = "REPLACE_WITH_FAA_KEY"
secret = "REPLACE_WITH_FAA_SECRET"

# Host only, without /nmsapi or /v1. This defaults to pre-production.
environment_url = "https://api-staging.cgifederal-aim.com"
# Production: https://api-nms.aim.faa.gov
# FIT: https://api-fit.cgifederal-aim.com
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
        print(f"Created {path}. Fill in the FAA key and secret locally.")
        return
    try:
        settings = load_settings(path)
    except ConfigurationError as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Configuration valid. FAA environment: {settings.faa.environment_url}")
    print("Credentials are present; no network request was made.")


if __name__ == "__main__":
    main()
