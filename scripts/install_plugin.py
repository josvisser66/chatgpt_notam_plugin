"""Self-contained personal marketplace installation; no Codex authoring skills required."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PLUGIN_NAME = "faa-notam"
SOURCE = {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"Invalid JSON in {path}; existing files were not replaced.") from None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def write_object(path: Path, value: dict) -> None:
    """Replace one complete file, so an interrupted write cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def marketplace_at(path: Path) -> tuple[dict, bool]:
    marketplace = (
        read_object(path)
        if path.exists()
        else {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    )
    name = marketplace.get("name")
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
        raise ValueError(f"Invalid marketplace name in {path}.")
    if "interface" in marketplace and not isinstance(marketplace["interface"], dict):
        raise ValueError(f"Expected an interface object in {path}.")
    plugins = marketplace.setdefault("plugins", [])
    if not isinstance(plugins, list) or any(not isinstance(p, dict) for p in plugins):
        raise ValueError(f"Expected a plugins array of objects in {path}.")
    matches = [p for p in plugins if p.get("name") == PLUGIN_NAME]
    if len(matches) > 1:
        raise ValueError(f"Duplicate {PLUGIN_NAME} entries in {path}; resolve these first.")
    if matches:
        entry = matches[0]
        if entry.get("source") not in (SOURCE, SOURCE["path"]):
            raise ValueError(
                f"{PLUGIN_NAME} already uses a different source in {path}; "
                "resolve that entry before installing this checkout."
            )
        policy = entry.get("policy", {})
        if not isinstance(policy, dict):
            raise ValueError(f"Expected a policy object for {PLUGIN_NAME} in {path}.")
        if policy.get("installation") == "NOT_AVAILABLE":
            raise ValueError(f"Installation of {PLUGIN_NAME} is disabled by {path}.")
        # Preserve policy, display metadata, ordering, and unrelated plugins verbatim.
        return marketplace, False
    plugins.append(
        {
            "name": PLUGIN_NAME,
            "source": SOURCE,
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    )
    return marketplace, True


def install(source: Path, *, register_only: bool = False) -> None:
    codex = None
    if not register_only:
        codex = shutil.which("codex")
        if not codex:
            raise ValueError(
                "Codex CLI is not on PATH. Use --register-only to install through the Codex app, "
                "or install the Codex CLI and rerun with --install."
            )
        supported = subprocess.run(
            [codex, "plugin", "add", "--help"], capture_output=True, text=True, check=False
        )
        if supported.returncode:
            raise ValueError(
                "This Codex CLI lacks 'plugin add'. Use --register-only and the Codex app, "
                "or update the CLI."
            )

    manifest = read_object(source / ".codex-plugin/plugin.json")
    if source.name != PLUGIN_NAME or manifest.get("name") != PLUGIN_NAME:
        raise ValueError("Unexpected plugin identity; expected faa-notam.")
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("The plugin manifest needs a non-empty version.")
    launcher = read_object(source / ".mcp.json")
    home = Path.home()
    target = home / "plugins" / PLUGIN_NAME
    marketplace_path = home / ".agents/plugins/marketplace.json"
    marketplace, changed = marketplace_at(marketplace_path)
    existing = target / ".codex-plugin/plugin.json"
    if target.exists() and (
        not existing.is_file() or read_object(existing).get("name") != PLUGIN_NAME
    ):
        raise ValueError(f"{target} contains another project; it will not be overwritten.")

    # Refresh only the installed source copy, never the version in the Git checkout.
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    manifest["version"] = f"{version.split('+', 1)[0]}+codex.{stamp}"
    write_object(target / ".mcp.json", launcher)
    write_object(existing, manifest)
    if changed:
        write_object(marketplace_path, marketplace)

    selector = f"{PLUGIN_NAME}@{marketplace['name']}"
    if register_only:
        print(f"Registered {selector} from {source.parents[1]}.")
        print(f"Personal marketplace: {marketplace_path}")
        print(
            "Restart the Codex app, open Plugins, select your personal marketplace, "
            "and install or refresh FAA NOTAM. Then start a new task."
        )
        return
    completed = subprocess.run([codex, "plugin", "add", selector], check=False)
    if completed.returncode:
        raise ValueError(
            f"Codex could not install {selector}. The local source is prepared; "
            "resolve the Codex error above and rerun with --install."
        )
    print(f"Installed {selector} from {source.parents[1]}.")
    print("Start a new Codex task and ask: Check my NOTAM plugin configuration.")
