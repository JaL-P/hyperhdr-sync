"""
Real-time waveform from Music Assistant's MilkDrop Visualizer plugin.

The plugin reads the audio Music Assistant decodes for a player and streams it as
waveform frames over a websocket on Music Assistant's web server, ahead of time and
stamped with when each frame sounds. The MA web frontend draws MilkDrop pictures from
them; the bridge gives them to the Liquid effect, so its ring is the real sound wave.

Wire format (see the plugin's ``relay.py``):

- binary ``[22][play-at: int64 BE, µs][1024 x uint8, 128 = zero]``: a waveform frame
- binary ``[17][play-at][flags]``: a beat (the bridge gets beats from Sendspin instead)
- text ``{"type": "server/time", ...}``: answer to ``client/time``, for clock sync
- text ``{"type": "stream/clear" | "stream/end"}``: drop frames of audio that went away
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from collections import deque
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
import numpy as np

LOGGER = logging.getLogger("hyperhdr_bridge.milkdrop")

WAVE_TAG = 22
WAVE_SAMPLES = 1024
_HEADER = struct.Struct(">Bq")
# Music Assistant's web server, when only the Sendspin address is known.
DEFAULT_WEB_PORT = 8095
# Close code of a refused sign-in: retrying with the same token cannot help.
AUTH_REJECTED = 4001
# Frames arrive up to 5 s ahead (about 43 a second); keep a little more than that.
MAX_FRAMES = 400
# A frame older than this is not shown: the audio moved on or the stream stalled.
STALE_US = 250_000
# Clock sync: quick pings at first, then now and then; the best of the last few wins.
SYNC_FAST_S, SYNC_FAST_COUNT, SYNC_SLOW_S = 0.25, 8, 5.0
SYNC_SAMPLES = 8
RECONNECT_S = 5.0
RETRY_AFTER_REJECT_S = 300.0


def _now_us() -> int:
    return int(time.monotonic() * 1_000_000)


def web_url_from_sendspin(server: str) -> str:
    """
    Return Music Assistant's web address on the host of its Sendspin address.

    :param server: The Sendspin address, such as ``ws://192.168.1.5:8927/sendspin``.
    """
    host = urlsplit(server).hostname or "127.0.0.1"
    if ":" in host:  # an IPv6 address needs brackets in a URL
        host = f"[{host}]"
    return f"http://{host}:{DEFAULT_WEB_PORT}"


def relay_url(web_url: str, player: str | None) -> str:
    """
    Return the plugin's websocket address.

    :param web_url: Music Assistant's web address, such as ``http://127.0.0.1:8095``.
    :param player: The Music Assistant player id to follow; none picks the one playing.
    """
    parts = urlsplit(web_url.strip())
    scheme = "wss" if parts.scheme in ("https", "wss") else "ws"
    path = parts.path.rstrip("/") + "/milkdrop_visualizer"
    query = f"player={quote(player)}" if player else ""
    return urlunsplit((scheme, parts.netloc, path, query, ""))


class WaveformFeed:
    """Holds the waveform frames that are on their way and hands out the one sounding now."""

    def __init__(self) -> None:
        """Initialize an empty feed."""
        self._frames: deque[tuple[int, bytes]] = deque()
        self._syncs: deque[tuple[int, float]] = deque(maxlen=SYNC_SAMPLES)
        self.offset_us: float | None = None
        self.connected = False

    def add_frame(self, data: bytes) -> None:
        """
        Queue a binary message from the relay, if it is a waveform frame.

        :param data: The message as received.
        """
        if len(data) != _HEADER.size + WAVE_SAMPLES or data[0] != WAVE_TAG:
            return
        play_at = _HEADER.unpack_from(data)[1]
        if self._frames and play_at < self._frames[-1][0]:
            # Older audio than the last frame: a replay after a reconnect, or a seek.
            self._frames = deque(f for f in self._frames if f[0] < play_at)
        self._frames.append((play_at, data[_HEADER.size :]))
        while len(self._frames) > MAX_FRAMES:
            self._frames.popleft()

    def add_time_sync(self, sent_us: int, server_rx_us: int, server_tx_us: int) -> None:
        """
        Take one clock sample (NTP style); the one with the shortest round trip wins.

        :param sent_us: Our clock when the ping left.
        :param server_rx_us: The server's clock when the ping arrived.
        :param server_tx_us: The server's clock when it answered.
        """
        received_us = _now_us()
        round_trip = (received_us - sent_us) - (server_tx_us - server_rx_us)
        offset = ((server_rx_us - sent_us) + (server_tx_us - received_us)) / 2.0
        self._syncs.append((round_trip, offset))
        self.offset_us = min(self._syncs)[1]

    def clear(self) -> None:
        """Drop the queued frames (the audio stopped or jumped)."""
        self._frames.clear()

    def reset(self) -> None:
        """Forget frames and the clock: a new connection starts from scratch."""
        self._frames.clear()
        self._syncs.clear()
        self.offset_us = None

    def current(self, ahead_us: int = 0) -> np.ndarray | None:
        """
        Return the waveform sounding ``ahead_us`` from now, as 1024 floats in -1..1.

        :param ahead_us: How far ahead of the music the lights are rendered.
        :return: The waveform, or None when there is none for this moment.
        """
        if self.offset_us is None:
            return None
        playhead = _now_us() + self.offset_us + ahead_us
        frames = self._frames
        due: bytes | None = None
        due_at = 0
        while frames and frames[0][0] <= playhead:
            due_at, due = frames.popleft()
        if due is None or playhead - due_at > STALE_US:
            return None
        return (np.frombuffer(due, np.uint8).astype(np.float32) - 128.0) / 127.0


async def waveform_loop(
    feed: WaveformFeed,
    session: aiohttp.ClientSession,
    web_url: str,
    token: str,
    player: str | None = None,
) -> None:
    """
    Keep a connection to the MilkDrop Visualizer plugin and fill ``feed``; runs until cancelled.

    :param feed: Where the frames go.
    :param session: HTTP session for the websocket.
    :param web_url: Music Assistant's web address.
    :param token: A Music Assistant long-lived access token.
    :param player: The Music Assistant player id to follow; none picks the one playing.
    """
    url = relay_url(web_url, player)
    while True:
        delay = RECONNECT_S
        try:
            async with session.ws_connect(url, heartbeat=25) as ws:
                delay = await _serve(feed, ws, token)
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            LOGGER.debug(
                "MilkDrop waveform not reachable at %s (%s)", url, err or type(err).__name__
            )
        finally:
            feed.connected = False
            feed.reset()
        await asyncio.sleep(delay)


async def _serve(feed: WaveformFeed, ws: aiohttp.ClientWebSocketResponse, token: str) -> float:
    """Sign in, keep the clock in sync and take frames; return how long to wait to reconnect."""
    await ws.send_str(json.dumps({"type": "auth", "token": token}))
    sync_task: asyncio.Task[None] | None = None
    try:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                feed.add_frame(message.data)
            elif message.type == aiohttp.WSMsgType.TEXT:
                try:
                    signed_in = _on_text(feed, json.loads(message.data))
                except (ValueError, KeyError, TypeError) as err:
                    LOGGER.debug("Ignoring a malformed MilkDrop message (%s)", err)
                    continue
                if signed_in and sync_task is None:
                    sync_task = asyncio.create_task(_sync_clock(ws))
    finally:
        if sync_task is not None:
            sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sync_task
    if ws.close_code == AUTH_REJECTED:
        LOGGER.error(
            "Music Assistant refused the MilkDrop token; create a new long-lived token in "
            "Music Assistant (Settings, your profile) and put it in the configuration"
        )
        return RETRY_AFTER_REJECT_S
    return RECONNECT_S


def _on_text(feed: WaveformFeed, data: object) -> bool:
    """
    Handle a text message from the relay.

    :param feed: Where clock samples and stream changes go.
    :param data: The decoded message.
    :return: Whether it confirmed the sign-in.
    """
    if not isinstance(data, dict):
        return False
    kind = data.get("type")
    if kind == "auth_ok":
        LOGGER.info("Receiving the MilkDrop waveform from Music Assistant")
        feed.connected = True
        return True
    if kind == "server/time":
        payload = data["payload"]
        feed.add_time_sync(
            int(payload["client_transmitted"]),
            int(payload["server_received"]),
            int(payload["server_transmitted"]),
        )
    elif kind in ("stream/clear", "stream/end"):
        feed.clear()
    elif kind == "error":
        LOGGER.warning("MilkDrop Visualizer: %s", data.get("message"))
    return False


async def _sync_clock(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Ask the server for its time: quickly at first, then every few seconds."""
    sent = 0
    while not ws.closed:
        await ws.send_str(
            json.dumps({"type": "client/time", "payload": {"client_transmitted": _now_us()}})
        )
        sent += 1
        await asyncio.sleep(SYNC_FAST_S if sent < SYNC_FAST_COUNT else SYNC_SLOW_S)
