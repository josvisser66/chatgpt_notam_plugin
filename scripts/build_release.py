"""Build a source ZIP from an allowlist, excluding local credentials and runtime state."""

from __future__ import annotations

import argparse
import hashlib
import re
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

FILES = (
    ".gitignore",
    "README.md",
    "pyproject.toml",
    "config.example.toml",
    "plugins/faa-notam/.codex-plugin/plugin.json",
    "scripts/configure_plugin.py",
    "scripts/install_plugin.py",
    "scripts/build_release.py",
    "docs/mcp-tools.json",
    "docs/reference/nms-api.yaml",
)


def build_release(root: Path, destination: Path) -> Path:
    root = root.resolve()
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
        raise ValueError("Invalid release version in pyproject.toml")
    members = [root / name for name in FILES]
    for directory in ("src/notam_plugin", "tests"):
        members.extend(sorted((root / directory).glob("*.py")))
    for path in members:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe release file: {path.relative_to(root)}")
    destination.mkdir(parents=True, exist_ok=True)
    name = f"faa-notam-plugin-{version}"
    archive = destination / f"{name}.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path in members:
            bundle.write(path, f"{name}/{path.relative_to(root).as_posix()}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=root / "dist")
    args = parser.parse_args()
    try:
        archive = build_release(root, args.output_dir.expanduser().resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Release build failed: {exc}\n")
    print(f"Created {archive}")
    print(f"Checksum: {archive.with_suffix('.zip.sha256')}")


if __name__ == "__main__":
    main()
