"""Tests for the standalone bridge's web UI."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hyperhdr_sync.engine import AlbumMode, Effect, Engine, EngineSettings, Palette, Response
from hyperhdr_sync.layout import LedLayout

_STANDALONE = Path(__file__).resolve().parents[1] / "standalone"
sys.path.insert(0, str(_STANDALONE))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _STANDALONE / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge_module = _load("hyperhdr_bridge")
web_ui = _load("web_ui")

GOOD = {
    "effect": "pulse",
    "palette": "fire",
    "brightness": 60,
    "sensitivity": 3,
    "response": "smooth",
    "latency_ms": 200,
    "color": "#ff3b1f",
    "night_light": 30,
    "album_mode": "accent",
}
# What the page gets back for GOOD: the values as numbers, and no palette of the user's own.
APPLIED = {
    **GOOD,
    "sensitivity": 3.0,
    "colors": [],
    "steps": False,
    "my_palettes": [],
    "milkdrop_preset": "",
}


class _NullSender:
    host = "127.0.0.1"
    port = 0

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def send(self, rgb: bytes) -> None:
        self.frames.append(rgb)


def _bridge() -> Any:
    layout = LedLayout.strip(12)
    engine = Engine(layout, EngineSettings(effect=Effect.SPECTRUM, palette=Palette.OCEAN))
    return bridge_module.Bridge(engine, _NullSender(), 120)


async def _client(ui: Any) -> TestClient[web.Request, web.Application]:
    client = TestClient(TestServer(ui.make_app()))
    await client.start_server()
    return client


def test_parse_settings_checks_values() -> None:
    """Settings from the page are validated and converted to engine settings."""
    settings = web_ui.parse_settings(GOOD)
    assert settings.engine == EngineSettings(
        effect=Effect.PULSE,
        palette=Palette.FIRE,
        brightness=0.6,
        sensitivity_db=3.0,
        response=Response.SMOOTH,
        color=(255, 59, 31),
        album_mode=AlbumMode.ACCENT,
    )
    assert settings.latency_ms == 200
    assert settings.night_light == 0.3
    assert web_ui.settings_to_dict(settings) == APPLIED
    for bad in (
        {**GOOD, "effect": "strobe"},
        {**GOOD, "brightness": 101},
        {**GOOD, "sensitivity": -13},
        {**GOOD, "latency_ms": "soon"},
        {**GOOD, "color": "red"},
        {**GOOD, "night_light": 101},
        {**GOOD, "album_mode": "sepia"},
        {k: v for k, v in GOOD.items() if k != "palette"},
        ["not", "an", "object"],
        {**GOOD, "palette": "mine", "colors": ["#ff0000"]},
        {**GOOD, "colors": ["#ff0000"] * 7},
        {**GOOD, "colors": ["red", "blue"]},
        {**GOOD, "my_palettes": [{"name": "", "colors": "#ff0000,#0000ff"}]},
        {**GOOD, "my_palettes": [{"name": "One", "colors": "#ff0000"}]},
        {**GOOD, "my_palettes": [{"name": "x", "colors": "#ff0000,#0000ff"}] * 13},
        {**GOOD, "my_palettes": "Movie night"},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            web_ui.parse_settings(bad)


def test_own_palettes_round_trip() -> None:
    """A palette of the user's own reaches the engine, and saved palettes come back unchanged."""
    mine = {
        **GOOD,
        "palette": "mine",
        "colors": ["#ff0000", "#00ff00", "#0000ff"],
        "steps": True,
        "my_palettes": [
            {"name": "Movie night", "colors": "#ff0000,#00ff00,#0000ff", "steps": True},
            {"name": "Sea", "colors": ["#003366", "#00ccff"], "steps": False},
        ],
    }
    settings = web_ui.parse_settings(mine)
    assert settings.engine.palette == Palette.MINE
    assert settings.engine.colors == ((255, 0, 0), (0, 255, 0), (0, 0, 255))
    assert settings.engine.steps is True
    assert [p.name for p in settings.my_palettes] == ["Movie night", "Sea"]
    back = web_ui.settings_to_dict(settings)
    assert back["colors"] == mine["colors"]
    assert back["my_palettes"][1] == {"name": "Sea", "colors": "#003366,#00ccff", "steps": False}
    assert web_ui.parse_settings(back) == settings


