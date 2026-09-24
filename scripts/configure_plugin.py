"""Generate a launcher for this checkout and optionally install it in Codex."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def configure(root: Path, config: Path) -> Path:
    root = root.resolve()
    # Keep the venv executable path: resolving its symlink can select system Python.
    python = root / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Create this checkout's .venv and install the package first (see README).")
    launcher = {
        "mcpServers": {
            "faa-notam": {
                "command": str(python),
                "args": [
                    "-m",
                    "notam_plugin",
                    "--config",
                    str(config.expanduser().resolve()),
                    "serve",
                ],
            }
        }
    }
    target = root / "plugins/faa-notam/.mcp.json"
    target.write_text(json.dumps(launcher, indent=2) + "\n", encoding="utf-8")
    return target


def validate_setup(root: Path, config: Path) -> None:
    """Check the selected runtime and credentials locally before registration."""
    python = root / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Create this checkout's .venv and install the package first (see README).")
    try:
        checked = subprocess.run(
            [
                str(python),
                "-c",
                "import sys; from notam_plugin.server import build_server; "
                "from notam_plugin.config import load_settings; load_settings(sys.argv[1])",
                str(config),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Configuration check timed out; recreate .venv and retry setup.") from None
    if checked.returncode:
        raise ValueError(
            "The selected configuration or Python environment is not ready. "
            "Run .venv/bin/python -m pip install -e . and then "
            f'.venv/bin/python -m notam_plugin --config "{config}" check-config for details. '
            "No plugin files were changed."
        )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("NOTAM_CONFIG", root / "config.toml")),
        help="Configuration file (default: NOTAM_CONFIG, then this checkout's config.toml)",
    )
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument(
        "--install",
        action="store_true",
        help="Register in your personal marketplace and install/refresh the plugin in Codex",
    )
    connection.add_argument(
        "--register-only",
        action="store_true",
        help="Register for installation in the Codex app without the Codex CLI",
    )
    args = parser.parse_args()
    try:
        args.config = args.config.expanduser().resolve()
        if args.install or args.register_only:
            validate_setup(root, args.config)
        target = configure(root, args.config)
        print(f"Configured {target}", flush=True)
        if args.install or args.register_only:
            from install_plugin import install

            install(target.parent, register_only=args.register_only)
        else:
            print("Launcher only. Use --install or --register-only to connect the plugin to Codex.")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Setup failed: {exc}\n")


if __name__ == "__main__":
    main()
