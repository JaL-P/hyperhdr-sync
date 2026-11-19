"""
Standalone HyperHDR music sync bridge.

Does the same as the "HyperHDR Sync" Music Assistant plugin in this repository, but
runs as its own small program, for example on the PC that runs HyperHDR. It joins
Music Assistant as a Sendspin visualizer client over the network, so nothing has to
be installed into Music Assistant: the bridge shows up there as a player that you
group with the speaker the lights should follow.

    python standalone/hyperhdr_bridge.py --server ws://MUSIC-ASSISTANT-HOST:8927/sendspin

Run with --demo to drive the LEDs with a built-in test beat, without Music Assistant.
Needs Python 3.12+ and the packages in standalone/requirements.txt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import logging
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import numpy as np

# The rendering engine lives in the plugin's folder, next to this folder; the bridge's
# own modules (web UI, MilkDrop waveform) live in this folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from milkdrop import WaveformFeed, waveform_loop, web_url_from_sendspin
from milkdrop_render import LIVE, PREVIEW, MilkdropRenderer

from hyperhdr_sync.engine import (
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    AlbumMode,
    Effect,
    Engine,
    EngineSettings,
    Palette,
    Response,
    hex_color,
    parse_hex_color,
)
from hyperhdr_sync.hyperhdr import (
    DEFAULT_UDP_PORT,
    DEFAULT_WEB_PORT,
    MAX_LEDS,
    HyperHdrError,
    HyperHdrUdpSender,
    fetch_serverinfo,
)
from hyperhdr_sync.layout import LedLayout

if TYPE_CHECKING:
    from aiosendspin.client import SendspinClient
    from aiosendspin.models.core import ServerStatePayload, StreamStartMessage
    from aiosendspin.models.visualizer import VisualizerFrame
    from aiosendspin.noise.keys import Identity

LOGGER = logging.getLogger("hyperhdr_bridge")

RENDER_RATE_HZ = 40
RECONNECT_INTERVAL_S = 5.0
# Bytes of visualizer data Music Assistant may send ahead (a few seconds' worth).
BUFFER_CAPACITY = 16384
STALE_BEAT_US = 250_000
COLOR_FIELDS = ("primary", "accent", "background_light", "background_dark", "on_dark")
DEFAULT_STATE_DIR = Path.home() / ".hyperhdr-sync-bridge"


class Bridge:
    """Receives visualizer features from Music Assistant and renders them to HyperHDR."""

    def __init__(
        self,
        engine: Engine,
        sender: HyperHdrUdpSender,
        latency_ms: int,
        night_light: float = 0.0,
        name: str = "HyperHDR LEDs",
    ) -> None:
        """
        Initialize the bridge.

        :param engine: The rendering engine for the LEDs.
        :param sender: Opened sender for HyperHDR's UDP raw receiver.
        :param latency_ms: How far ahead of the music the lights are rendered.
        :param night_light: Brightness (0..1) of the custom colour shown while no music
            plays; 0 hands the LEDs back to HyperHDR instead.
        :param name: The player name shown in Music Assistant.
        """
        self.engine = engine
        self.name = name
        # Where the music comes from, for the web UI: "connecting" until Music Assistant
        # answers, then "connected"; "demo" while the built-in test beat plays instead.
        self.source = "connecting"
        self.sender = sender
        self.latency_us = latency_ms * 1000
        self.night_light = night_light
        self.client: SendspinClient | None = None
        # The sound wave from Music Assistant's MilkDrop Visualizer plugin, when set up.
        self.waveform: WaveformFeed | None = None
        # Draws the real MilkDrop presets for the MilkDrop effect and palette, when set up,
        # and the preset chosen for the LEDs and for the preview (empty: a random one).
        self.milkdrop: MilkdropRenderer | None = None
        self.milkdrop_preset = ""
        self.preview_milkdrop_preset = ""
        self._milkdrop_warned = False
        self.streaming = False
        # Settings being tried out in the web UI while music plays. When they differ from
        # the live ones, ``preview`` is a copy of the live engine (same gain, colours and
        # beat state) with those settings; it is fed and rendered in the same steps as the
        # live engine, but its frames never reach the LEDs.
        self.preview_settings: EngineSettings | None = None
        self.preview: Engine | None = None
        # The frame most recently sent to HyperHDR, shown as "now" in the web UI, and the
        # preview frame rendered in the same step.
        self.last_rgb = engine.render_black()
        self.last_preview_rgb = self.last_rgb
        self._frames: deque[VisualizerFrame] = deque()
        self._beats: list[VisualizerFrame] = []
        self._colors: dict[str, tuple[int, int, int] | None] = {}
        self._stats = {"frames": 0, "beats": 0, "peak": 0}

    @property
    def latency_ms(self) -> int:
        """How far ahead of the music the lights are rendered."""
        return self.latency_us // 1000

    def apply(
        self,
        settings: EngineSettings,
        latency_ms: int,
        night_light: float,
        milkdrop_preset: str = "",
    ) -> None:
        """
        Switch the LEDs to new settings, keeping the audio state.

        :param settings: The new rendering settings.
        :param latency_ms: How far ahead of the music to render.
        :param night_light: Night light brightness, 0..1 (0: off).
        :param milkdrop_preset: The MilkDrop preset; empty for a random one now and then.
        """
        self.engine.update_settings(settings)
        self.latency_us = latency_ms * 1000
        self.night_light = night_light
        self.milkdrop_preset = milkdrop_preset
        if self.preview_settings is not None:
            self.set_preview(self.preview_settings, self.preview_milkdrop_preset)
        LOGGER.info(
            "Settings applied: %s, %s colours, brightness %d%%, sensitivity %+.0f dB, %s, "
            "%d ms, colour %s, night light %d%%",
            settings.effect,
            settings.palette,
            round(settings.brightness * 100),
            settings.sensitivity_db,
            settings.response,
            latency_ms,
            hex_color(settings.color),
            round(night_light * 100),
        )

    def set_preview(self, settings: EngineSettings, milkdrop_preset: str | None = None) -> None:
        """
        Preview other settings against the music, starting from the live engine's state.

        With the live settings the preview is the live frame itself. Otherwise the live
        engine is copied (automatic gain, colour drift, beat pulse, even the random
        sequence of Party) and given the new settings, so the two pictures differ only
        by what was changed.

        :param settings: The settings to preview.
        :param milkdrop_preset: The MilkDrop preset to preview; None keeps the live one.
        """
        if milkdrop_preset is not None:
            self.preview_milkdrop_preset = milkdrop_preset
        same_preset = self.preview_milkdrop_preset == self.milkdrop_preset
        if settings == self.engine.settings and (same_preset or not settings.uses_milkdrop):
            self.preview = None
        elif self.preview is None or self.preview_settings != settings:
            if self.preview is None:
                self.preview = copy.deepcopy(self.engine)
            self.preview.update_settings(settings)
        self.preview_settings = settings

    def stop_preview(self) -> None:
        """Stop previewing."""
        self.preview = None
        self.preview_settings = None

    # ---------------------------------------------------- Sendspin callbacks

    def on_stream_start(self, message: StreamStartMessage) -> None:
        """Start rendering when Music Assistant starts a visualizer stream."""
        if message.payload.visualizer is None:
            return
        if not self.streaming:
            LOGGER.info("Music started, driving the LEDs")
            self._frames.clear()
            self.engine.reset()
            self._refork_preview()
        self.streaming = True

    def on_stream_end(self, roles: list[str] | None) -> None:
        """Stop rendering and switch the LEDs off when the stream ends."""
        if roles is not None and "visualizer" not in roles:
            return
        if self.streaming:
            LOGGER.info("Music stopped, releasing the LEDs")
        self.stop_stream()

    def on_stream_clear(self, roles: list[str] | None) -> None:
        """Drop features that belong to audio before a seek."""
        if roles is None or "visualizer" in roles:
            self._frames.clear()
            self._beats.clear()
            self.engine.reset()
            self._refork_preview()

    def on_frames(self, frames: list[VisualizerFrame]) -> None:
        """Queue feature frames until their playback time."""
        for frame in frames:
            if frame.is_downbeat is not None:
                if not self.streaming:
                    continue
                self._beats.append(frame)
                self._beats.sort(key=lambda beat: beat.timestamp_us)
            elif self.streaming:
                self._frames.append(frame)

    def on_color(self, payload: ServerStatePayload | None) -> None:
        """Take over the colours of the current track's artwork."""
        color = payload.color if payload is not None else None
        if color is None:
            return
        for name in COLOR_FIELDS:
            value = getattr(color, name, None)
            # a field the server left undefined keeps its colour, None clears it
            if value is None:
                self._colors[name] = None
            elif isinstance(value, (tuple, list)) and len(value) >= 3:
                self._colors[name] = (int(value[0]), int(value[1]), int(value[2]))
        colors = [c for name in COLOR_FIELDS if (c := self._colors.get(name)) is not None]
        self.engine.set_album_colors(colors)
        if self.preview is not None:
            self.preview.set_album_colors(colors)

    def stop_stream(self) -> None:
        """Stop rendering; the render loop then blacks the LEDs out."""
        self.streaming = False
        self._frames.clear()
        self._beats.clear()
        self.engine.reset()
        self._refork_preview()

    def _refork_preview(self) -> None:
        """Start the preview afresh from the (just reset) live engine."""
        if self.preview_settings is not None:
            self.preview = None
            self.set_preview(self.preview_settings)

    # ----------------------------------------------------------- render loop

    async def render_loop(self) -> None:
        """Render at a fixed rate while music plays; send a few black frames when it stops."""
        period = 1.0 / RENDER_RATE_HZ
        last = time.perf_counter()
        blackouts_left = 0
        next_report = last + 10.0
        while True:
            await asyncio.sleep(period)
            now = time.perf_counter()
            dt, last = now - last, now
            if self.streaming and self.client is not None:
                playhead_us = (
                    self.client.compute_server_time(self.client.now_us()) + self.latency_us
                )
                self._drain(playhead_us)
                wave = self._feed_waveform()
                await self._feed_milkdrop(wave)
                rgb = self.engine.render(dt)
                self.last_preview_rgb = rgb if self.preview is None else self.preview.render(dt)
                self._stats["peak"] = max(self._stats["peak"], *rgb)
                self._send(rgb)
                blackouts_left = 3
                if now >= next_report:
                    next_report = now + 10.0
                    LOGGER.debug(
                        "Last 10 s: %(frames)d feature frames, %(beats)d beats, brightest LED %(peak)d/255",
                        self._stats,
                    )
                    self._stats = {"frames": 0, "beats": 0, "peak": 0}
            elif self.night_light > 0.0:
                # Keep the LEDs (instead of HyperHDR's grabber) and glow in the custom colour.
                self._send(self.engine.render_night_light(dt, self.night_light))
                blackouts_left = 3
            elif blackouts_left > 0:
                # HyperHDR hands the LEDs back to its other sources 3 seconds after the last frame.
                self._send(self.engine.render_black())
                blackouts_left -= 1

    def _drain(self, playhead_us: int) -> None:
        """Feed the engine everything that is audible by now."""
        frames = self._frames
        engines = [self.engine] if self.preview is None else [self.engine, self.preview]
        while frames and frames[0].timestamp_us <= playhead_us:
            frame = frames.popleft()
            self._stats["frames"] += 1
            for engine in engines:
                engine.feed(
                    loudness=frame.loudness, spectrum=frame.spectrum, onset=frame.peak_strength
                )
        while self._beats and self._beats[0].timestamp_us <= playhead_us:
            beat = self._beats.pop(0)
            if playhead_us - beat.timestamp_us <= STALE_BEAT_US:
                self._stats["beats"] += 1
                for engine in engines:
                    engine.beat(1.0 if beat.is_downbeat else 0.8)

    def _feed_waveform(self) -> np.ndarray | None:
        """Give the engines the sound wave sounding now, if the MilkDrop plugin sends it."""
        if self.waveform is None:
            return None
        wave = self.waveform.current(self.latency_us)
        if wave is not None:
            self.engine.feed(waveform=wave)
            if self.preview is not None:
                self.preview.feed(waveform=wave)
        return wave

    async def _feed_milkdrop(self, wave: np.ndarray | None) -> None:
        """Have the MilkDrop presets drawn for the engines that show them."""
        await self.render_milkdrop(self.engine, LIVE, wave)
        if self.preview is not None:
            await self.render_milkdrop(self.preview, PREVIEW, wave)

    async def render_milkdrop(
        self, engine: Engine, slot: int, wave: np.ndarray | None = None
    ) -> None:
        """
        Send the next waveform to a MilkDrop slot and give ``engine`` its latest picture.

        :param engine: The engine showing that slot.
        :param slot: ``LIVE`` (the LEDs) or ``PREVIEW`` (the web UI).
        :param wave: The real sound wave (-1..1), if there is one; else one made from the
            engine's spectrum.
        """
        renderer = self.milkdrop
        if renderer is None or not engine.settings.uses_milkdrop:
            return
        if (reason := renderer.unavailable_reason) is not None:
            if not self._milkdrop_warned:
                LOGGER.warning("%s; showing Liquid instead", reason)
                self._milkdrop_warned = True
            return
        data = (
            engine.synth_waveform()
            if wave is None
            else np.rint(np.clip(wave, -1.0, 1.0) * 127.0 + 128.0).astype(np.uint8).tobytes()
        )
        preset = self.milkdrop_preset if slot == LIVE else self.preview_milkdrop_preset
        renderer.set_preset(slot, preset)
        await renderer.render(slot, data)
        picture = renderer.picture(slot)
        if picture is not None:
            engine.feed(picture=picture)

    def _send(self, rgb: bytes) -> None:
        self.last_rgb = rgb
        with contextlib.suppress(OSError):
            self.sender.send(rgb)


