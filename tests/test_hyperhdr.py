"""Tests of the HyperHDR client against a fake HyperHDR on the local machine."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from aiohttp import ClientSession

from hyperhdr_sync.hyperhdr import (
    HyperHdrAuthError,
    HyperHdrError,
    HyperHdrUdpSender,
    fetch_serverinfo,
)
from tests.fake_hyperhdr import LEDS, TOKEN, FakeHyperHdr


@pytest.fixture
async def hyperhdr() -> AsyncGenerator[FakeHyperHdr]:
    """Return a running fake HyperHDR."""
    fake = FakeHyperHdr()
    await fake.start()
    yield fake
    await fake.stop()


async def test_serverinfo_returns_the_leds(hyperhdr: FakeHyperHdr) -> None:
    """The LED list is read from the JSON-RPC endpoint."""
    async with ClientSession() as session:
        info = await fetch_serverinfo(session, "127.0.0.1", hyperhdr.web_port)
    assert info["leds"] == LEDS


async def test_serverinfo_uses_the_token(hyperhdr: FakeHyperHdr) -> None:
    """Without the token an authorization error is raised; with it the request succeeds."""
    hyperhdr.require_token = True
    async with ClientSession() as session:
        with pytest.raises(HyperHdrAuthError):
            await fetch_serverinfo(session, "127.0.0.1", hyperhdr.web_port)
        info = await fetch_serverinfo(session, "127.0.0.1", hyperhdr.web_port, TOKEN)
    assert info["leds"] == LEDS


async def test_serverinfo_reports_an_unreachable_host(unused_tcp_port: int) -> None:
    """A closed port gives a HyperHDR error instead of a raw network exception."""
    async with ClientSession() as session:
        with pytest.raises(HyperHdrError):
            await fetch_serverinfo(session, "127.0.0.1", unused_tcp_port)


async def test_udp_sender_delivers_frames(hyperhdr: FakeHyperHdr) -> None:
    """A frame arrives at the raw receiver exactly as sent."""
    sender = HyperHdrUdpSender("127.0.0.1", hyperhdr.udp_port)
    await sender.open()
    frame = bytes(range(9))
    sender.send(frame)
    await asyncio.wait_for(hyperhdr.frame_received.wait(), 2)
    assert hyperhdr.frames == [frame]
    sender.close()
    sender.send(frame)  # ignored once closed


async def test_udp_sender_refuses_frames_hyperhdr_would_drop(hyperhdr: FakeHyperHdr) -> None:
    """Frames that are empty, not RGB triplets or above 1500 bytes are refused."""
    sender = HyperHdrUdpSender("127.0.0.1", hyperhdr.udp_port)
    await sender.open()
    for bad in (b"", b"\x00" * 4, b"\x00" * 1503):
        with pytest.raises(ValueError, match="Frame"):
            sender.send(bad)
    sender.close()
