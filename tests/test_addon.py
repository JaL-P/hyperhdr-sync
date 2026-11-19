"""Checks that the Home Assistant add-on's options match the standalone bridge."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hyperhdr_sync.engine import AlbumMode, Effect, Palette, Response

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "hyperhdr_sync_addon"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _config() -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load((ADDON / "config.yaml").read_text(encoding="utf-8"))
    return config


def _choices(schema_type: str) -> set[str]:
    match = re.fullmatch(r"list\((.*)\)", schema_type)
    assert match, schema_type
    return set(match.group(1).split("|"))


def test_config_is_complete() -> None:
    """The add-on has its required keys and every default fits its schema."""
    config = _config()
    for key in ("name", "version", "slug", "description", "arch"):
        assert config[key]
    assert set(config["arch"]) <= {"aarch64", "amd64"}
    assert config["init"] is False
    assert set(config["options"]) <= set(config["schema"])
    # the HyperHDR address has no default, so the add-on waits until it is set
    assert "hyperhdr_host" not in config["options"]
    # the default used for building by hand follows the add-on version
    dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG BUILD_VERSION={config['version']}\n" in dockerfile
    # The code comes from main, and the version in the download step makes each new version
    # download it again instead of reusing Docker's cached layer.
    assert "ARG BUILD_REF=main\n" in dockerfile
    assert "refs/heads/${BUILD_REF}.tar.gz" in dockerfile
    assert '"Building add-on ${BUILD_VERSION} from ${BUILD_REF}"' in dockerfile


def test_choices_match_engine() -> None:
    """The add-on offers exactly the engine's effects, palettes and responses."""
    schema = _config()["schema"]
    assert _choices(schema["effect"]) == {effect.value for effect in Effect}
    assert _choices(schema["palette"]) == {palette.value for palette in Palette}
    assert _choices(schema["response"]) == {response.value for response in Response}
    assert _choices(schema["album_mode"]) == {mode.value for mode in AlbumMode}


def test_translations_cover_schema() -> None:
    """Every option has a label."""
    translations = yaml.safe_load((ADDON / "translations" / "en.yaml").read_text(encoding="utf-8"))
    assert set(translations["configuration"]) == set(_config()["schema"])


def test_options_parse_as_bridge_arguments() -> None:
    """The default options plus an address form a valid bridge command line, without the token."""
    run = _load("addon_run", ADDON / "run.py")
    bridge = _load("hyperhdr_bridge", ROOT / "standalone" / "hyperhdr_bridge.py")
    options = {**_config()["options"], "hyperhdr_host": " 192.168.1.20 ", "token": "secret"}
    argv = run.options_to_argv(options)
    assert "secret" not in argv
    assert all(run.options_to_argv({**options, "token": ""}))
    args = bridge.parse_args(argv)
    assert args.hyperhdr_host == "192.168.1.20"
    assert args.server == "ws://127.0.0.1:8927/sendspin"
    assert args.state_dir == "/data/bridge"
    assert args.sensitivity == 0.0
    assert args.log_level == "INFO"
    assert args.color == (255, 147, 41)
    assert args.night_light == 0
    assert args.colors == ()
    assert args.steps is False
    assert args.token is None
    assert args.ui_supervisor, "the add-on always serves its Ingress panel"


def test_own_palette_options_reach_the_bridge() -> None:
    """The palette made in the web UI reaches the bridge; the saved palettes stay out of argv."""
    run = _load("addon_run", ADDON / "run.py")
    bridge = _load("hyperhdr_bridge", ROOT / "standalone" / "hyperhdr_bridge.py")
    options = {
        **_config()["options"],
        "hyperhdr_host": "192.168.1.20",
        "palette": "mine",
        "colors": ["#ff0000", "#0000ff"],
        "steps": True,
        "my_palettes": [{"name": "Two", "colors": "#ff0000,#0000ff", "steps": True}],
    }
    argv = run.options_to_argv(options)
    assert "Two" not in " ".join(argv)
    args = bridge.parse_args(argv)
    assert args.palette == "mine"
    assert args.colors == ((255, 0, 0), (0, 0, 255))
    assert args.steps is True


def test_supervisor_token_is_read_from_the_s6_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6 overlay withholds container variables; the launcher reads the token from its file."""
    run = _load("addon_run", ADDON / "run.py")
    (tmp_path / "SUPERVISOR_TOKEN").write_text("abc123\n", encoding="utf-8")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    run.load_container_environment(tmp_path)
    assert os.environ["SUPERVISOR_TOKEN"] == "abc123"  # noqa: S105 - test value
    monkeypatch.setenv("SUPERVISOR_TOKEN", "from-env")
    run.load_container_environment(tmp_path)
    assert os.environ["SUPERVISOR_TOKEN"] == "from-env"  # noqa: S105 - test value
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    run.load_container_environment(tmp_path / "missing")
    assert "SUPERVISOR_TOKEN" not in os.environ
