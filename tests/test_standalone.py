"""Tests for the standalone bridge's lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
from pathlib import Path

from .fake_hyperhdr import LEDS, FakeHyperHdr

_PATH = Path(__file__).resolve().parents[1] / "standalone" / "hyperhdr_bridge.py"
_SPEC = importlib.util.spec_from_file_location("hyperhdr_bridge", _PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
bridge = importlib.util.module_from_spec(_SPEC)
sys.modules["hyperhdr_bridge"] = bridge
_SPEC.loader.exec_module(bridge)


async def test_stopping_blacks_out_the_leds(tmp_path: Path) -> None:
    """Cancelling the bridge (Ctrl+C or SIGTERM) leaves the LEDs with three black frames."""
    fake = FakeHyperHdr()
    await fake.start()
    try:
        args = bridge.parse_args(
            [
                "--demo",
                "--hyperhdr-host=127.0.0.1",
                f"--web-port={fake.web_port}",
                f"--udp-port={fake.udp_port}",
                f"--state-dir={tmp_path}",
            ]
        )
        task = asyncio.create_task(bridge.run(args))
        await asyncio.wait_for(fake.frame_received.wait(), 10)
        await asyncio.sleep(0.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
    finally:
        await fake.stop()
    assert any(any(frame) for frame in fake.frames)
    assert fake.frames[-3:] == [bytes(len(LEDS) * 3)] * 3


async def test_night_light_keeps_the_leds_while_idle() -> None:
    """With a night light, the idle bridge keeps sending the custom colour; without, it stops."""
    from hyperhdr_sync.engine import Engine, EngineSettings  # noqa: PLC0415
    from hyperhdr_sync.layout import LedLayout  # noqa: PLC0415

    class Sender:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def send(self, rgb: bytes) -> None:
            self.frames.append(rgb)

    for night_light in (0.5, 0.0):
        sender = Sender()
        engine = Engine(LedLayout.strip(10), EngineSettings(color=(0, 0, 255)))
        idle = bridge.Bridge(engine, sender, 120, night_light)
        task = asyncio.create_task(idle.render_loop())
        await asyncio.sleep(2.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if night_light:
            assert len(sender.frames) > 40
            last = sender.frames[-1]
            assert last[2] > 100
            assert last[0] == last[1] == 0
        else:
            assert sender.frames == []
