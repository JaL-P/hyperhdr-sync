"""
Audio-reactive rendering engine: Sendspin visualizer features in, LED colours out.

The engine is fed the raw feature values of Sendspin's visualizer pipeline
(loudness, a 16-band spectrum and onset strength, all on the protocol's own
scales) and renders one RGB value per LED of a :class:`LedLayout` on demand.

It knows nothing about Music Assistant, Sendspin transports or HyperHDR, and
needs numpy only, so the Music Assistant plugin and the standalone bridge share it.

Signal chain per rendered frame:

1. Band values arrive in dB (0 = -60 dB, 65535 = 0 dB), A-weighted by the
   extractor. The weighting is undone so bass is not ~30 dB quieter than mids.
2. Automatic gain: every band is scaled against its own recent maximum (a slowly
   falling ceiling), so quiet and loud tracks, and thin and bass-heavy mixes, all
   use the full brightness range.
3. Attack/release envelopes smooth the bands; bass/mid/treble and a beat pulse
   are derived from them.
4. The selected effect maps those onto the LEDs using their screen positions.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .layout import LedLayout

# Spectrum requested from the visualizer. Eight octaves in half-octave bands: at
# the extractor's ~23 Hz FFT resolution every band, including the lowest, still
# contains at least one FFT bin (narrower low bands would stay empty and dark).
SPECTRUM_BINS: int = 16
SPECTRUM_F_MIN: int = 40
SPECTRUM_F_MAX: int = 10240
SPECTRUM_SCALE: str = "log"

_U16_MAX: float = 65535.0
_DB_RANGE: float = 60.0  # the wire format spans -60 dB .. 0 dB
_SILENCE_DB: float = -120.0

# Automatic gain.
_AGC_WINDOW_DB: float = 28.0  # band level this far below its ceiling renders black
_AGC_FALL_DB_PER_S: float = 1.2  # how fast a ceiling follows the music down
_LEVEL_WINDOW_DB: float = 18.0  # the overall level uses a tighter window than the bands
_AGC_MIN_CEILING_DB: float = -36.0  # ceilings never fall below this, so noise stays dark
_AGC_BAND_SPREAD_DB: float = 24.0  # a band's ceiling stays within this of the loudest band
_A_WEIGHT_MAX_BOOST_DB: float = 30.0

_ATTACK_TAU_S: float = 0.012
_BEAT_DECAY_TAU_S: float = 0.16
_BEAT_REFRACTORY_S: float = 0.14
_GATE_TAU_S: float = 0.35
_HUE_DRIFT_PER_S: float = 0.02
_HUE_BEAT_STEP: float = 0.11
_CONTRAST: float = 1.5

_LUT_SIZE: int = 256
_SCROLL_LENGTH: int = 96
_MAX_RIPPLES: int = 10


class Effect(StrEnum):
    """Available effects."""

    SPECTRUM = "spectrum"
    VU = "vu"
    PULSE = "pulse"
    RIPPLE = "ripple"
    SCROLL = "scroll"
    ENERGY = "energy"
    AMBIENT = "ambient"


class Palette(StrEnum):
    """Available colour palettes."""

    ALBUM = "album"
    RAINBOW = "rainbow"
    SUNSET = "sunset"
    OCEAN = "ocean"
    FIRE = "fire"
    FOREST = "forest"
    NEON = "neon"


class Response(StrEnum):
    """How quickly the lights follow the music."""

    SNAPPY = "snappy"
    BALANCED = "balanced"
    SMOOTH = "smooth"


# (band release time constant, output smoothing time constant), in seconds.
_RESPONSE_TAUS: dict[Response, tuple[float, float]] = {
    Response.SNAPPY: (0.09, 0.0),
    Response.BALANCED: (0.18, 0.035),
    Response.SMOOTH: (0.35, 0.11),
}

_RGB = tuple[int, int, int]

_PALETTE_STOPS: dict[Palette, tuple[_RGB, ...]] = {
    Palette.SUNSET: ((255, 40, 0), (255, 0, 80), (150, 0, 255), (255, 130, 0)),
    Palette.OCEAN: ((0, 30, 255), (0, 190, 255), (0, 255, 140)),
    Palette.FIRE: ((255, 0, 0), (255, 80, 0), (255, 190, 20)),
    Palette.FOREST: ((0, 255, 50), (170, 255, 0), (0, 170, 120)),
    Palette.NEON: ((255, 0, 200), (0, 220, 255), (140, 0, 255)),
}
# Album palette for artwork without colour: warm white through white to cool white.
_MONOCHROME_STOPS: tuple[_RGB, ...] = ((255, 170, 90), (255, 255, 255), (160, 195, 255))


@dataclass(frozen=True)
class EngineSettings:
    """User-facing rendering settings."""

    effect: Effect = Effect.SPECTRUM
    palette: Palette = Palette.ALBUM
    brightness: float = 1.0
    """Output brightness, 0..1."""
    sensitivity_db: float = 0.0
    """Shifts the automatic gain window; positive makes the lights react more."""
    response: Response = Response.BALANCED


class Engine:
    """Renders LED colours for one HyperHDR instance."""

    def __init__(self, layout: LedLayout, settings: EngineSettings | None = None) -> None:
        """
        Initialize the engine.

        :param layout: Geometry of the LEDs to render for.
        :param settings: Rendering settings; defaults are used when omitted.
        """
        self.layout = layout
        self.settings = settings or EngineSettings()
        count = layout.count
        self._pos = _normalize_span(layout.mirror)
        self._ring = _normalize_span(layout.ring)
        # Width of roughly one and a half LEDs along ``_pos``, for soft edges.
        self._soft = max(1.5 / max(count / 2.0, 1.0), 0.02)
        band_pos = self._pos * (SPECTRUM_BINS - 1)
        self._band_lo = np.floor(band_pos).astype(np.intp)
        self._band_hi = np.minimum(self._band_lo + 1, SPECTRUM_BINS - 1)
        self._band_frac = (band_pos - self._band_lo).astype(np.float32)
        self._comp_db = _a_weight_compensation_db()
        self._album_colors: tuple[_RGB, ...] = ()
        self._lut_linear = np.zeros((_LUT_SIZE, 3), np.float32)
        self._lut_cyclic = np.zeros((_LUT_SIZE, 3), np.float32)
        self._rebuild_palette()
        self._out = np.zeros((count, 3), np.float32)
        self.reset()

    # ------------------------------------------------------------------ inputs

    def update_settings(self, settings: EngineSettings) -> None:
        """
        Apply new settings without losing the audio state.

        :param settings: The new settings.
        """
        palette_changed = settings.palette != self.settings.palette
        self.settings = settings
        if palette_changed:
            self._rebuild_palette()

    def set_album_colors(self, colors: Iterable[Sequence[int]] | None) -> None:
        """
        Set the colours taken from the current track's artwork.

        :param colors: RGB triplets (0-255), most important first; None or empty
            when the track has no usable artwork colours.
        """
        cleaned = tuple((int(c[0]), int(c[1]), int(c[2])) for c in colors or () if len(c) >= 3)
        if cleaned != self._album_colors:
            self._album_colors = cleaned
            if self.settings.palette == Palette.ALBUM:
                self._rebuild_palette()

    def reset(self) -> None:
        """Forget all audio state (stream start, seek, stop). Lights fade to black."""
        self._band_db = np.full(SPECTRUM_BINS, _SILENCE_DB, np.float32)
        self._loudness_db = _SILENCE_DB
        self._has_spectrum = False
        self._has_loudness = False
        self._ceiling_db = np.full(SPECTRUM_BINS, _AGC_MIN_CEILING_DB, np.float32)
        self._loudness_ceiling_db = _AGC_MIN_CEILING_DB
        self._bands = np.zeros(SPECTRUM_BINS, np.float32)
        self._level = 0.0
        self._level_slow = 0.0
        self._bass = self._mid = self._treble = 0.0
        self._bass_slow = 0.0
        self._kick = 0.0
        self._pulse = 0.0
        self._gate = 0.0
        self._since_beat_s = 1.0
        self._pending_beat = 0.0
        self._new_beat = 0.0
        self._vu = 0.0
        self._vu_peak = 0.0
        self._ripples: list[list[float]] = []
        self._scroll = np.zeros((_SCROLL_LENGTH, 3), np.float32)
        self._scroll_acc = 0.0
        # ``_time`` and ``_hue`` are deliberately kept so colours do not jump on a seek.
        if not hasattr(self, "_time"):
            self._time = 0.0
            self._hue = 0.0

    def feed(
        self,
        *,
        loudness: int | None = None,
        spectrum: Sequence[int] | np.ndarray | None = None,
        onset: int | None = None,
    ) -> None:
        """
        Feed the feature values that became audible.

        :param loudness: Visualizer ``loudness`` (0-65535, dB-linear).
        :param spectrum: Visualizer ``spectrum`` bins (0-65535 each, dB-linear),
            requested with the ``SPECTRUM_*`` constants of this module.
        :param onset: Visualizer ``peak`` onset strength (1-255).
        """
        if loudness is not None:
            self._loudness_db = _u16_to_db(float(loudness))
            self._has_loudness = True
        if spectrum is not None:
            raw = np.zeros(SPECTRUM_BINS, np.float32)
            values = np.asarray(spectrum, np.float32)[:SPECTRUM_BINS]
            raw[: values.size] = values
            db = raw / _U16_MAX * _DB_RANGE - _DB_RANGE + self._comp_db
            self._band_db = np.where(raw > 0, db, _SILENCE_DB).astype(np.float32)
            self._has_spectrum = True
        if onset is not None and onset > 0:
            self.beat(0.5 + 0.5 * min(1.0, onset / 128.0))

    def beat(self, strength: float = 1.0) -> None:
        """
        Register a beat or onset that became audible.

        :param strength: Relative strength, 0..1.
        """
        self._pending_beat = max(self._pending_beat, min(1.0, max(0.0, strength)))

    # --------------------------------------------------------------- rendering

    def render(self, dt: float) -> bytes:
        """
        Advance the engine and render one frame.

        :param dt: Seconds since the previous call.
        :return: ``R G B`` bytes for every LED, in layout order.
        """
        dt = min(max(dt, 1e-4), 0.25)
        self._advance(dt)
        match self.settings.effect:
            case Effect.SPECTRUM:
                frame = self._fx_spectrum()
            case Effect.VU:
                frame = self._fx_vu(dt)
            case Effect.PULSE:
                frame = self._fx_pulse()
            case Effect.RIPPLE:
                frame = self._fx_ripple(dt)
            case Effect.SCROLL:
                frame = self._fx_scroll(dt)
            case Effect.ENERGY:
                frame = self._fx_energy()
            case _:
                frame = self._fx_ambient()
        frame = np.clip(frame, 0.0, 1.0) * (
            self._gate * min(1.0, max(0.0, self.settings.brightness))
        )
        smooth_tau = _RESPONSE_TAUS[self.settings.response][1]
        if smooth_tau > 0.0:
            self._out += (frame - self._out) * _alpha(dt, smooth_tau)
        else:
            self._out = frame.astype(np.float32)
        return (self._out * 255.0 + 0.5).astype(np.uint8).tobytes()

    def render_black(self) -> bytes:
        """Return an all-off frame and clear the output smoothing."""
        self._out[:] = 0.0
        return bytes(self.layout.count * 3)

    @property
    def is_dark(self) -> bool:
        """Whether the last rendered frame was (practically) all off."""
        return bool(self._out.max() < 1.0 / 255.0)

    # ---------------------------------------------------------------- analysis

    def _advance(self, dt: float) -> None:
        """Update gain, envelopes, band groups and the beat pulse."""
        self._time += dt
        self._hue = (self._hue + _HUE_DRIFT_PER_S * dt) % 1.0
        release_tau = _RESPONSE_TAUS[self.settings.response][0]
        sensitivity = self.settings.sensitivity_db

        if self._has_spectrum:
            ceiling = np.maximum(self._ceiling_db - _AGC_FALL_DB_PER_S * dt, self._band_db)
            floor = max(_AGC_MIN_CEILING_DB, float(ceiling.max()) - _AGC_BAND_SPREAD_DB)
            self._ceiling_db = np.maximum(ceiling, floor)
            target = np.clip(
                (self._band_db - self._ceiling_db + _AGC_WINDOW_DB + sensitivity) / _AGC_WINDOW_DB,
                0.0,
                1.0,
            )
            rising = target > self._bands
            self._bands += (target - self._bands) * np.where(
                rising, _alpha(dt, _ATTACK_TAU_S), _alpha(dt, release_tau)
            )

        if self._has_loudness:
            self._loudness_ceiling_db = max(
                self._loudness_ceiling_db - _AGC_FALL_DB_PER_S * dt,
                self._loudness_db,
                _AGC_MIN_CEILING_DB,
            )
            level_target = min(
                1.0,
                max(
                    0.0,
                    (self._loudness_db - self._loudness_ceiling_db + _LEVEL_WINDOW_DB + sensitivity)
                    / _LEVEL_WINDOW_DB,
                ),
            )
        else:
            level_target = float(self._bands.mean()) if self._has_spectrum else 0.0
        tau = _ATTACK_TAU_S if level_target > self._level else release_tau
        self._level += (level_target - self._level) * _alpha(dt, tau)
        self._level_slow += (self._level - self._level_slow) * _alpha(dt, 1.2)

        if self._has_spectrum:
            shaped = self._bands**_CONTRAST
            self._bass = _group(shaped[0:4])
            self._mid = _group(shaped[4:10])
            self._treble = _group(shaped[10:16])
        else:
            # Loudness-only stream: drive everything from the overall level.
            self._bass = self._mid = self._treble = self._level**_CONTRAST

        # Kick: how far the bass currently sits above its own recent average.
        self._bass_slow += (self._bass - self._bass_slow) * _alpha(dt, 0.35)
        self._kick = max(0.0, self._bass - self._bass_slow)

        # Beats: onsets/beat grid from outside, plus bass kicks found here, de-duplicated.
        self._since_beat_s += dt
        strength = self._pending_beat
        self._pending_beat = 0.0
        if self._kick > 0.3 and self._bass > 0.45:
            strength = max(strength, min(1.0, 0.4 + self._kick))
        self._pulse *= math.exp(-dt / _BEAT_DECAY_TAU_S)
        self._new_beat = 0.0
        if strength > 0.0 and self._since_beat_s >= _BEAT_REFRACTORY_S:
            self._since_beat_s = 0.0
            self._new_beat = strength
            self._pulse = max(self._pulse, strength)
            self._hue = (self._hue + _HUE_BEAT_STEP) % 1.0

        audible = self._level > 0.01 or float(self._bands.max()) > 0.02
        self._gate += ((1.0 if audible else 0.0) - self._gate) * _alpha(dt, _GATE_TAU_S)

    # ----------------------------------------------------------------- effects

    def _fx_spectrum(self) -> np.ndarray:
        """Bass at the bottom of both sides rising to treble at top-centre."""
        value = self._bands[self._band_lo] * (1.0 - self._band_frac)
        value += self._bands[self._band_hi] * self._band_frac
        value = value**_CONTRAST * (0.85 + 0.15 * self._pulse)
        frame: np.ndarray = _sample(self._lut_linear, self._pos) * value[:, None]
        return frame

    def _fx_vu(self, dt: float) -> np.ndarray:
        """Level meter climbing both sides, with a falling peak marker."""
        tau = _ATTACK_TAU_S if self._level > self._vu else 0.12
        self._vu += (self._level - self._vu) * _alpha(dt, tau)
        self._vu_peak = max(self._vu, self._vu_peak - 0.35 * dt)
        lit = np.clip((self._vu - self._pos) / self._soft + 0.5, 0.0, 1.0)
        frame = _sample(self._lut_linear, self._pos) * lit[:, None]
        if self._vu_peak > 0.03:
            marker = np.clip(1.0 - np.abs(self._pos - self._vu_peak) / self._soft, 0.0, 1.0)
            frame = np.maximum(frame, marker[:, None] * 0.9)
        return frame

    def _fx_pulse(self) -> np.ndarray:
        """All LEDs breathe with the bass and flash on beats; the colour steps on each beat."""
        value = 0.16 + 0.45 * self._bass + 0.55 * self._pulse + 0.4 * self._kick
        return _sample_wrap(self._lut_cyclic, self._hue + self._ring * 0.12) * min(1.0, value)

    def _fx_ripple(self, dt: float) -> np.ndarray:
        """Every beat launches a coloured wave from the bottom of both sides to top-centre."""
        if self._new_beat > 0.0:
            self._ripples.append([-0.05, self._new_beat, self._hue])
            del self._ripples[:-_MAX_RIPPLES]
        frame = _sample_wrap(self._lut_cyclic, self._hue + self._ring * 0.1) * (
            0.07 + 0.1 * self._level
        )
        fade = math.exp(-dt / 0.9)
        for ripple in self._ripples:
            ripple[0] += 0.85 * dt
            ripple[1] *= fade
            shape = np.exp(-(((self._pos - ripple[0]) / 0.1) ** 2)) * ripple[1]
            frame = frame + _sample_one(self._lut_cyclic, ripple[2]) * shape[:, None]
        self._ripples = [r for r in self._ripples if r[0] < 1.3 and r[1] > 0.02]
        return frame

    def _fx_scroll(self, dt: float) -> np.ndarray:
        """Feed the current sound colour in at the bottom and scroll it up to top-centre."""
        low, mid, high = (_sample_one(self._lut_linear, t) for t in (0.0, 0.5, 1.0))
        total = self._bass + self._mid + self._treble
        color = (low * self._bass + mid * self._mid + high * self._treble) / max(total, 1e-3)
        color = color * min(
            1.0, max(self._bass, self._mid, self._treble) * (0.8 + 0.5 * self._pulse)
        )
        self._scroll_acc += 0.55 * _SCROLL_LENGTH * dt
        steps = min(int(self._scroll_acc), _SCROLL_LENGTH)
        if steps > 0:
            self._scroll_acc -= steps
            self._scroll = np.roll(self._scroll, steps, axis=0)
            self._scroll[:steps] = color
        return _sample(self._scroll, self._pos)

    def _fx_energy(self) -> np.ndarray:
        """Three overlapping colour bars (bass, mids, treble) growing from the bottom."""
        frame = np.zeros((self.layout.count, 3), np.float32)
        for t, extent in ((0.0, self._bass), (0.5, self._mid * 0.75), (1.0, self._treble * 0.5)):
            lobe = np.clip((extent - self._pos) / self._soft + 0.5, 0.0, 1.0)
            frame += _sample_one(self._lut_linear, t) * lobe[:, None]
        return frame

    def _fx_ambient(self) -> np.ndarray:
        """Slowly rotating colours whose brightness gently follows the music."""
        value = 0.3 + 0.5 * self._level_slow + 0.2 * self._pulse
        return _sample_wrap(self._lut_cyclic, self._ring * 0.5 + self._time * 0.03) * min(
            1.0, value
        )

    # ---------------------------------------------------------------- palettes

    @property
    def album_colors(self) -> tuple[tuple[int, int, int], ...]:
        """The artwork colours last given to :meth:`set_album_colors`."""
        return self._album_colors

    def _rebuild_palette(self) -> None:
        """Rebuild both colour lookup tables for the selected palette."""
        self._lut_linear, self._lut_cyclic = _palette_luts(
            self.settings.palette, self._album_colors
        )


def palette_swatch(
    palette: Palette, album_colors: Iterable[Sequence[int]] = (), count: int = 8
) -> list[tuple[int, int, int]]:
    """
    Return colours spread across a palette's gradient, e.g. to show it in a user interface.

    :param palette: The palette.
    :param album_colors: Artwork colours, used by :attr:`Palette.ALBUM`.
    :param count: How many colours to return.
    """
    cleaned = tuple((int(c[0]), int(c[1]), int(c[2])) for c in album_colors if len(c) >= 3)
    linear, _ = _palette_luts(palette, cleaned)
    at = np.linspace(0.0, 1.0, max(count, 2))
    return [
        (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))
        for r, g, b in _sample(linear, at).tolist()
    ]


# ------------------------------------------------------------------- helpers


def _palette_luts(
    palette: Palette, album_colors: tuple[_RGB, ...]
) -> tuple[np.ndarray, np.ndarray]:
    """Return the (linear, cyclic) colour lookup tables of a palette."""
    stops: tuple[_RGB, ...] | None = None
    if palette == Palette.ALBUM:
        # Black-and-white artwork has no colour to show: light it in shades of white
        # rather than the rainbow, whose bass end would turn the LEDs red.
        stops = _vivid_stops(album_colors) or (_MONOCHROME_STOPS if album_colors else None)
    elif palette in _PALETTE_STOPS:
        stops = _PALETTE_STOPS[palette]
    if stops:
        colors = np.array(stops, np.float32) / 255.0
        return _gradient(colors, cyclic=False), _gradient(colors, cyclic=True)
    # Rainbow (also the fallback when the artwork has no usable colours). The
    # linear table stops at violet so both ends of a bar are not the same red.
    hues = np.arange(_LUT_SIZE) / _LUT_SIZE
    cyclic = np.array([colorsys.hsv_to_rgb(h, 1.0, 1.0) for h in hues], np.float32)
    linear = np.array([colorsys.hsv_to_rgb(h * 0.8, 1.0, 1.0) for h in hues], np.float32)
    return linear, cyclic


def _alpha(dt: float, tau: float) -> float:
    """Smoothing factor of a one-pole filter with time constant ``tau`` for step ``dt``."""
    return 1.0 if tau <= 0.0 else 1.0 - math.exp(-dt / tau)


def _u16_to_db(value: float) -> float:
    """Convert a dB-linear uint16 feature value to dB (-60..0), or silence for 0."""
    return _SILENCE_DB if value <= 0 else value / _U16_MAX * _DB_RANGE - _DB_RANGE


def _group(bands: np.ndarray) -> float:
    """Level of a group of bands: between their average and their loudest member."""
    return float(0.5 * bands.mean() + 0.5 * bands.max())


def _normalize_span(values: np.ndarray) -> np.ndarray:
    """Stretch coordinates so the LEDs actually present span the full 0..1 range."""
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-6:
        return np.full(values.shape, 0.5, np.float32)
    return ((values - low) / (high - low)).astype(np.float32)


def _a_weight_compensation_db() -> np.ndarray:
    """Per-band gain (dB) that undoes the extractor's A-weighting at each band centre."""
    octaves = math.log2(SPECTRUM_F_MAX / SPECTRUM_F_MIN)
    centres = SPECTRUM_F_MIN * 2.0 ** ((np.arange(SPECTRUM_BINS) + 0.5) * octaves / SPECTRUM_BINS)
    f2 = centres.astype(np.float64) ** 2
    ra = (12194.0**2 * f2**2) / (
        (f2 + 20.6**2) * np.sqrt((f2 + 107.7**2) * (f2 + 737.9**2)) * (f2 + 12194.0**2)
    )
    a_db = np.clip(2.0 + 20.0 * np.log10(ra), -50.0, 6.0)
    boost: np.ndarray = np.clip(-a_db, -6.0, _A_WEIGHT_MAX_BOOST_DB).astype(np.float32)
    return boost