def load_or_create_identity(key_file: Path) -> Identity:
    """
    Return the bridge's Sendspin identity, creating it on first run.

    A stable identity keeps the same player (and its group) in Music Assistant across
    restarts. A new key file is created owner-only from the start, never widened later.

    :param key_file: File holding the 32-byte private key.
    """
    from aiosendspin.noise.keys import Identity  # noqa: PLC0415

    try:
        return Identity.from_private_bytes(key_file.read_bytes())
    except FileNotFoundError:
        pass
    identity = Identity.generate()
    # O_EXCL: refuse to reuse a file that appeared meanwhile, which may have other permissions.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(identity.private_bytes)
    return identity


async def connect_loop(
    bridge: Bridge, args: argparse.Namespace, session: aiohttp.ClientSession
) -> None:
    """Stay connected to Music Assistant's Sendspin server, retrying while it is unreachable."""
    from aiosendspin.client import PairingSupport, SendspinClient  # noqa: PLC0415
    from aiosendspin.models.core import DeviceInfo  # noqa: PLC0415
    from aiosendspin.models.types import Roles  # noqa: PLC0415
    from aiosendspin.models.visualizer import (  # noqa: PLC0415
        ClientHelloVisualizerSpectrum,
        ClientHelloVisualizerSupport,
        VisualizerStatePayload,
    )
    from aiosendspin.noise.trust_store import FileClientPairingStore  # noqa: PLC0415

    state_dir = Path(args.state_dir)
    # Private to this user: it holds the bridge's Sendspin private key and pairing secrets.
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = load_or_create_identity(state_dir / "identity.key")
    store = await FileClientPairingStore.open(state_dir / "pairing.json")
    # The bridge only receives sound features, which Music Assistant hands out without pairing.
    await store.store_pairing_config(
        replace(await store.get_pairing_config(), unpaired_access_enabled=True)
    )
    client = SendspinClient(
        identity,
        args.name,
        [Roles.VISUALIZER, Roles.COLOR],
        pairing_store=store,
        device_info=DeviceInfo(product_name="HyperHDR Sync bridge", manufacturer="HyperHDR"),
        visualizer_support=ClientHelloVisualizerSupport(buffer_capacity=BUFFER_CAPACITY),
        visualizer_state=VisualizerStatePayload(
            types=["beat", "peak", "loudness", "spectrum"],
            rate_max=RENDER_RATE_HZ,
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=SPECTRUM_BINS,
                scale=SPECTRUM_SCALE,  # type: ignore[arg-type]
                f_min=SPECTRUM_F_MIN,
                f_max=SPECTRUM_F_MAX,
            ),
        ),
        pairing_support=PairingSupport(offer_static_pairing_code=False),
        session=session,
    )
    disconnected = asyncio.Event()
    client.add_disconnect_listener(disconnected.set)
    client.add_stream_start_listener(bridge.on_stream_start)
    client.add_stream_end_listener(bridge.on_stream_end)
    client.add_stream_clear_listener(bridge.on_stream_clear)
    client.add_visualizer_listener(bridge.on_frames)
    client.add_color_listener(bridge.on_color)
    bridge.client = client
    LOGGER.info("Sendspin client id: %s", identity.peer_id)
    try:
        while True:
            disconnected.clear()
            try:
                await client.connect(args.server)
            except Exception as err:  # noqa: BLE001 - any failure is retried
                LOGGER.warning(
                    "Cannot connect to %s (%s), retrying", args.server, err or type(err).__name__
                )
                await asyncio.sleep(RECONNECT_INTERVAL_S)
                continue
            LOGGER.info(
                "Connected to Music Assistant as '%s'. Group this player with a speaker there.",
                args.name,
            )
            bridge.source = "connected"
            await disconnected.wait()
            LOGGER.warning("Disconnected from Music Assistant, reconnecting")
            bridge.source = "connecting"
            bridge.stop_stream()
            await asyncio.sleep(RECONNECT_INTERVAL_S)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def demo_loop(bridge: Bridge) -> None:
    """Drive the engine with a synthetic 120 BPM track, to test the LEDs without Music Assistant."""
    LOGGER.info("Demo mode: playing a built-in test beat on the LEDs (Ctrl+C to stop)")
    bridge.source = "demo"
    period = 1.0 / RENDER_RATE_HZ
    start = last = time.perf_counter()
    last_beat = -1
    while True:
        await asyncio.sleep(period)
        now = time.perf_counter()
        dt, last = now - last, now
        loudness, spectrum, beat = demo_features(now - start)
        onset = 200 if beat != last_beat else None
        last_beat = beat
        # (the web UI feeds its preview the test beat itself while no music streams)
        bridge.engine.feed(loudness=loudness, spectrum=spectrum, onset=onset)
        await bridge.render_milkdrop(bridge.engine, LIVE)
        bridge._send(bridge.engine.render(dt))  # noqa: SLF001