async def test_page_state_and_apply() -> None:
    """The page loads, shows the current settings and applying changes the real LEDs."""
    bridge = _bridge()
    client = await _client(web_ui.WebUi(bridge, bridge_module.demo_features))
    try:
        page = await client.get("/")
        assert page.status == 200
        assert "HyperHDR Sync" in await page.text()

        state = await (await client.get("/api/state")).json()
        assert len(state["leds"]["x"]) == 12
        assert state["settings"]["effect"] == "spectrum"
        assert state["saves"] is False
        assert state["playing"] is False
        assert state["source"] == "connecting"
        assert state["player"] == "HyperHDR LEDs"
        assert state["version"] is None
        assert {p["id"] for p in state["palettes"]} == {p.value for p in Palette} - {"mine"}
        assert {m["id"] for m in state["album_modes"]} == {m.value for m in AlbumMode}
        assert all(len(p["colors"]) >= 2 for p in state["palettes"])

        bad = await client.post("/api/apply", json={**GOOD, "brightness": 500})
        assert bad.status == 400
        assert bridge.engine.settings.effect == Effect.SPECTRUM

        result = await (await client.post("/api/apply", json=GOOD)).json()
        assert result == {"settings": APPLIED, "saved": False}
        assert bridge.engine.settings.effect == Effect.PULSE
        assert bridge.latency_ms == 200
        assert bridge.night_light == 0.3
        assert bridge.engine.settings.color == (255, 59, 31)
    finally:
        await client.close()


async def test_preview_streams_without_touching_the_leds() -> None:
    """The websocket streams live and preview frames; previewing never changes the LEDs."""
    bridge = _bridge()
    client = await _client(web_ui.WebUi(bridge, bridge_module.demo_features))
    try:
        ws = await client.ws_connect("/api/ws")
        await ws.send_json({"type": "preview", "settings": GOOD})
        frames: list[bytes] = []
        async with asyncio.timeout(5):
            while len(frames) < 20:
                message = await ws.receive()
                if message.type == aiohttp.WSMsgType.BINARY:
                    frames.append(message.data)
        assert all(len(frame) == 12 * 3 * 2 for frame in frames)
        assert any(any(frame[36:]) for frame in frames), "the preview plays the test beat"
        assert bridge.preview is not None
        assert bridge.preview.settings.effect == Effect.PULSE
        assert bridge.engine.settings.effect == Effect.SPECTRUM
        assert not bridge.sender.frames, "nothing was sent to HyperHDR"

        await ws.send_json({"type": "preview", "settings": {**GOOD, "effect": "strobe"}})
        async with asyncio.timeout(5):
            while (message := await ws.receive()).type != aiohttp.WSMsgType.TEXT or (
                "error" not in message.data
            ):
                pass
        await ws.close()
        async with asyncio.timeout(5):
            while bridge.preview is not None:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
    finally:
        await client.close()


def test_version_comes_from_the_addon_config() -> None:
    """Without the Supervisor, the page shows the add-on version of this checkout."""
    config = (Path(__file__).parent.parent / "hyperhdr_sync_addon" / "config.yaml").read_text()
    assert f'version: "{web_ui.repo_version()}"' in config


async def test_page_shows_the_version() -> None:
    """The version handed to the web UI reaches the page."""
    client = await _client(web_ui.WebUi(_bridge(), bridge_module.demo_features, version="9.8.7"))
    try:
        assert (await (await client.get("/api/state")).json())["version"] == "9.8.7"
    finally:
        await client.close()


def test_milkdrop_preset_round_trips() -> None:
    """The MilkDrop preset is a setting like the others; an absurdly long name is refused."""
    settings = web_ui.parse_settings({**GOOD, "effect": "milkdrop", "milkdrop_preset": " martin "})
    assert settings.milkdrop_preset == "martin"
    assert web_ui.settings_to_dict(settings)["milkdrop_preset"] == "martin"
    assert web_ui.parse_settings(GOOD).milkdrop_preset == ""
    with pytest.raises(ValueError, match="preset"):
        web_ui.parse_settings({**GOOD, "milkdrop_preset": "x" * 500})


async def test_every_page_hears_the_status() -> None:
    """Each page learns whether music plays and the player's name, also one opened later."""
    layout = LedLayout.strip(12)
    bridge = bridge_module.Bridge(Engine(layout), _NullSender(), 120, name="Lounge LEDs")
    bridge.source = "connected"
    client = await _client(web_ui.WebUi(bridge, bridge_module.demo_features))

    async def status(ws: Any) -> dict[str, Any]:
        async with asyncio.timeout(5):
            while (message := await ws.receive()).type != aiohttp.WSMsgType.TEXT:
                pass
        data: dict[str, Any] = message.json()
        return data

    try:
        first = await client.ws_connect("/api/ws")
        heard = await status(first)
        assert (heard["playing"], heard["source"], heard["player"]) == (
            False,
            "connected",
            "Lounge LEDs",
        )
        second = await client.ws_connect("/api/ws")
        assert (await status(second))["player"] == "Lounge LEDs"
        bridge.streaming = True
        assert (await status(first))["playing"] is True
        assert (await status(second))["playing"] is True
        await first.close()
        await second.close()
    finally:
        await client.close()


