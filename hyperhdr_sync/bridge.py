"""HyperHDR light bridge: renders Sendspin visualizer features and streams them to HyperHDR."""

from __future__ import annotations

import re
from collections import deque
from typing import TYPE_CHECKING, cast

from aiosendspin.models.types import UndefinedField
from aiosendspin.models.visualizer import ClientHelloVisualizerSpectrum, VisualizerStatePayload

from music_assistant.providers.sendspin_lights.base import SendspinLightBridge

from .constants import RENDER_RATE_HZ, STALE_BEAT_US
from .engine import (
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    Engine,
)

if TYPE_CHECKING:
    from aiosendspin.models.color import SessionUpdateColor
    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .hyperhdr import HyperHdrUdpSender
    from .layout import LedLayout
    from .provider import HyperHdrProvider
    from .settings import HyperHdrSettings

# Artwork colours in the order they are offered to the palette.
_COLOR_FIELDS = ("primary", "accent", "background_light", "background_dark", "on_dark")
# Frames sent after the music stops, so one lost datagram cannot leave the LEDs lit.
_BLACKOUT_REPEATS = 3
_BLACKOUT_INTERVAL_S = 0.06


class HyperHdrBridge(SendspinLightBridge):
    """Light player for the LEDs of one HyperHDR instance."""

    def __init__(
        self,
        provider: HyperHdrProvider,
        sendspin_provider: SendspinProvider,
        sender: HyperHdrUdpSender,
        layout: LedLayout,
        settings: HyperHdrSettings,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The owning provider.
        :param sendspin_provider: The loaded Sendspin provider.
        :param sender: Opened sender for HyperHDR's UDP raw receiver.
        :param layout: Geometry of the LEDs.
        :param settings: Playback settings.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", provider.instance_id.lower()).strip("-")
        super().__init__(
            provider,
            sendspin_provider,
            client_id=f"hyperhdr-{slug}",
            name=f"HyperHDR ({sender.host})",
            manufacturer="HyperHDR",
            product_name="Ambient LEDs",
            visualizer_request=VisualizerStatePayload(
                # Onsets ("peak") also cover tracks without a computed beat grid.
                types=["beat", "peak", "loudness", "spectrum"],
                rate_max=RENDER_RATE_HZ,
                spectrum=ClientHelloVisualizerSpectrum(
                    n_disp_bins=SPECTRUM_BINS,
                    scale=SPECTRUM_SCALE,  # type: ignore[arg-type]
                    f_min=SPECTRUM_F_MIN,
                    f_max=SPECTRUM_F_MAX,
                ),
            ),
            buffer_capacity=2048,
            render_rate_hz=RENDER_RATE_HZ,
            with_color_role=True,
        )
        self._hyperhdr_provider = provider
        self._sender = sender
        self._engine = Engine(layout, settings.engine)
        self._latency_us = settings.latency_ms * 1000
        self._pending_frames: deque[ExtractedFrame] = deque()
        self._pending_beats: deque[BeatTiming] = deque()
        self._colors: dict[str, tuple[int, int, int] | None] = {}
        self._last_render_s = 0.0

    def start(self) -> None:
        """Register the light player with Sendspin."""
        self.register_client()
        self.logger.info(
            "HyperHDR light player started: %d LEDs at %s:%d",
            self._engine.layout.count,
            self._sender.host,
            self._sender.port,
        )

    async def stop(self) -> None:
        """Switch the LEDs off, remove the light player and close the socket."""
        self._send(self._engine.render_black())
        await self.unregister_client()
        self._sender.close()

    def update_settings(self, settings: HyperHdrSettings) -> None:
        """
        Apply changed playback settings without restarting the bridge.

        :param settings: The new playback settings.
        """
        self._engine.update_settings(settings.engine)
        self._latency_us = settings.latency_ms * 1000

    def set_layout(self, layout: LedLayout) -> None:
        """
        Switch to a new LED layout (the LEDs were reconfigured in HyperHDR).

        :param layout: The new geometry of the LEDs.
        """
        engine = Engine(layout, self._engine.settings)
        engine.set_album_colors(self._album_colors())
        self._engine = engine

    # ------------------------------------------------------------- callbacks

    def _on_stream_start(self) -> None:
        """Reset state and begin rendering."""
        self._pending_frames.clear()
        self._engine.reset()
        self._last_render_s = self.mass.loop.time()
        self._is_streaming = True
        self._start_render_loop()
        self._hyperhdr_provider.refresh_layout_soon()

    def _on_stream_clear(self) -> None:
        """Drop features that belong to pre-seek audio."""
        self._pending_frames.clear()
        self._engine.reset()

    def _on_stream_end(self) -> None:
        """Stop rendering and switch the LEDs off."""
        self._is_streaming = False
        self._cancel_render_loop()
        self._pending_frames.clear()
        self._engine.reset()
        self._blackout(_BLACKOUT_REPEATS)

    def _on_frame(self, frame: ExtractedFrame) -> None:
        """Queue an extracted feature frame until its playback time."""
        if self._is_streaming:
            self._pending_frames.append(frame)

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        """Append a beat schedule segment (it may arrive before the stream starts)."""
        merged = sorted([*self._pending_beats, *beats], key=lambda beat: beat.timestamp_us)
        self._pending_beats = deque(merged)

    def _on_beats_clear(self) -> None:
        """Drop the beat schedule (a track change pushes a fresh one)."""
        self._pending_beats.clear()

    def _on_color(self, payload: ServerStatePayload) -> None:
        """Take over the colours of the current track's artwork."""
        color = cast("SessionUpdateColor | UndefinedField | None", payload.color)
        if color is None or isinstance(color, UndefinedField):
            return
        for name in _COLOR_FIELDS:
            value = getattr(color, name, None)
            # an undefined field keeps its colour, None clears it
            if not isinstance(value, UndefinedField):
                self._colors[name] = value
        self._engine.set_album_colors(self._album_colors())

    # ------------------------------------------------------------- rendering

    def _render(self) -> None:
        """Render the frame for what is audible now and send it to HyperHDR."""
        now_s = self.mass.loop.time()
        dt = now_s - self._last_render_s
        self._last_render_s = now_s
        # Render slightly ahead of the playhead to offset the delay of the lights.
        playhead_us = self.sendspin_server.clock.now_us() + self._latency_us
        frames = self._pending_frames
        while frames and frames[0].timestamp_us <= playhead_us:
            frame = frames.popleft()
            self._engine.feed(loudness=frame.loudness, spectrum=frame.spectrum, onset=frame.peak)
        beats = self._pending_beats
        while beats and beats[0].timestamp_us <= playhead_us:
            beat = beats.popleft()
            if playhead_us - beat.timestamp_us <= STALE_BEAT_US:
                self._engine.beat(1.0 if beat.is_downbeat else 0.8)
        self._send(self._engine.render(dt))

    def _blackout(self, remaining: int) -> None:
        """Send an all-off frame, repeated a few times against packet loss."""
        if self._is_streaming or remaining <= 0:
            return
        self._send(self._engine.render_black())
        self.mass.loop.call_later(_BLACKOUT_INTERVAL_S, self._blackout, remaining - 1)

    def _send(self, rgb: bytes) -> None:
        """Send one frame; an unreachable HyperHDR must never stop the render loop."""
        try:
            self._sender.send(rgb)
        except OSError as err:
            self.logger.debug("Could not send to HyperHDR: %s", err)

    def _album_colors(self) -> list[tuple[int, int, int]]:
        """Return the known artwork colours, most important first."""
        return [color for name in _COLOR_FIELDS if (color := self._colors.get(name)) is not None]
