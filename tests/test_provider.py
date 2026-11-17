"""Tests of the provider, loaded by a real server."""

import asyncio
import socket
from collections.abc import AsyncGenerator
from typing import Any, cast

import numpy as np
import pytest
from aiosendspin.server.roles.visualizer.features import ExtractedFrame
from music_assistant_models.enums import FlowStepType, PlayerType, ProviderTier

from hyperhdr_sync.engine import SPECTRUM_BINS
from music_assistant.mass import MusicAssistant
from tests import DOMAIN
from tests.fake_hyperhdr import LEDS, FakeHyperHdr


@pytest.fixture
async def hyperhdr() -> AsyncGenerator[FakeHyperHdr]:
    """Return a running fake HyperHDR."""
    fake = FakeHyperHdr()
    await fake.start()
    yield fake
    await fake.stop()


def _closed_port() -> int:
    """Return a local TCP port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


async def _next_frame(hyperhdr: FakeHyperHdr) -> bytes:
    """Wait for the next frame the fake HyperHDR receives."""
    hyperhdr.frame_received.clear()
    await asyncio.wait_for(hyperhdr.frame_received.wait(), 5)
    return hyperhdr.frames[-1]


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_setup_and_light_show(mass: MusicAssistant, hyperhdr: FakeHyperHdr) -> None:
    """The setup flow checks HyperHDR, then music on the light player reaches the LEDs."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.type == FlowStepType.FORM
    assert step.step_id == "user"

    # an address where no HyperHDR answers brings the form back with an error
    step = await mass.config.submit_setup_flow(
        step.flow_id,
        {"host": "127.0.0.1", "web_port": _closed_port(), "udp_port": hyperhdr.udp_port},
    )
    assert step.type == FlowStepType.FORM
    assert "base" in step.errors

    step = await mass.config.submit_setup_flow(
        step.flow_id,
        {"host": "127.0.0.1", "web_port": hyperhdr.web_port, "udp_port": hyperhdr.udp_port},
    )
    assert step.type == FlowStepType.FINISH, step.errors
    assert step.result is not None
    provider = mass.get_provider(step.result["instance_id"])
    assert provider is not None

    # the LEDs appear as a light player that can be grouped with a speaker
    bridge = cast("Any", provider)._bridge  # noqa: SLF001
    for _ in range(50):
        if player := mass.players.get_player(bridge.client_id):
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the light player was not registered")
    assert player.type == PlayerType.LIGHT

    # music: feature frames that are due light the LEDs, one colour per LED
    bridge._on_stream_start()  # noqa: SLF001
    lit = b""
    for _ in range(40):
        bridge._on_frame(  # noqa: SLF001
            ExtractedFrame(
                timestamp_us=0, loudness=50000, spectrum=np.full(SPECTRUM_BINS, 50000, np.uint16)
            )
        )
        lit = await _next_frame(hyperhdr)
        if max(lit) > 60:
            break
    assert len(lit) == len(LEDS) * 3
    assert max(lit) > 60

    # the music stops: the LEDs are switched off
    bridge._on_stream_end()  # noqa: SLF001
    assert await _next_frame(hyperhdr) == bytes(len(LEDS) * 3)


async def test_starts_without_hyperhdr(mass: MusicAssistant, hyperhdr: FakeHyperHdr) -> None:
    """A provider set up earlier loads from its stored LED layout while HyperHDR is off."""
    step = await mass.config.setup_provider(DOMAIN)
    step = await mass.config.submit_setup_flow(
        step.flow_id,
        {"host": "127.0.0.1", "web_port": hyperhdr.web_port, "udp_port": hyperhdr.udp_port},
    )
    assert step.result is not None
    instance_id = step.result["instance_id"]

    await hyperhdr.stop()
    await mass.unload_provider(instance_id)
    await mass.load_provider(instance_id, allow_retry=False)

    provider = mass.get_provider(instance_id)
    assert provider is not None
    assert cast("Any", provider)._bridge is not None  # noqa: SLF001
