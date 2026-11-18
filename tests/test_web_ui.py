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

from hyperhdr_sync.engine import Effect, Engine, EngineSettings, Palette, Response
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
    settings, latency = web_ui.parse_settings(GOOD)
    assert settings == EngineSettings(
        effect=Effect.PULSE,
        palette=Palette.FIRE,
        brightness=0.6,
        sensitivity_db=3.0,
        response=Response.SMOOTH,
    )
    assert latency == 200
    assert web_ui.settings_to_dict(settings, latency) == {**GOOD, "sensitivity": 3.0}
    for bad in (
        {**GOOD, "effect": "strobe"},
        {**GOOD, "brightness": 101},
        {**GOOD, "sensitivity": -13},
        {**GOOD, "latency_ms": "soon"},
        {k: v for k, v in GOOD.items() if k != "palette"},
        ["not", "an", "object"],
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            web_ui.parse_settings(bad)


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
        assert {p["id"] for p in state["palettes"]} == {p.value for p in Palette}
        assert all(len(p["colors"]) >= 2 for p in state["palettes"])

        bad = await client.post("/api/apply", json={**GOOD, "brightness": 500})
        assert bad.status == 400
        assert bridge.engine.settings.effect == Effect.SPECTRUM

        result = await (await client.post("/api/apply", json=GOOD)).json()
        assert result == {"settings": {**GOOD, "sensitivity": 3.0}, "saved": False}
        assert bridge.engine.settings.effect == Effect.PULSE
        assert bridge.latency_ms == 200
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
    assert posted == [{"options": {**options, **GOOD, "sensitivity": 3.0}}]