async def test_only_the_allowed_client_gets_in() -> None:
    """Behind Ingress, every client but the Supervisor is refused."""
    client = await _client(
        web_ui.WebUi(_bridge(), bridge_module.demo_features, allowed_remote="172.30.32.2")
    )
    try:
        assert (await client.get("/api/state")).status == 403
        assert (await client.post("/api/apply", json=GOOD)).status == 403
    finally:
        await client.close()


async def test_other_sites_cannot_drive_the_page() -> None:
    """Cross-site requests, DNS-rebinding host names and non-JSON posts are refused."""
    bridge = _bridge()
    client = await _client(web_ui.WebUi(bridge, bridge_module.demo_features))
    try:
        evil = {"Origin": "http://evil.example"}
        assert (await client.post("/api/apply", json=GOOD, headers=evil)).status == 403
        assert (await client.get("/api/ws", headers=evil)).status == 403
        rebound = {"Host": "evil.example:8765"}
        assert (await client.get("/api/state", headers=rebound)).status == 403
        plain = await client.post(
            "/api/apply", data=json.dumps(GOOD), headers={"Content-Type": "text/plain"}
        )
        assert plain.status == 415
        assert bridge.engine.settings.effect == Effect.SPECTRUM
        same = {"Origin": str(client.make_url("")).rstrip("/")}
        assert (await client.post("/api/apply", json=GOOD, headers=same)).status == 200
        assert (await client.get("/api/state", headers={"Host": "localhost:8765"})).status == 200
    finally:
        await client.close()


async def test_applied_settings_are_saved_as_addon_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the Supervisor, Apply merges the settings into the add-on's options."""
    posted: list[dict[str, Any]] = []
    options = {"hyperhdr_host": "192.168.1.20", "effect": "spectrum", "token": "keep"}

    async def info(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test-supervisor-token"
        return web.json_response(
            {
                "result": "ok",
                "data": {"options": options, "ip_address": "127.0.0.1", "ingress_port": 0},
            }
        )

    async def save(request: web.Request) -> web.Response:
        posted.append(await request.json())
        return web.json_response({"result": "ok", "data": {}})

    app = web.Application()
    app.router.add_get("/addons/self/info", info)
    app.router.add_post("/addons/self/options", save)
    supervisor = TestServer(app)
    await supervisor.start_server()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    monkeypatch.setattr(web_ui, "SUPERVISOR_URL", str(supervisor.make_url("")).rstrip("/"))
    bridge = _bridge()
    async with aiohttp.ClientSession() as session:
        client = await _client(
            web_ui.WebUi(
                bridge, bridge_module.demo_features, saver=web_ui.supervisor_saver(session)
            )
        )
        try:
            result = await (await client.post("/api/apply", json=GOOD)).json()
        finally:
            await client.close()
            await supervisor.close()
    assert result["saved"] is True
    assert posted == [{"options": {**options, **APPLIED}}]


async def test_idle_preview_shows_the_night_light() -> None:
    """'When idle' previews the night light in the custom colour, or dark when it is off."""
    bridge = _bridge()
    client = await _client(web_ui.WebUi(bridge, bridge_module.demo_features))

    async def last_preview(ws: Any, frames: int = 40) -> bytes:
        preview = b""
        async with asyncio.timeout(5):
            for _ in range(frames):
                message = await ws.receive()
                while message.type != aiohttp.WSMsgType.BINARY:
                    message = await ws.receive()
                preview = message.data[36:]
        return preview

    try:
        ws = await client.ws_connect("/api/ws")
        night = {**GOOD, "color": "#00ff00", "night_light": 50}
        await ws.send_json({"type": "preview", "settings": night, "mode": "idle"})
        rgb = await last_preview(ws, 60)
        assert all(rgb[i] < 5 and rgb[i + 1] > 100 and rgb[i + 2] < 5 for i in range(0, 36, 3))
        await ws.send_json(
            {"type": "preview", "settings": {**night, "night_light": 0}, "mode": "idle"}
        )
        assert not any(await last_preview(ws, 5))
        assert bridge.night_light == 0.0, "previewing never changes the real night light"
        await ws.close()
    finally:
        await client.close()