def demo_features(t: float) -> tuple[int, list[int], int]:
    """
    Return the sound features of a synthetic 120 BPM track at time ``t``.

    :param t: Seconds since the track started.
    :return: Loudness, spectrum (both on the visualizer's scale) and the number of
        the current beat; a new number means a beat just started.
    """
    beat_pos = t * 2.0  # two beats per second
    kick = math.exp(-(beat_pos % 1.0) * 6.0)
    hat = math.exp(-((beat_pos + 0.5) % 1.0) * 9.0)
    sweep = 0.5 + 0.5 * math.sin(t * 0.7)
    spectrum = []
    for band in range(SPECTRUM_BINS):
        x = band / (SPECTRUM_BINS - 1)
        low = kick * math.exp(-(((x - 0.08) / 0.12) ** 2))
        mid = (0.45 + 0.3 * math.sin(t * 1.9 + band)) * math.exp(
            -(((x - sweep * 0.6 - 0.2) / 0.18) ** 2)
        )
        high = hat * math.exp(-(((x - 0.9) / 0.12) ** 2))
        level = min(1.0, max(low, mid, high))
        spectrum.append(int((0.35 + 0.5 * level) * 65535) if level > 0.02 else 9000)
    return int((0.55 + 0.3 * kick) * 65535), spectrum, int(beat_pos)


