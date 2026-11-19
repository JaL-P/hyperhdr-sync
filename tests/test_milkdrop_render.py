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
from milkdrop_render import LIVE, PREVIEW, MilkdropRenderer

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
        assert len(renderer.presets) > 100
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