def _gradient(colors: np.ndarray, *, cyclic: bool) -> np.ndarray:
    """Build a lookup table blending through ``colors`` (optionally back to the first)."""
    stops = np.vstack([colors, colors[:1]]) if cyclic else colors
    if len(stops) == 1:
        return np.repeat(stops, _LUT_SIZE, axis=0).astype(np.float32)
    span = len(stops) - 1
    at = np.arange(_LUT_SIZE) / (_LUT_SIZE if cyclic else _LUT_SIZE - 1) * span
    low = np.minimum(at.astype(np.intp), span - 1)
    frac = (at - low)[:, None]
    table: np.ndarray = (stops[low] * (1.0 - frac) + stops[low + 1] * frac).astype(np.float32)
    return table


def _sample(lut: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Look up colours at positions ``at``, clamped to 0..1 (for bars and gradients)."""
    size = len(lut)
    return lut[np.clip((at * size).astype(np.intp), 0, size - 1)]


def _sample_wrap(lut: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Look up colours at positions ``at``, wrapping around (for cyclic colour wheels)."""
    size = len(lut)
    colors: np.ndarray = lut[np.minimum((np.mod(at, 1.0) * size).astype(np.intp), size - 1)]
    return colors


def _sample_one(lut: np.ndarray, at: float) -> np.ndarray:
    """Look up one colour; 1.0 maps to the last entry instead of wrapping."""
    size = len(lut)
    color: np.ndarray = lut[size - 1] if at >= 1.0 else lut[int((at % 1.0) * size)]
    return color


def _vivid_stops(colors: tuple[_RGB, ...]) -> tuple[_RGB, ...] | None:
    """
    Turn artwork colours into palette stops bright enough for LEDs.

    Artwork colours are often dark or greyish. Each is scaled to full brightness,
    near-greys and near-duplicates are dropped, and a single surviving colour gets
    a neighbouring hue so effects still show a gradient.
    """
    hues: list[float] = []
    stops: list[_RGB] = []
    for red, green, blue in colors:
        hue, sat, value = colorsys.rgb_to_hsv(red / 255.0, green / 255.0, blue / 255.0)
        if value < 0.08 or sat < 0.2:
            continue
        if any(min(abs(hue - h), 1.0 - abs(hue - h)) < 0.04 for h in hues):
            continue
        hues.append(hue)
        vivid = colorsys.hsv_to_rgb(hue, min(1.0, 0.35 + sat), 1.0)
        stops.append((round(vivid[0] * 255), round(vivid[1] * 255), round(vivid[2] * 255)))
    if not stops:
        return None
    if len(stops) == 1:
        shifted = colorsys.hsv_to_rgb((hues[0] + 0.09) % 1.0, 1.0, 1.0)
        stops.append((round(shifted[0] * 255), round(shifted[1] * 255), round(shifted[2] * 255)))
    return tuple(stops)