async def load_layout(args: argparse.Namespace, session: aiohttp.ClientSession) -> LedLayout:
    """Read the LED layout from HyperHDR, waiting until HyperHDR can be reached."""
    token = args.token or os.environ.get("HYPERHDR_TOKEN") or None
    while True:
        try:
            info = await fetch_serverinfo(session, args.hyperhdr_host, args.web_port, token)
            layout = LedLayout.from_serverinfo(info.get("leds") or [])
        except (HyperHdrError, ValueError) as err:
            LOGGER.warning("%s; retrying in %.0f s", err, RECONNECT_INTERVAL_S)
            await asyncio.sleep(RECONNECT_INTERVAL_S)
            continue
        if layout.count > MAX_LEDS:
            raise SystemExit(f"HyperHDR has {layout.count} LEDs; at most {MAX_LEDS} can be driven.")
        return layout


async def run(args: argparse.Namespace) -> None:
    """Run the bridge until interrupted."""
    async with aiohttp.ClientSession() as session:
        layout = await load_layout(args, session)
        LOGGER.info("HyperHDR at %s has %d LEDs", args.hyperhdr_host, layout.count)
        sender = HyperHdrUdpSender(args.hyperhdr_host, args.udp_port)
        await sender.open()
        settings = EngineSettings(
            effect=Effect(args.effect),
            palette=Palette(args.palette),
            brightness=args.brightness / 100.0,
            sensitivity_db=args.sensitivity,
            response=Response(args.response),
            color=args.color,
            album_mode=AlbumMode(args.album_mode),
            colors=args.colors,
            steps=args.steps,
        )
        bridge = Bridge(
            Engine(layout, settings),
            sender,
            args.latency_ms,
            args.night_light / 100.0,
            name=args.name,
        )
        bridge.milkdrop = MilkdropRenderer(args.chromium)
        bridge.milkdrop_preset = bridge.preview_milkdrop_preset = args.milkdrop_preset
        # Stop as cleanly on SIGTERM (a service or container stopping) as on Ctrl+C.
        task = asyncio.current_task()
        if task is not None and hasattr(signal, "SIGTERM"):
            with contextlib.suppress(NotImplementedError):  # no signal handlers on Windows
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        web_runner = None
        if args.ui_port or args.ui_supervisor:
            from web_ui import start_web_ui  # noqa: PLC0415

            try:
                web_runner = await start_web_ui(
                    bridge,
                    demo_features,
                    session,
                    host=args.ui_host,
                    port=args.ui_port,
                    supervisor=args.ui_supervisor,
                )
            except (OSError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
                # The LEDs matter more than the page: keep running without it.
                LOGGER.warning("Web UI not started: %s", err or type(err).__name__)
        else:
            LOGGER.info("Web UI off (start with --ui-port to use it)")
        try:
            if args.demo:
                await demo_loop(bridge)
            else:
                loops = [bridge.render_loop(), connect_loop(bridge, args, session)]
                if ma_token := args.ma_token or os.environ.get("MA_TOKEN"):
                    web_url = args.ma_url or web_url_from_sendspin(args.server)
                    bridge.waveform = WaveformFeed()
                    LOGGER.info(
                        "Liquid will draw the sound wave from Music Assistant at %s", web_url
                    )
                    loops.append(
                        waveform_loop(
                            bridge.waveform, session, web_url, ma_token, args.milkdrop_player
                        )
                    )
                await asyncio.gather(*loops)
        finally:
            # Spaced out, so they are not dropped together; the socket closes right after.
            for _ in range(3):
                bridge._send(bridge.engine.render_black())  # noqa: SLF001
                await asyncio.sleep(0.05)
            sender.close()
            if web_runner is not None:
                await web_runner.cleanup()
            await bridge.milkdrop.close()


def _parse_colors(value: str) -> tuple[tuple[int, int, int], ...]:
    """
    Parse the ``--colors`` argument: 2 to 6 ``#rrggbb`` colours separated by commas.

    :param value: The argument.
    :raises argparse.ArgumentTypeError: When a colour is invalid or the count is wrong.
    """
    try:
        colors = tuple(parse_hex_color(part) for part in value.split(",") if part.strip())
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from err
    if not 2 <= len(colors) <= 6:
        raise argparse.ArgumentTypeError("give 2 to 6 colours")
    return colors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Sync HyperHDR LEDs to the music played by Music Assistant.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        help="Sendspin address of Music Assistant, e.g. ws://homeassistant.local:8927/sendspin",
    )
    parser.add_argument(
        "--demo", action="store_true", help="play a test beat instead of connecting"
    )
    parser.add_argument(
        "--name", default="HyperHDR LEDs", help="player name shown in Music Assistant"
    )
    parser.add_argument("--hyperhdr-host", default="127.0.0.1", help="machine HyperHDR runs on")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, help="HyperHDR web port")
    parser.add_argument(
        "--udp-port", type=int, default=DEFAULT_UDP_PORT, help="HyperHDR UDP raw receiver port"
    )
    parser.add_argument(
        "--token", help="HyperHDR API token (or set HYPERHDR_TOKEN); not needed on the same machine"
    )
    parser.add_argument(
        "--ma-token",
        help="Music Assistant long-lived token (or set MA_TOKEN): with the MilkDrop Visualizer "
        "plugin enabled, MilkDrop and Liquid follow the real sound wave",
    )
    parser.add_argument(
        "--ma-url",
        help="Music Assistant's web address for the MilkDrop waveform "
        "(default: the --server host, port 8095)",
    )
    parser.add_argument(
        "--milkdrop-preset",
        default="",
        help="MilkDrop preset of the MilkDrop effect (default: a random one every 30 seconds)",
    )
    parser.add_argument(
        "--chromium",
        help="Chromium executable that draws the MilkDrop presets (default: found on the PATH)",
    )
    parser.add_argument(
        "--milkdrop-player",
        help="Music Assistant player id whose sound wave to draw (default: the one playing)",
    )
    parser.add_argument(
        "--effect", default=Effect.SPECTRUM.value, choices=[e.value for e in Effect]
    )
    parser.add_argument(
        "--palette", default=Palette.ALBUM.value, choices=[p.value for p in Palette]
    )
    parser.add_argument("--brightness", type=int, default=100, help="brightness in percent")
    parser.add_argument(
        "--sensitivity", type=float, default=0.0, help="reaction strength in dB (-12..12)"
    )
    parser.add_argument(
        "--response", default=Response.BALANCED.value, choices=[r.value for r in Response]
    )
    parser.add_argument(
        "--latency-ms", type=int, default=120, help="render this far ahead of the music"
    )
    parser.add_argument(
        "--album-mode",
        default=AlbumMode.VIBRANT.value,
        choices=[m.value for m in AlbumMode],
        help="how the album palette picks colours from the cover",
    )
    parser.add_argument(
        "--color",
        type=parse_hex_color,
        default="#ff9329",
        help="custom colour #rrggbb, for the manual effect, the custom palette and the night light",
    )
    parser.add_argument(
        "--colors",
        type=_parse_colors,
        default=(),
        help="the colours of the 'mine' palette: 2 to 6 #rrggbb, separated by commas",
    )
    parser.add_argument(
        "--steps",
        action="store_true",
        help="show the 'mine' palette as blocks of colour instead of blending them",
    )
    parser.add_argument(
        "--night-light",
        type=int,
        default=0,
        choices=range(101),
        metavar="0-100",
        help="while no music plays, glow in the custom colour at this brightness in percent "
        "instead of handing the LEDs back to HyperHDR (0: off)",
    )
    parser.add_argument(
        "--state-dir", default=str(DEFAULT_STATE_DIR), help="where the bridge keeps its identity"
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=0,
        help="serve a web UI with a live view and previews on this port (0: off)",
    )
    parser.add_argument(
        "--ui-host",
        default="127.0.0.1",
        help="address the web UI listens on; 0.0.0.0 opens it to the network (no login!)",
    )
    parser.add_argument(
        "--ui-supervisor",
        action="store_true",
        help="serve the web UI as a Home Assistant add-on Ingress panel",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)
    if not args.demo and not args.server:
        parser.error("give --server ws://HOST:8927/sendspin, or --demo")
    return args


def main() -> None:
    """Entry point."""
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
