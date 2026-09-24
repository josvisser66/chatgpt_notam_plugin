import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import build_release
    import configure_plugin
    import install_plugin

    return build_release, configure_plugin, install_plugin


def test_release_excludes_private_files_and_is_extractable(setup_modules, tmp_path):
    release, _, _ = setup_modules
    source = tmp_path / "source"
    for name in release.FILES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copytree(ROOT / "src/notam_plugin", source / "src/notam_plugin")
    shutil.copytree(ROOT / "tests", source / "tests")
    excluded = (
        "config.toml",
        "config.production.toml",
        ".env",
        "state/downloads/response.json",
        "plugins/faa-notam/.mcp.json",
        ".venv/bin/python",
        ".git/config",
        "src/notam_plugin/credentials.toml",
    )
    private_value = f"private-test-{uuid4().hex}"
    for name in excluded:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(private_value)

    archive = release.build_release(source, tmp_path / "downloads")
    with ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        names = bundle.namelist()
        prefix = archive.stem + "/"
        assert all(name.startswith(prefix) for name in names)
        relative = {name.removeprefix(prefix) for name in names}
        assert set(release.FILES) <= relative
        assert "src/notam_plugin/server.py" in relative
        assert "tests/test_release_setup.py" in relative
        assert not set(excluded) & relative
        assert all("__pycache__" not in name for name in names)
        assert all(private_value.encode() not in bundle.read(name) for name in names)
        bundle.extractall(tmp_path / "extracted folder")
    assert archive.with_suffix(".zip.sha256").read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    extracted = tmp_path / "extracted folder" / archive.stem
    assert (extracted / "scripts/install_plugin.py").is_file()
    assert not (extracted / "config.toml").exists()


def test_release_rejects_symlinks_before_creating_archive(setup_modules, tmp_path):
    release, _, _ = setup_modules
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(ROOT / "pyproject.toml", source / "pyproject.toml")
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE_TEST_CREDENTIAL")
    (source / ".gitignore").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe release file"):
        release.build_release(source, tmp_path / "downloads")
    assert not (tmp_path / "downloads").exists()


def test_app_only_registration_needs_no_cli_and_preserves_other_plugins(
    setup_modules, tmp_path, monkeypatch
):
    _, configure, install = setup_modules
    home = tmp_path / "new user's home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(install.shutil, "which", lambda _: None)

    def no_cli(*args, **kwargs):
        pytest.fail("App-only registration must not call the CLI")

    monkeypatch.setattr(install.subprocess, "run", no_cli)
    root = tmp_path / "download with spaces"
    shutil.copytree(
        ROOT / "plugins/faa-notam/.codex-plugin", root / "plugins/faa-notam/.codex-plugin"
    )
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    config = tmp_path / "private config.toml"
    config.write_text("PRIVATE_TEST_CREDENTIAL")
    source = configure.configure(root, config).parent
    marketplace = home / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    other = {"name": "unrelated", "source": "./plugins/unrelated"}
    marketplace.write_text(json.dumps({"name": "custom-personal", "plugins": [other]}))
    install.install(source, register_only=True)
    first = marketplace.read_bytes()
    install.install(source, register_only=True)
    assert marketplace.read_bytes() == first
    assert json.loads(first)["plugins"][0] == other
    assert len(json.loads(first)["plugins"]) == 2
    target = home / "plugins/faa-notam"
    launcher = json.loads((target / ".mcp.json").read_text())["mcpServers"]["faa-notam"]
    assert launcher["command"] == str(root / ".venv/bin/python")
    assert str(config) in launcher["args"]
    assert all(
        "PRIVATE_TEST_CREDENTIAL" not in p.read_text() for p in target.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize("present", [False, True])
def test_preflight_rejects_missing_or_invalid_config_without_secret_output(
    setup_modules, tmp_path, present, monkeypatch
):
    _, configure, _ = setup_modules
    root = tmp_path / "download"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    # Use the test interpreter's installed dependencies to exercise the real check.
    run = subprocess.run

    def run_with_test_interpreter(command, **kwargs):
        assert command[0] == str(root / ".venv/bin/python")
        return run([sys.executable, *command[1:]], **kwargs)

    monkeypatch.setattr(configure.subprocess, "run", run_with_test_interpreter)
    config = tmp_path / "config.toml"
    if present:
        config.write_text('[faa]\nsecret = "PRIVATE_TEST_CREDENTIAL"\n')
    launcher = root / "plugins/faa-notam/.mcp.json"
    before = launcher.read_bytes() if launcher.exists() else None
    with pytest.raises(ValueError, match="not ready") as error:
        configure.validate_setup(root, config)
    assert "PRIVATE_TEST_CREDENTIAL" not in str(error.value)
    assert (launcher.read_bytes() if launcher.exists() else None) == before
