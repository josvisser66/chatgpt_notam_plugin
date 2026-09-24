import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import configure_plugin
    import install_plugin

    home = tmp_path / "another user's home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install_plugin.shutil, "which", lambda name: "/fake cli/codex")
    monkeypatch.setattr(install_plugin.subprocess, "run", run)
    return configure_plugin, install_plugin, home, calls


def checkout(tmp_path, name="a renamed checkout with spaces"):
    root = tmp_path / name
    shutil.copytree(
        ROOT / "plugins/faa-notam/.codex-plugin", root / "plugins/faa-notam/.codex-plugin"
    )
    shutil.copytree(
        ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    (root / "config.toml").write_text('secret = "DO_NOT_COPY_THIS"\n')
    return root


def test_fresh_personal_install_has_only_manifest_and_launcher(installer, tmp_path):
    configure, install, home, calls = installer
    root = checkout(tmp_path)
    original = (root / "plugins/faa-notam/.codex-plugin/plugin.json").read_bytes()
    source = configure.configure(root, root / "config.toml").parent
    install.install(source)

    target = home / "plugins/faa-notam"
    files = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert files == {".mcp.json", ".codex-plugin/plugin.json"}
    assert all("DO_NOT_COPY_THIS" not in p.read_text() for p in target.rglob("*") if p.is_file())
    server = json.loads((target / ".mcp.json").read_text())["mcpServers"]["faa-notam"]
    assert server["command"] == str(root / ".venv/bin/python")  # Not the symlink's system target.
    assert server["args"] == ["-m", "notam_plugin", "--config", str(root / "config.toml"), "serve"]
    assert calls[-1] == ["/fake cli/codex", "plugin", "add", "faa-notam@personal"]
    assert (source / ".codex-plugin/plugin.json").read_bytes() == original
    marketplace = json.loads((home / ".agents/plugins/marketplace.json").read_text())
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/faa-notam"


def test_switch_checkout_refreshes_paths_without_rewriting_marketplace(installer, tmp_path):
    configure, install, home, calls = installer
    first = checkout(tmp_path, "first checkout")
    second = checkout(tmp_path, "second checkout")
    marketplace = home / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    unrelated = {"name": "another-plugin", "source": "./plugins/another-plugin", "custom": True}
    marketplace.write_text(
        json.dumps(
            {
                "name": "my-personal",
                "interface": {"displayName": "My plugins"},
                "plugins": [unrelated],
            }
        )
    )
    install.install(configure.configure(first, first / "config.toml").parent)
    registered = marketplace.read_bytes()
    installed_manifest = home / "plugins/faa-notam/.codex-plugin/plugin.json"
    old_version = json.loads(installed_manifest.read_text())["version"]
    external_config = tmp_path / "separate credentials/config.toml"
    install.install(configure.configure(second, external_config).parent)
    assert marketplace.read_bytes() == registered
    assert json.loads(registered)["plugins"][0] == unrelated
    assert json.loads(registered)["interface"]["displayName"] == "My plugins"
    server = json.loads((home / "plugins/faa-notam/.mcp.json").read_text())["mcpServers"][
        "faa-notam"
    ]
    assert server["command"] == str(second / ".venv/bin/python")
    assert str(external_config) in server["args"]
    assert json.loads(installed_manifest.read_text())["version"] != old_version
    assert calls[-1][-1] == "faa-notam@my-personal"


@pytest.mark.parametrize(
    "contents",
    [
        "not JSON",
        "[]",
        '{"name":"invalid@name","plugins":[]}',
        '{"name":"personal","plugins":{}}',
        json.dumps({"name": "personal", "plugins": [{"name": "faa-notam", "source": "./other"}]}),
    ],
)
def test_invalid_or_conflicting_marketplace_is_not_overwritten(installer, tmp_path, contents):
    configure, install, home, calls = installer
    root = checkout(tmp_path)
    path = home / ".agents/plugins/marketplace.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(ValueError):
        install.install(configure.configure(root, root / "config.toml").parent)
    assert path.read_text() == contents
    assert not (home / "plugins").exists()
    assert len(calls) == 1  # Only the CLI capability check ran.


def test_missing_codex_does_not_change_personal_settings(installer, tmp_path, monkeypatch):
    configure, install, home, calls = installer
    root = checkout(tmp_path)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="not on PATH"):
        install.install(configure.configure(root, root / "config.toml").parent)
    assert list(home.iterdir()) == []
    assert calls == []


def test_codex_failure_is_reported_and_can_be_retried(installer, tmp_path, monkeypatch):
    configure, install, home, _ = installer
    root = checkout(tmp_path)
    source = configure.configure(root, root / "config.toml").parent

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0 if command[-1] == "--help" else 1)

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(ValueError, match="could not install"):
        install.install(source)
    monkeypatch.setattr(
        install.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0)
    )
    install.install(source)
    entries = json.loads((home / ".agents/plugins/marketplace.json").read_text())["plugins"]
    assert len(entries) == 1


@pytest.mark.parametrize("selection", ["checkout", "environment", "argument"])
def test_generator_runs_from_unrelated_directory_and_selects_config(tmp_path, selection):
    root = checkout(tmp_path, "other user/renamed plugin $ with spaces")
    outside = tmp_path / "unrelated working directory"
    outside.mkdir()
    environment = {**os.environ}
    environment.pop("NOTAM_CONFIG", None)
    args = []
    expected = root / "config.toml"
    if selection in {"environment", "argument"}:
        environment["NOTAM_CONFIG"] = str(tmp_path / "from environment.toml")
        expected = Path(environment["NOTAM_CONFIG"])
    if selection == "argument":
        expected = outside / "explicit config.toml"
        args = ["--config", expected.name]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/configure_plugin.py"), *args],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    server = json.loads((root / "plugins/faa-notam/.mcp.json").read_text())["mcpServers"][
        "faa-notam"
    ]
    assert server["command"] == str(root / ".venv/bin/python")
    assert str(expected) in server["args"]


def test_missing_virtual_environment_explains_setup(tmp_path):
    root = checkout(tmp_path)
    (root / ".venv/bin/python").unlink()
    result = subprocess.run(
        [sys.executable, str(root / "scripts/configure_plugin.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Create this checkout's .venv" in result.stderr
    assert not (root / "plugins/faa-notam/.mcp.json").exists()
