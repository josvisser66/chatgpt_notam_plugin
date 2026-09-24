"""Generate the local plugin's launcher for this checkout; no credentials are copied."""

import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--config", type=Path, default=root / "config.toml")
args = parser.parse_args()
launcher = {
    "mcpServers": {
        "faa-notam": {
            "command": str(root / ".venv/bin/python"),
            "args": [
                "-m",
                "notam_plugin",
                "--config",
                str(args.config.expanduser().resolve()),
                "serve",
            ],
        }
    }
}
target = root / "plugins/faa-notam/.mcp.json"
target.write_text(json.dumps(launcher, indent=2) + "\n")
print(f"Configured {target}")
