"""Tests for the bridge's MilkDrop renderer (Butterchurn in a headless Chromium)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import aiohttp
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "standalone"))

import milkdrop_render
from milkdrop_render import LIVE, PREVIEW, MilkdropRenderer, VisualizerPrefs

PRESETS = ["alpha", "beta", "gamma"]


async def _fake_page(url: str, seen: list[tuple[int, str]]) -> None:
    """Act as the page: say hello, note presets, answer each waveform with a picture."""
    ws_url = url.split("?", maxsplit=1)[0].replace("http://", "ws://") + "ws"
    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url) as ws:
        await ws.send_str(json.dumps({"type": "hello", "presets": PRESETS}))
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(message.data)
                seen.append((data["slot"], data["name"]))
            elif message.type == aiohttp.WSMsgType.BINARY:
                slot, wave = message.data[0], message.data[1:]
                assert len(wave) == milkdrop_render.WAVE_SAMPLES
                # The picture is the slot's colour, brighter for a louder wave.
                level = min(255, 40 + abs(wave[0] - 128) * 2)
                color = (level, 0, 0) if slot == LIVE else (0, 0, level)
                pixels = bytes(color) * (milkdrop_render.WIDTH * milkdrop_render.HEIGHT)
                await ws.send_bytes(bytes([slot]) + pixels)


async def test_renderer_trades_waveforms_for_pictures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pictures come back per slot, presets follow the choice, nothing else gets in."""
    for name in milkdrop_render.VENDOR_FILES:
        (tmp_path / name).write_text("// stand-in", encoding="utf-8")
    seen: list[tuple[int, str]] = []
    urls: list[str] = []

    async def run_page(self: MilkdropRenderer, url: str) -> None:
        del self
        urls.append(url)
        await _fake_page(url, seen)

    monkeypatch.setattr(MilkdropRenderer, "_run_chromium", run_page)
    renderer = MilkdropRenderer(chromium=sys.executable, vendor=tmp_path)
    assert renderer.unavailable_reason is None
    renderer.set_preset(LIVE, "beta")
    wave = bytes([200]) * milkdrop_render.WAVE_SAMPLES
    try:
        async with asyncio.timeout(5):
            while renderer.picture(LIVE) is None or renderer.picture(PREVIEW) is None:
                await renderer.render(LIVE, wave)
                await renderer.render(PREVIEW, wave)
                await asyncio.sleep(0.02)
        live, preview = renderer.picture(LIVE), renderer.picture(PREVIEW)
        assert live is not None
        assert preview is not None
        assert live.shape == (milkdrop_render.HEIGHT, milkdrop_render.WIDTH, 3)
        assert live[..., 0].min() > 0.5
        assert live[..., 2].max() == 0
        assert preview[..., 2].min() > 0.5
        assert renderer.presets == PRESETS
        assert renderer.now_showing(LIVE) == "beta"
        assert renderer.now_showing(PREVIEW) in PRESETS, "no choice: a random preset"
        assert (LIVE, "beta") in seen
        renderer.set_preset(LIVE, "gamma")
        async with asyncio.timeout(5):
            while (LIVE, "gamma") not in seen:
                await renderer.render(LIVE, wave)
                await asyncio.sleep(0.02)
        # The page lives under a random path: a guessed address gets nothing.
        base = urls[0].split("?")[0]
        guessed = base.rsplit("/", 2)[0] + "/ws"
        async with aiohttp.ClientSession() as session:
            response = await session.get(guessed)
            assert response.status == 404
    finally:
        await renderer.close()


def test_renderer_says_what_is_missing(tmp_path: Path) -> None:
    """Without Chromium or Butterchurn the renderer says what to install."""
    missing = MilkdropRenderer(chromium=str(tmp_path / "no-chromium"), vendor=tmp_path)
    assert missing.unavailable_reason is not None
    assert "Chromium" in missing.unavailable_reason
    no_vendor = MilkdropRenderer(chromium=sys.executable, vendor=tmp_path)
    assert no_vendor.unavailable_reason is not None
    assert "fetch_butterchurn" in no_vendor.unavailable_reason


@pytest.mark.timeout(90)
async def test_real_presets_render_in_chromium() -> None:
    """With Chromium and Butterchurn present, a real preset draws a real picture."""
    renderer = MilkdropRenderer(chromium=os.environ.get("HYPERHDR_TEST_CHROMIUM"))
    if renderer.unavailable_reason is not None:
        pytest.skip(renderer.unavailable_reason)
    renderer.set_preset(LIVE, "martin - mucus cervix")
    samples = np.arange(milkdrop_render.WAVE_SAMPLES)
    try:
        async with asyncio.timeout(60):
            frame = 0
            while renderer.picture(LIVE) is None or frame < 120:
                wave = 128 + 100 * np.sin((samples + frame * 37) * 0.05)
                await renderer.render(LIVE, wave.astype(np.uint8).tobytes())
                await asyncio.sleep(1 / 60)
                frame += 1 if renderer.picture(LIVE) is not None else 0
        picture = renderer.picture(LIVE)
        assert picture is not None
        assert len(renderer.presets) == 245, "the two packs Music Assistant loads"
        assert renderer.now_showing(LIVE) == "martin - mucus cervix"
        assert float(picture.mean()) > 0.05, "the preset draws something"
    finally:
        await renderer.close()


