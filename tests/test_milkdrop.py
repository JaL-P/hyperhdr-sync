"""Tests for the MilkDrop waveform client of the standalone bridge."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import numpy as np
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "standalone"))

import milkdrop

from hyperhdr_sync.engine import WAVE_SAMPLES, Effect, Engine, EngineSettings, bytes_to_wave
from hyperhdr_sync.layout import LedLayout

_now_us = milkdrop._now_us  # noqa: SLF001


def _frame(play_at_us: int, value: int = 200) -> bytes:
    samples = np.full(WAVE_SAMPLES, value, np.uint8)
    samples[::2] = 255 - value  # a square wave, so it is not flat
    return milkdrop._HEADER.pack(milkdrop.WAVE_TAG, play_at_us) + samples.tobytes()  # noqa: SLF001


def test_addresses() -> None:
    """The web address follows the Sendspin host; the relay address names the player."""
    assert milkdrop.web_url_from_sendspin("ws://192.168.1.5:8927/sendspin") == (
        "http://192.168.1.5:8095"
    )
    assert milkdrop.web_url_from_sendspin("ws://[fd00::5]:8927/sendspin") == "http://[fd00::5]:8095"
    assert milkdrop.relay_url("http://ma.local:8095/", None) == (
        "ws://ma.local:8095/milkdrop_visualizer"
    )
    assert milkdrop.relay_url("https://ma.example/ma", "media player.1") == (
        "wss://ma.example/ma/milkdrop_visualizer?player=media%20player.1"
    )


def test_feed_hands_out_the_frame_sounding_now() -> None:
    """Frames wait for their time, on the server's clock and the lights' latency."""
    feed = milkdrop.WaveformFeed()
    now = _now_us()
    feed.add_frame(_frame(now + 2_000_000))
    assert feed.current() is None, "no clock sync yet"
    # The server's clock runs 10 s ahead of ours.
    feed.add_time_sync(now, now + 10_000_000, now + 10_000_000)
    assert feed.offset_us is not None
    assert abs(feed.offset_us - 10_000_000) < 50_000
    feed.add_frame(_frame(now + 10_000_000 - 100_000, 200))
    feed.add_frame(_frame(now + 10_000_000 + 400_000, 60))
    data = feed.current()
    assert data is not None
    wave = bytes_to_wave(data)
    assert wave.shape == (WAVE_SAMPLES,)
    assert float(wave.max()) <= 1.0
    assert float(wave.min()) >= -1.0
    assert wave[1] > 0.5, "the frame due now, not the one after it"
    assert feed.current(ahead_us=500_000) is not None, "rendering ahead picks the next frame"
    assert feed.current() is None, "nothing new is due"


def test_feed_skips_stale_frames_and_restarts_after_a_jump() -> None:
    """An old frame is not shown, and audio that jumps back replaces the frames after it."""
    feed = milkdrop.WaveformFeed()
    now = _now_us()
    feed.add_time_sync(now, now, now)
    feed.add_frame(_frame(now - 1_000_000))
    assert feed.current() is None, "a second old: stale"
    feed.add_frame(_frame(now + 5_000_000))
    feed.add_frame(_frame(now - 50_000))  # a seek back: the later frame is dropped
    assert feed.current() is not None
    assert feed.current(ahead_us=6_000_000) is None
    feed.add_frame(b"\x11" + bytes(9))  # a beat entry: ignored
    feed.clear()
    assert feed.current(ahead_us=6_000_000) is None


async def test_waveform_loop_against_a_relay() -> None:
    """The client signs in, syncs its clock and receives frames, like the MA frontend."""
    tokens: list[str] = []
    players: list[str | None] = []

    async def relay(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        players.append(request.query.get("player"))
        auth = await ws.receive_json()
        tokens.append(auth["token"])
        if auth != {"type": "auth", "token": "good"}:
            await ws.close(code=milkdrop.AUTH_REJECTED, message=b"Invalid or expired token")
            return ws
        await ws.send_str('{"type": "auth_ok"}')
        await ws.send_str(json.dumps({"type": "stream/start", "payload": {}}))
        await ws.send_bytes(_frame(_now_us() + 300_000))
        async for message in ws:
            data = json.loads(message.data)
            if data["type"] == "client/time":
                now = _now_us()
                payload = {
                    "client_transmitted": data["payload"]["client_transmitted"],
                    "server_received": now,
                    "server_transmitted": now,
                }
                await ws.send_str(json.dumps({"type": "server/time", "payload": payload}))
        return ws

    app = web.Application()
    app.router.add_get("/milkdrop_visualizer", relay)
    server = TestServer(app)
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    feed = milkdrop.WaveformFeed()
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(milkdrop.waveform_loop(feed, session, url, "good", "tv"))
        try:
            async with asyncio.timeout(5):
                while (data := feed.current(ahead_us=400_000)) is None:  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
            wave = bytes_to_wave(data)
            assert wave[0] < 0 < wave[1]
            assert players == ["tv"]
        finally:
            task.cancel()
        # A refused token: the client says so and waits long before trying again.
        async with session.ws_connect(milkdrop.relay_url(url, None)) as ws:
            delay = await milkdrop._serve(feed, ws, "bad")  # noqa: SLF001
        assert delay == milkdrop.RETRY_AFTER_REJECT_S
        assert tokens == ["good", "bad"]
    await server.close()


def test_liquid_draws_the_waveform() -> None:
    """With a waveform Liquid draws it as its ring; in silence it still goes dark."""
    layout = LedLayout.strip(40)
    engines = [Engine(layout, EngineSettings(effect=Effect.LIQUID)) for _ in range(2)]
    t = np.linspace(0.0, 1.0, WAVE_SAMPLES, dtype=np.float32)
    wave = (0.6 * np.sin(2.0 * np.pi * 9.0 * t)).astype(np.float32)
    frames: list[bytes] = []
    for engine in engines:
        frame = b""
        for _ in range(90):
            engine.feed(loudness=50000, spectrum=[30000] * 16)
            if engine is engines[1]:
                engine.feed(waveform=wave)
            frame = engine.render(1 / 60)
        frames.append(frame)
    assert any(frames[1]), "the wave lights the LEDs"
    assert frames[0] != frames[1], "the wave changes the picture"
    quiet = engines[1]
    frame = b""
    for _ in range(240):
        quiet.feed(loudness=0, spectrum=[0] * 16, waveform=wave)
        frame = quiet.render(1 / 60)
    assert max(frame) < 8, "silence fades out even with a waveform"