def _preview_of(bridge: Any) -> Any:
    return bridge.preview


class _StubRenderer:
    """Stands in for the renderer: keeps the presets and waveforms, returns a fixed picture."""

    unavailable_reason = None

    def __init__(self) -> None:
        self.presets: dict[int, str] = {}
        self.waves: dict[int, bytes] = {}

    def set_preset(self, slot: int, name: str) -> None:
        self.presets[slot] = name

    async def render(self, slot: int, wave: bytes) -> None:
        self.waves[slot] = wave

    def picture(self, slot: int) -> np.ndarray:
        color = (1.0, 0.0, 0.0) if slot == LIVE else (0.0, 1.0, 0.0)
        return np.tile(
            np.array(color, np.float32), (milkdrop_render.HEIGHT, milkdrop_render.WIDTH, 1)
        )


async def test_bridge_feeds_pictures_to_the_engines_that_show_them() -> None:
    """The LEDs and the preview get their own slot, preset and picture; others get nothing."""
    import hyperhdr_bridge  # noqa: PLC0415

    from hyperhdr_sync.engine import Effect, Engine, EngineSettings, Palette  # noqa: PLC0415
    from hyperhdr_sync.layout import LedLayout  # noqa: PLC0415

    class Sender:
        host, port = "127.0.0.1", 0

        def send(self, rgb: bytes) -> None:
            pass

    live = EngineSettings(effect=Effect.MILKDROP, palette=Palette.MILKDROP)
    bridge = hyperhdr_bridge.Bridge(Engine(LedLayout.strip(20), live), cast("Any", Sender()), 0)
    stub = _StubRenderer()
    bridge.milkdrop = cast("Any", stub)
    bridge.apply(live, 0, 0.0, milkdrop_preset="alpha")
    bridge.set_preview(live, "beta")
    assert bridge.preview is not None, "only the preset differs, still a preview of its own"
    preview = bridge.preview
    live_rgb = preview_rgb = np.zeros((20, 3), np.uint8)
    for _ in range(30):
        for engine in (bridge.engine, preview):
            engine.feed(loudness=50000, spectrum=[40000] * 16)
        await bridge._feed_milkdrop(None)  # noqa: SLF001
        live_rgb = np.frombuffer(bridge.engine.render(1 / 60), np.uint8).reshape(-1, 3)
        preview_rgb = np.frombuffer(preview.render(1 / 60), np.uint8).reshape(-1, 3)
    assert stub.presets == {LIVE: "alpha", PREVIEW: "beta"}
    assert len(stub.waves[LIVE]) == milkdrop_render.WAVE_SAMPLES, "made from the spectrum"
    assert live_rgb[:, 0].min() > 100
    assert live_rgb[:, 1].max() == 0
    assert preview_rgb[:, 1].min() > 100
    bridge.set_preview(live, "alpha")
    assert _preview_of(bridge) is None, "the same settings and preset: the live frame"
    other = hyperhdr_bridge.Bridge(Engine(LedLayout.strip(20)), cast("Any", Sender()), 0)
    idle = _StubRenderer()
    other.milkdrop = cast("Any", idle)
    await other._feed_milkdrop(None)  # noqa: SLF001
    assert idle.waves == {}, "an effect without MilkDrop starts nothing"


class _Socket:
    """Collects the preset messages the renderer sends to the page."""

    def __init__(self) -> None:
        self.loaded: list[str] = []

    async def send_str(self, text: str) -> None:
        self.loaded.append(json.loads(text)["name"])


async def _step(renderer: MilkdropRenderer, ws: _Socket, slot: int = LIVE) -> str | None:
    await renderer._update_preset(cast("Any", ws), slot)  # noqa: SLF001
    return renderer.now_showing(slot)


def test_visualizer_prefs_read_music_assistants_settings() -> None:
    """The settings Music Assistant's web interface saves are read, with its defaults."""
    defaults = VisualizerPrefs()
    assert (defaults.mode, defaults.beat_switch, defaults.beat_dwell_s) == ("random", False, 30.0)
    assert not defaults.from_music_assistant
    prefs = VisualizerPrefs.from_preferences(
        {
            "visualizer_preset_mode": "random_favorites",
            "visualizer_preset": "beta",
            "visualizer_favorites": ["alpha", "gamma", 7],
            "visualizer_beat_switch": True,
            "visualizer_beat_dwell": 12,
            "theme": "dark",
        }
    )
    assert prefs.mode == "random_favorites"
    assert prefs.favorites == ("alpha", "gamma")
    assert (prefs.beat_switch, prefs.beat_dwell_s, prefs.from_music_assistant) == (True, 12.0, True)
    assert "2 favourites" in prefs.describe()
    assert "on the beat every 12 s" in prefs.describe()
    odd = VisualizerPrefs.from_preferences(
        {"visualizer_preset_mode": "nonsense", "visualizer_beat_dwell": "x"}
    )
    assert (odd.mode, odd.beat_dwell_s) == ("random", 30.0)


async def test_presets_are_picked_like_music_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed, favourites, beat switch after the dwell, a new pick on start, or the user's own."""
    renderer = MilkdropRenderer(chromium=sys.executable)
    renderer.presets = [f"preset {i}" for i in range(20)]
    ws = _Socket()
    clock = [1000.0]
    monkeypatch.setattr("milkdrop_render.time.monotonic", lambda: clock[0])

    first = await _step(renderer, ws)
    assert first in renderer.presets, "Music Assistant's default: a random preset"
    clock[0] += 120
    assert await _step(renderer, ws) == first, "and it keeps it: no timer"
    renderer.downbeat()
    assert await _step(renderer, ws) == first, "beat switch is off by default"

    renderer.set_ma_prefs(
        VisualizerPrefs(mode="fixed", preset="preset 7", from_music_assistant=True)
    )
    assert await _step(renderer, ws) == "preset 7"

    favourites = ("preset 3", "preset 4", "missing")
    renderer.set_ma_prefs(
        VisualizerPrefs(
            mode="random_favorites",
            favorites=favourites,
            beat_switch=True,
            beat_dwell_s=30,
            from_music_assistant=True,
        )
    )
    picked = await _step(renderer, ws)
    assert picked in favourites[:2]
    clock[0] += 10
    renderer.downbeat()
    assert await _step(renderer, ws) == picked, "a downbeat before the dwell changes nothing"
    clock[0] += 25
    assert await _step(renderer, ws) == picked, "after the dwell, but no downbeat yet"
    renderer.downbeat()
    switched = await _step(renderer, ws)
    assert switched in favourites[:2]
    assert switched != picked

    renderer.repick()
    assert await _step(renderer, ws) in favourites[:2], "music starting picks again"

    renderer.set_preset(LIVE, "preset 11")
    assert await _step(renderer, ws) == "preset 11", "the user's own choice wins"
    clock[0] += 100
    renderer.downbeat()
    assert await _step(renderer, ws) == "preset 11", "and beat switch leaves it alone"


async def test_preferences_loop_reads_auth_me(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge reads Music Assistant's visualizer settings from /auth/me with its token."""
    import milkdrop  # noqa: PLC0415
    from aiohttp import web  # noqa: PLC0415
    from aiohttp.test_utils import TestServer  # noqa: PLC0415

    seen: list[str | None] = []

    async def auth_me(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Authorization"))
        return web.json_response(
            {
                "user_id": "1",
                "preferences": {"visualizer_preset_mode": "fixed", "visualizer_preset": "beta"},
            }
        )

    app = web.Application()
    app.router.add_get("/auth/me", auth_me)
    server = TestServer(app)
    await server.start_server()
    got: list[VisualizerPrefs] = []
    monkeypatch.setattr(milkdrop, "PREFERENCES_S", 0.01)
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(
            milkdrop.preferences_loop(got.append, session, str(server.make_url("")), "token-1")
        )
        try:
            async with asyncio.timeout(5):
                while len(got) < 2:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
    await server.close()
    assert seen[0] == "Bearer token-1"
    assert got[0] == VisualizerPrefs(mode="fixed", preset="beta", from_music_assistant=True)


def test_bridge_passes_music_assistants_tint_beats_and_starts() -> None:
    """Sendspin's on_light becomes the tint; downbeats and music starting reach the renderer."""
    from types import SimpleNamespace  # noqa: PLC0415

    import hyperhdr_bridge  # noqa: PLC0415

    from hyperhdr_sync.engine import Engine  # noqa: PLC0415
    from hyperhdr_sync.layout import LedLayout  # noqa: PLC0415

    class Renderer:
        def __init__(self) -> None:
            self.downbeats = 0
            self.repicks = 0

        def downbeat(self) -> None:
            self.downbeats += 1

        def repick(self) -> None:
            self.repicks += 1

    bridge = hyperhdr_bridge.Bridge(Engine(LedLayout.strip(10)), cast("Any", None), 0)
    renderer = Renderer()
    bridge.milkdrop = cast("Any", renderer)
    palette = SimpleNamespace(
        primary=(200, 20, 20),
        accent=None,
        background_light=None,
        background_dark=None,
        on_dark=(250, 240, 230),
        on_light=(30, 60, 90),
    )
    bridge.on_color(cast("Any", SimpleNamespace(color=palette)))
    assert bridge.engine.milkdrop_tint == (30, 60, 90)
    bridge.on_stream_start(
        cast("Any", SimpleNamespace(payload=SimpleNamespace(visualizer=object())))
    )
    assert renderer.repicks == 1
    beats = [
        SimpleNamespace(timestamp_us=100, is_downbeat=True),
        SimpleNamespace(timestamp_us=200, is_downbeat=False),
    ]
    bridge._beats = cast("Any", beats)  # noqa: SLF001
    bridge._drain(300)  # noqa: SLF001
    assert renderer.downbeats == 1
    bridge.on_color(
        cast("Any", SimpleNamespace(color=SimpleNamespace(**{**vars(palette), "on_light": None})))
    )
    assert bridge.engine.milkdrop_tint is None
