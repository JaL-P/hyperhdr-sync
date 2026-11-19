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
    from collections.abc import Callable, Iterable, Sequence

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
# Most flakes, sparks and fireflies drawn at once.
_MAX_PARTICLES: int = 48
# Liquid: the size of the picture it swirls (across, down) and the points on its ring.
_LIQUID_W, _LIQUID_H = 48, 27
_LIQUID_RING = 160
# How long a waveform from the MilkDrop plugin is drawn before Liquid falls back to the spectrum.
_WAVE_FRESH_S = 0.3
# MilkDrop: how long a picture is shown, how often its colours refresh the MilkDrop palette
# (and how quickly the palette follows), and where it is read: like a screen grabber, near
# each LED's edge of the screen, averaged over a little depth.
_PICTURE_FRESH_S = 0.5
_PICTURE_PALETTE_S, _PICTURE_PALETTE_TAU = 0.1, 0.4
_PICTURE_DEPTHS = (0.97, 0.9, 0.83)
# The waveform made from the spectrum when Music Assistant's real one is not available.
_SYNTH_RATE = 44100.0
# MilkDrop's waveform: this many samples, as unsigned bytes with 128 as zero (the format of
# Music Assistant's MilkDrop plugin and of Butterchurn).
WAVE_SAMPLES: int = 1024


class Effect(StrEnum):
    """Available effects."""

    SPECTRUM = "spectrum"
    VU = "vu"
    PULSE = "pulse"
    RIPPLE = "ripple"
    SCROLL = "scroll"
    ENERGY = "energy"
    AMBIENT = "ambient"
    LUMINA = "lumina"
    """Adaptive brightness: one calm colour whose brightness follows the music."""
    COLORA = "colora"
    """Adaptive colour: steady brightness, the colour follows the sound (bass to treble)."""
    RETRO = "retro"
    """Graphic equaliser: eight frequency bands as blocks with peak dots, like an old hi-fi."""
    SCANNER = "scanner"
    """A light sweeping back and forth with a trail; faster when louder."""
    RHYTHM = "rhythm"
    """Colour blocks that swap on every beat."""
    PARTY = "party"
    """Random colour bursts on beats, sparkles on the treble."""
    MANUAL = "manual"
    """One fixed colour on every LED; does not follow the music."""
    # Our own LED takes on effect ideas from Paraline (https://github.com/SamXop123/Paraline)
    # by SamXop123: written from its descriptions, with no code taken from it.
    FLOW = "flow"
    """Comets of light circling the screen; faster when louder, strong beats turn them round."""
    CENTRE_PULSE = "centre_pulse"
    """Each beat sends a pulse from the middle of every edge out to the corners."""
    FIREFLIES = "fireflies"
    """Small lights drifting around the screen; beats give them a burst of speed."""
    SNOWFALL = "snowfall"
    """Glowing flakes falling down the sides of the screen; more with the treble."""
    CRYSTALS = "crystals"
    """Short shards that flare with their own frequency band and flutter with the treble."""
    BRAIDS = "braids"
    """Two strands of colour winding along the sides, crossing as they move."""
    EMBERS = "embers"
    """A flickering glow with sparks rising from the bottom on the bass."""
    AURORA = "aurora"
    """Slow, folded curtains of colour rising from the bottom, taller when louder."""
    HORIZON = "horizon"
    """A sun at the top pulsing with the bass, grid lines rushing down the sides."""
    SIDE_BARS = "side_bars"
    """Bars growing from the middle of each side: bass on the left, treble on the right."""
    LIQUID = "liquid"
    """
    Swirling trails that zoom, twist and fade into themselves, in the style of the
    MilkDrop visualizer (our own code; no MilkDrop code or presets are used).
    """
    MILKDROP = "milkdrop"
    """
    The real MilkDrop presets, drawn by Butterchurn and sampled at the LEDs like a screen
    grabber would. The pictures come from outside (see :meth:`Engine.feed`); the standalone
    bridge renders them in a headless Chromium. Liquid stands in until a picture arrives.
    """


class AlbumMode(StrEnum):
    """How the Album art palette is made from the colours Music Assistant takes from the cover."""

    VIBRANT = "vibrant"
    """All cover colours, each boosted to full brightness and saturation."""
    DOMINANT = "dominant"
    """Only the cover's main colour, with neighbouring shades."""
    ACCENT = "accent"
    """The cover's accent colour first, then its main colour."""
    NATURAL = "natural"
    """The colours as they look on the cover, greys included; only dark ones are lifted."""
    PASTEL = "pastel"
    """The cover colours softened towards white."""
    CONTRAST = "contrast"
    """The main colour against its opposite on the colour wheel."""


class Palette(StrEnum):
    """Available colour palettes."""

    ALBUM = "album"
    RAINBOW = "rainbow"
    SUNSET = "sunset"
    OCEAN = "ocean"
    FIRE = "fire"
    FOREST = "forest"
    NEON = "neon"
    AURORA = "aurora"
    PASTEL = "pastel"
    ICE = "ice"
    CANDY = "candy"
    LAVA = "lava"
    WARM = "warm"
    CLASSIC = "classic"
    """Green, yellow, red: the colours of a classic level meter."""
    CUSTOM = "custom"
    """Shades of the custom colour."""
    MINE = "mine"
    """The user's own colours (:attr:`EngineSettings.colors`), blended or as blocks."""
    MILKDROP = "milkdrop"
    """
    The colours of the MilkDrop picture as Music Assistant shows it: tinted with the cover's
    colour (:meth:`Engine.set_milkdrop_tint`), or the preset's own colours without one. With
    the MilkDrop effect any other palette tints the picture instead; with another effect, the
    palette is the colours round the edges of the live picture. The rainbow until one arrives.
    """


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
    Palette.AURORA: ((0, 255, 140), (0, 140, 255), (170, 0, 255)),
    Palette.PASTEL: ((255, 150, 200), (150, 190, 255), (170, 255, 200), (255, 225, 150)),
    Palette.ICE: ((255, 255, 255), (140, 220, 255), (0, 110, 255)),
    Palette.CANDY: ((255, 50, 160), (130, 80, 255), (0, 220, 220)),
    Palette.LAVA: ((255, 30, 0), (255, 0, 70), (255, 110, 0)),
    Palette.WARM: ((255, 110, 25), (255, 165, 70), (255, 70, 15)),
    Palette.CLASSIC: ((0, 255, 40), (255, 220, 0), (255, 0, 0)),
}

# Warm white, like a 2700 K bulb: the default custom colour (Manual, Custom, night light).
DEFAULT_COLOR: tuple[int, int, int] = (255, 147, 41)

# Frequency bands (blocks) of the Retro equaliser; the spectrum bins are grouped into them.
_RETRO_BANDS: int = 8
# First spectrum bin of each Retro band.
_RETRO_BIN_STARTS: np.ndarray = (np.arange(_RETRO_BANDS) * SPECTRUM_BINS // _RETRO_BANDS).astype(
    np.intp
)
# Fewest LEDs per Retro block that can show a growing bar with a gap and a peak dot;
# smaller blocks show their band by brightness instead.
_RETRO_MIN_BAR_LEDS: float = 4.0
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
    color: tuple[int, int, int] = DEFAULT_COLOR
    """Custom colour (RGB 0-255), for the Manual effect, the Custom palette and the night light."""
    album_mode: AlbumMode = AlbumMode.VIBRANT
    """How the Album art palette picks colours from the cover."""
    colors: tuple[tuple[int, int, int], ...] = ()
    """The colours of :attr:`Palette.MINE` (RGB 0-255), in order."""
    steps: bool = False
    """Show :attr:`Palette.MINE` as separate blocks of colour instead of blending them."""

    @property
    def uses_milkdrop(self) -> bool:
        """Whether these settings show MilkDrop pictures (the MilkDrop effect or palette)."""
        return self.effect == Effect.MILKDROP or self.palette == Palette.MILKDROP


def parse_hex_color(value: str) -> tuple[int, int, int]:
    """
    Parse a ``#rrggbb`` colour.

    :param value: The colour, with or without the ``#``.
    :raises ValueError: When it is not six hexadecimal digits.
    """
    digits = value.strip().removeprefix("#")
    if len(digits) != 6 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise ValueError(f"not a #rrggbb colour: {value!r}")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def hex_color(color: Sequence[int]) -> str:
    """Return an RGB colour as ``#rrggbb``."""
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in color[:3]))


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
        self._rng = np.random.default_rng(7)
        retro_at = self._ring * _RETRO_BANDS
        self._retro_block = np.minimum(retro_at.astype(np.intp), _RETRO_BANDS - 1)
        # 0 in the middle of a Retro block, 1 at its ends.
        self._retro_offset = (np.abs(retro_at - self._retro_block - 0.5) * 2.0).astype(np.float32)
        self._retro_leds = count / _RETRO_BANDS
        # Which edge each LED is on (0 left, 1 right, 2 top, 3 bottom), how far it is from
        # that edge's middle (0) towards its corners (1), and its height (0 bottom, 1 top).
        x, y = layout.x.astype(np.float32), layout.y.astype(np.float32)
        self._edge = np.argmin(np.stack([x, 1.0 - x, y, 1.0 - y]), axis=0).astype(np.intp)
        along = np.where(self._edge < 2, y, x)
        self._from_mid = np.clip(np.abs(along - 0.5) * 2.0, 0.0, 1.0).astype(np.float32)
        self._height = (1.0 - y).astype(np.float32)
        self._side = self._edge < 2
        # Liquid: the position of every pixel of its picture and of every LED on it (-1..1;
        # the LEDs slightly inside the picture's border, where its trails are brightest).
        grid_v, grid_u = np.meshgrid(
            np.linspace(-1.0, 1.0, _LIQUID_H, dtype=np.float32),
            np.linspace(-1.0, 1.0, _LIQUID_W, dtype=np.float32),
            indexing="ij",
        )
        self._liquid_grid = (grid_u, grid_v)
        self._liquid_at = ((x * 2.0 - 1.0) * 0.92, (y * 2.0 - 1.0) * 0.92)
        self._setup_milkdrop(x, y)
        # Crystals: fixed shards around the screen, each tied to one frequency band.
        shards = max(4, count // 14)
        shard_rng = np.random.default_rng(11)
        self._shard_at = (
            (np.arange(shards) + 0.2 + 0.6 * shard_rng.random(shards)) / shards
        ).astype(np.float32)
        self._shard_band = (np.arange(shards) * 7) % SPECTRUM_BINS
        self._shard_color = shard_rng.random(shards).astype(np.float32)
        self._album_colors: tuple[_RGB, ...] = ()
        self._lut_linear = np.zeros((_LUT_SIZE, 3), np.float32)
        self._lut_cyclic = np.zeros((_LUT_SIZE, 3), np.float32)
        self._rebuild_palette()
        self._out = np.zeros((count, 3), np.float32)
        self.reset()

    def _setup_milkdrop(self, x: np.ndarray, y: np.ndarray) -> None:
        """
        Work out what the MilkDrop effect and palette and the made-up waveform need per LED.

        :param x: The LEDs' positions across the screen, 0..1.
        :param y: The LEDs' positions down the screen, 0..1.
        """
        # MilkDrop: where each LED reads the picture (-1..1), at a few depths straight in
        # from its edge, and the LEDs in order round the screen (for the MilkDrop palette).
        across, down = x * 2.0 - 1.0, y * 2.0 - 1.0
        self._picture_reads = [
            (
                np.where(self._side, across * depth, across * _PICTURE_DEPTHS[0]),
                np.where(self._side, down * _PICTURE_DEPTHS[0], down * depth),
            )
            for depth in _PICTURE_DEPTHS
        ]
        # The pixels and weights those reads blend, worked out once per picture size.
        self._picture_taps: tuple[tuple[int, ...], np.ndarray, np.ndarray] | None = None
        self._ring_order = np.argsort(self._ring, kind="stable")
        self._ring_sorted = self._ring[self._ring_order]
        self._lut_at = np.arange(_LUT_SIZE) / _LUT_SIZE
        self._picture: np.ndarray | None = None
        self._picture_at = -math.inf
        self._picture_palette_due = 0.0
        self._milkdrop_tint: tuple[int, int, int] | None = None
        # The waveform made from the spectrum: a sine at the middle of each band. Each
        # frame only shifts their phases, so the sines and cosines are worked out once.
        self._synth_freqs = np.geomspace(SPECTRUM_F_MIN, SPECTRUM_F_MAX, SPECTRUM_BINS)
        base = 2.0 * math.pi * np.outer(self._synth_freqs, np.arange(WAVE_SAMPLES) / _SYNTH_RATE)
        base += np.random.default_rng(5).random(SPECTRUM_BINS)[:, None] * 2.0 * math.pi
        self._synth_sin, self._synth_cos = np.sin(base), np.cos(base)
        # Where a waveform is resampled to the points of Liquid's ring.
        self._wave_points = np.linspace(0.0, WAVE_SAMPLES - 1, _LIQUID_RING, dtype=np.float32)

    # ------------------------------------------------------------------ inputs

    def update_settings(self, settings: EngineSettings) -> None:
        """
        Apply new settings without losing the audio state.

        :param settings: The new settings.
        """
        palette_changed = (
            settings.palette != self.settings.palette
            or (settings.palette == Palette.CUSTOM and settings.color != self.settings.color)
            or (
                settings.palette == Palette.ALBUM
                and settings.album_mode != self.settings.album_mode
            )
            or (
                settings.palette == Palette.MINE
                and (settings.colors, settings.steps) != (self.settings.colors, self.settings.steps)
            )
        )
        self.settings = settings
        if palette_changed:
            self._rebuild_palette()

    def set_milkdrop_tint(self, color: Sequence[int] | None) -> None:
        """
        Set the colour Music Assistant tints its MilkDrop visualizer with, or None for none.

        Music Assistant blends one colour of the cover over the picture (a colour blend: the
        picture keeps its brightness, the colour gives hue and saturation): ``on_light`` of
        the track's palette in its dark full-screen player.

        :param color: The colour (RGB 0-255), or None.
        """
        self._milkdrop_tint = (
            None if color is None else (int(color[0]), int(color[1]), int(color[2]))
        )

    @property
    def milkdrop_tint(self) -> tuple[int, int, int] | None:
        """The colour given to :meth:`set_milkdrop_tint`, if any."""
        return self._milkdrop_tint

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
        count = self.layout.count
        self._lumina = 0.0
        self._colora = 0.3
        self._retro = np.zeros(_RETRO_BANDS, np.float32)
        self._retro_peak = np.zeros(_RETRO_BANDS, np.float32)
        self._scan_pos = 0.0
        self._scan_dir = 1.0
        self._scan_flash = 0.0
        self._scan_trail = np.zeros((count, 3), np.float32)
        self._rhythm_step = 0
        self._party = np.zeros((count, 3), np.float32)
        self._reset_paraline_effects()
        # ``_time`` and ``_hue`` are deliberately kept so colours do not jump on a seek.
        if not hasattr(self, "_time"):
            self._time = 0.0
            self._hue = 0.0
            self._ambient_phase = 0.0

    def _reset_paraline_effects(self) -> None:
        """Reset the state of the effects inspired by Paraline (flow, fireflies, snowfall ...)."""
        count = self.layout.count
        self._flow_pos = 0.0
        self._flow_dir = 1.0
        self._centre_pulses: list[list[float]] = []
        flies = min(_MAX_PARTICLES // 2, max(6, count // 10))
        self._fly_pos = self._rng.random(flies).astype(np.float32)
        self._fly_speed = (
            (0.02 + 0.04 * self._rng.random(flies)) * self._rng.choice([-1, 1], flies)
        ).astype(np.float32)
        self._fly_color = self._rng.random(flies).astype(np.float32)
        self._fly_boost = 0.0
        # Particles as rows: side (0 left, 1 right; -1 both), height, speed, colour, life.
        self._flakes = np.zeros((0, 5), np.float32)
        self._sparks = np.zeros((0, 5), np.float32)
        self._spawn = 0.0
        self._shard_flare = np.zeros(len(self._shard_at), np.float32)
        self._ember = self._rng.random(count).astype(np.float32)
        self._braid_phase = 0.0
        self._aurora_phase = 0.0
        self._aurora_height = 0.3
        self._aurora_glow = 0.0
        self._horizon_phase = 0.0
        self._liquid = np.zeros((_LIQUID_H, _LIQUID_W, 3), np.float32)
        self._liquid_spin = 0.0
        self._liquid_hue = 0.0
        self._liquid_peak = 0.05
        self._wave: np.ndarray | None = None
        self._wave_at = 0.0

    def feed(
        self,
        *,
        loudness: int | None = None,
        spectrum: Sequence[int] | np.ndarray | None = None,
        onset: int | None = None,
        waveform: np.ndarray | None = None,
        picture: np.ndarray | None = None,
    ) -> None:
        """
        Feed the feature values that became audible.

        :param loudness: Visualizer ``loudness`` (0-65535, dB-linear).
        :param spectrum: Visualizer ``spectrum`` bins (0-65535 each, dB-linear),
            requested with the ``SPECTRUM_*`` constants of this module.
        :param onset: Visualizer ``peak`` onset strength (1-255).
        :param waveform: The sound wave sounding now (samples in -1..1), from Music
            Assistant's MilkDrop Visualizer plugin; Liquid draws it as its ring.
        :param picture: The latest MilkDrop picture, rows of ``R G B`` in 0..1, for the
            MilkDrop effect and palette.
        """
        if picture is not None:
            self._picture = np.asarray(picture, np.float32)
            self._picture_at = self._time
        if waveform is not None and len(waveform) >= 2:
            wave = np.asarray(waveform, np.float32)
            at = self._wave_points * ((wave.size - 1) / (WAVE_SAMPLES - 1))
            self._wave = np.interp(at, np.arange(wave.size), wave).astype(np.float32)
            self._wave_at = self._time
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
        effects: dict[Effect, Callable[[], np.ndarray]] = {
            Effect.SPECTRUM: self._fx_spectrum,
            Effect.VU: lambda: self._fx_vu(dt),
            Effect.PULSE: self._fx_pulse,
            Effect.RIPPLE: lambda: self._fx_ripple(dt),
            Effect.SCROLL: lambda: self._fx_scroll(dt),
            Effect.ENERGY: self._fx_energy,
            Effect.AMBIENT: lambda: self._fx_ambient(dt),
            Effect.LUMINA: lambda: self._fx_lumina(dt),
            Effect.COLORA: lambda: self._fx_colora(dt),
            Effect.RETRO: lambda: self._fx_retro(dt),
            Effect.SCANNER: lambda: self._fx_scanner(dt),
            Effect.RHYTHM: self._fx_rhythm,
            Effect.PARTY: lambda: self._fx_party(dt),
            Effect.MANUAL: lambda: self._solid(1.0),
            Effect.FLOW: lambda: self._fx_flow(dt),
            Effect.CENTRE_PULSE: lambda: self._fx_centre_pulse(dt),
            Effect.FIREFLIES: lambda: self._fx_fireflies(dt),
            Effect.SNOWFALL: lambda: self._fx_snowfall(dt),
            Effect.CRYSTALS: lambda: self._fx_crystals(dt),
            Effect.BRAIDS: lambda: self._fx_braids(dt),
            Effect.EMBERS: lambda: self._fx_embers(dt),
            Effect.AURORA: lambda: self._fx_aurora(dt),
            Effect.HORIZON: lambda: self._fx_horizon(dt),
            Effect.SIDE_BARS: self._fx_side_bars,
            Effect.LIQUID: lambda: self._fx_liquid(dt),
            Effect.MILKDROP: lambda: self._fx_milkdrop(dt),
        }
        if self.settings.palette == Palette.MILKDROP and self.settings.effect != Effect.MILKDROP:
            self._follow_picture_colors()
        frame = effects[self.settings.effect]()
        # Manual is a fixed light: it stays on through quiet passages.
        gate = 1.0 if self.settings.effect == Effect.MANUAL else self._gate
        frame = np.clip(frame, 0.0, 1.0) * (gate * min(1.0, max(0.0, self.settings.brightness)))
        smooth_tau = _RESPONSE_TAUS[self.settings.response][1]
        if smooth_tau > 0.0:
            self._out += (frame - self._out) * _alpha(dt, smooth_tau)
        else:
            self._out = frame.astype(np.float32)
        return (self._out * 255.0 + 0.5).astype(np.uint8).tobytes()

    def render_night_light(self, dt: float, level: float) -> bytes:
        """
        Render the night light: every LED in the custom colour, fading in from the music.

        :param dt: Seconds since the previous call.
        :param level: Night light brightness, 0..1 (the effect brightness does not apply).
        """
        dt = min(max(dt, 1e-4), 0.25)
        self._out += (self._solid(min(1.0, max(0.0, level))) - self._out) * _alpha(dt, 0.5)
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
        if self._has_spectrum:
            value = self._bands[self._band_lo] * (1.0 - self._band_frac)
            value += self._bands[self._band_hi] * self._band_frac
        else:
            # Loudness-only stream: the whole spectrum follows the overall level.
            value = np.full(self.layout.count, self._level, np.float32)
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
        """Flash every LED on each beat and go dark in between; the colour steps per beat."""
        value = 0.03 + 0.97 * self._pulse**1.5
        frame: np.ndarray = _sample_wrap(self._lut_cyclic, self._hue + self._ring * 0.05) * value
        return frame

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
        """Bass rises from the bottom, treble drops from the top-centre; they meet in between."""
        bass = np.clip((self._bass * 0.7 - self._pos) / self._soft + 0.5, 0.0, 1.0)
        treble = np.clip((self._treble * 0.7 - (1.0 - self._pos)) / self._soft + 0.5, 0.0, 1.0)
        frame: np.ndarray = (
            _sample_one(self._lut_linear, 0.0) * bass[:, None]
            + _sample_one(self._lut_linear, 1.0) * treble[:, None]
            + _sample_one(self._lut_linear, 0.5) * (0.12 * self._mid)
        )
        return frame

    def _fx_ambient(self, dt: float) -> np.ndarray:
        """
        Calm colours turning around the screen; the music sets their speed, not brightness.

        :param dt: Seconds since the previous frame.
        """
        self._ambient_phase = (self._ambient_phase + dt * (0.01 + 0.15 * self._level_slow)) % 1.0
        frame: np.ndarray = _sample_wrap(self._lut_cyclic, self._ring * 0.5 + self._ambient_phase)
        return frame * 0.55

    def _fx_lumina(self, dt: float) -> np.ndarray:
        """One calm colour on every LED; its brightness follows the loudness."""
        tau = 0.06 if self._level > self._lumina else 0.3
        self._lumina += (self._level - self._lumina) * _alpha(dt, tau)
        value = min(1.0, 0.04 + 0.96 * self._lumina**1.3 + 0.12 * self._pulse)
        color = _sample_wrap(self._lut_cyclic, self._time * 0.01 + self._ring * 0.06)
        frame: np.ndarray = color * value
        return frame

    def _fx_colora(self, dt: float) -> np.ndarray:
        """Steady brightness; the colour slides along the palette with the sound's pitch."""
        weights = self._bands**2
        total = float(weights.sum())
        if total > 1e-3:
            centroid = float((weights * np.linspace(0.0, 1.0, SPECTRUM_BINS)).sum()) / total
            target = min(1.0, max(0.0, (centroid - 0.12) / 0.55))
            self._colora += (target - self._colora) * _alpha(dt, 0.25)
        elif not self._has_spectrum:
            # Loudness-only stream: no pitch to follow, so the colour follows the loudness.
            self._colora += (self._level - self._colora) * _alpha(dt, 0.25)
        at = np.clip(self._colora + (self._ring - 0.5) * 0.12, 0.0, 1.0)
        # Steady brightness: only the colour moves (brightness is what Lumina follows).
        frame: np.ndarray = _sample(self._lut_linear, at) * 0.85
        return frame

    def _fx_retro(self, dt: float) -> np.ndarray:
        """
        Graphic equaliser: eight frequency bands, each a block that lights from its middle.

        The blocks run around the screen from bass to treble; each has a peak dot that
        falls back slowly, and dark gaps keep the blocks apart. Blocks too short for a bar
        show their band by brightness instead.

        :param dt: Seconds since the previous frame.
        """
        if self._has_spectrum:
            levels = np.maximum.reduceat(self._bands, _RETRO_BIN_STARTS)
        else:
            # Loudness-only stream: every block shows the overall level.
            levels = np.full(_RETRO_BANDS, self._level, np.float32)
        rising = levels > self._retro
        self._retro += (levels - self._retro) * np.where(
            rising, _alpha(dt, _ATTACK_TAU_S), _alpha(dt, 0.12)
        )
        self._retro_peak = np.maximum(self._retro, self._retro_peak - 0.5 * dt)
        block = self._retro_block
        colors = _sample(self._lut_linear, block / (_RETRO_BANDS - 1.0))
        if self._retro_leds < _RETRO_MIN_BAR_LEDS:
            short: np.ndarray = colors * self._retro[block][:, None]
            return short
        # Half an LED, measured in block offsets (a block's half spans ``_retro_leds / 2``).
        half_led = 1.0 / self._retro_leds
        reach = min(0.85, 1.0 - half_led)  # the outermost LED of every block stays dark
        offset = self._retro_offset
        lit = offset < self._retro[block] * reach
        frame: np.ndarray = colors * lit[:, None]
        # The peak dot is the LED nearest the peak, kept out of the gap: a window one LED
        # wide always holds exactly one LED per side, wherever the LEDs fall in a block.
        centre = np.minimum(self._retro_peak[block] * reach, reach - half_led)
        dot = (offset > centre - half_led) & (offset <= centre + half_led) & (offset < reach)
        frame[dot & (self._retro_peak[block] > 0.05)] = 0.85
        return frame

    def _fx_scanner(self, dt: float) -> np.ndarray:
        """Sweep a light back and forth with a fading trail; louder is faster, beats flash."""
        self._scan_pos += self._scan_dir * (0.3 + 1.4 * self._level) * dt
        if self._scan_pos > 1.0 or self._scan_pos < 0.0:
            self._scan_dir = -self._scan_dir
            self._scan_pos = min(1.0, max(0.0, self._scan_pos))
        if self._new_beat > 0.0:
            self._scan_flash = self._new_beat
        self._scan_flash *= math.exp(-dt / 0.2)
        self._scan_trail *= math.exp(-dt / 0.18)
        width = 0.03 + 0.05 * self._bass
        head = np.clip(1.0 - np.abs(self._ring - self._scan_pos) / width, 0.0, 1.0)
        color = _sample_one(self._lut_cyclic, self._hue)
        strength = min(1.0, 0.55 + 0.6 * self._level)
        self._scan_trail = np.maximum(self._scan_trail, head[:, None] * color * strength)
        frame: np.ndarray = self._scan_trail + color * (0.18 * self._scan_flash)
        return frame

    def _fx_rhythm(self) -> np.ndarray:
        """Six colour blocks; every beat swaps which half is lit and moves the colours on."""
        if self._new_beat > 0.0:
            self._rhythm_step += 1
        blocks = 6
        block = np.minimum((self._ring * blocks).astype(np.intp), blocks - 1)
        colors = _sample_wrap(self._lut_cyclic, block / blocks * 0.5 + self._rhythm_step * 0.13)
        lit = (block + self._rhythm_step) % 2 == 0
        value = np.where(lit, 0.3 + 0.7 * self._pulse, 0.04 + 0.12 * self._level)
        frame: np.ndarray = colors * value[:, None]
        return frame

    def _fx_party(self, dt: float) -> np.ndarray:
        """Random colour bursts on beats and sparkles on the treble, fading out."""
        count = self.layout.count
        self._party *= math.exp(-dt / 0.4)
        if self._new_beat > 0.0:
            length = max(1, count // 8)
            for _ in range(3):
                where = (int(self._rng.integers(count)) + np.arange(length)) % count
                color = _sample_one(self._lut_cyclic, float(self._rng.random()))
                self._party[where] = np.maximum(self._party[where], color * self._new_beat)
        sparkles = int(self._rng.poisson(self._treble * dt * count * 0.6))
        if sparkles:
            where = self._rng.integers(0, count, sparkles)
            self._party[where] = _sample_wrap(self._lut_cyclic, self._rng.random(sparkles))
        return self._party

    # Our own takes on effect ideas from Paraline (https://github.com/SamXop123/Paraline) by
    # SamXop123, made for a strip of LEDs: written from its descriptions, no code taken.

    def _fx_flow(self, dt: float) -> np.ndarray:
        """
        Three comets with fading tails circling the screen; louder is faster.

        :param dt: Seconds since the previous frame.
        """
        if self._new_beat > 0.85:
            self._flow_dir = -self._flow_dir
        self._flow_pos = (self._flow_pos + self._flow_dir * (0.05 + 0.45 * self._level) * dt) % 1.0
        tail = 0.08 + 0.1 * self._bass
        frame = np.zeros((self.layout.count, 3), np.float32)
        for comet in range(3):
            head = (self._flow_pos + comet / 3.0) % 1.0
            behind = ((head - self._ring) * self._flow_dir) % 1.0  # 0 at the head, growing behind
            shape = np.where(behind < 0.3, np.exp(-behind / tail), 0.0)
            color = _sample_one(self._lut_cyclic, (self._hue + comet / 3.0) % 1.0)
            frame += color * (shape * (0.35 + 0.65 * self._level))[:, None]
        return frame

    def _fx_centre_pulse(self, dt: float) -> np.ndarray:
        """
        On each beat a pulse leaves the middle of every edge and runs out to its corners.

        :param dt: Seconds since the previous frame.
        """
        if self._new_beat > 0.0:
            self._centre_pulses.append([-0.05, self._new_beat, self._hue])
            del self._centre_pulses[:-_MAX_RIPPLES]
        frame = _sample_wrap(self._lut_cyclic, self._hue + self._from_mid * 0.2) * (
            0.04 * self._level
        )
        fade = math.exp(-dt / 0.6)
        for pulse in self._centre_pulses:
            pulse[0] += 1.3 * dt
            pulse[1] *= fade
            shape = np.exp(-(((self._from_mid - pulse[0]) / 0.07) ** 2)) * pulse[1]
            frame = frame + _sample_one(self._lut_cyclic, pulse[2]) * shape[:, None]
        self._centre_pulses = [p for p in self._centre_pulses if p[0] < 1.2 and p[1] > 0.02]
        return frame

    def _fx_fireflies(self, dt: float) -> np.ndarray:
        """
        Small lights drifting around the screen; a beat speeds them up and may turn them.

        :param dt: Seconds since the previous frame.
        """
        if self._new_beat > 0.0:
            self._fly_boost = 1.0 + 3.0 * self._new_beat
            turn = self._rng.random(len(self._fly_speed)) < 0.35
            self._fly_speed[turn] = -self._fly_speed[turn]
        self._fly_boost = 1.0 + (self._fly_boost - 1.0) * math.exp(-dt / 0.35)
        speed = self._fly_speed * self._fly_boost * (0.5 + self._level)
        self._fly_pos = ((self._fly_pos + speed * dt) % 1.0).astype(np.float32)
        width = 1.4 / max(self.layout.count, 1)
        gap = np.abs((self._ring[:, None] - self._fly_pos[None, :] + 0.5) % 1.0 - 0.5)
        shape = np.exp(-((gap / width) ** 2))
        twinkle = 0.55 + 0.45 * np.sin(self._time * 3.0 + self._fly_color * 40.0)
        colors = (
            _sample_wrap(self._lut_cyclic, self._fly_color)
            * (twinkle * (0.4 + 0.6 * self._level))[:, None]
        )
        frame: np.ndarray = (shape @ colors).astype(np.float32)
        return frame

    def _fx_snowfall(self, dt: float) -> np.ndarray:
        """
        Glowing flakes fall down the left and right sides; the treble brings more of them.

        :param dt: Seconds since the previous frame.
        """
        self._spawn += dt * (0.8 + 6.0 * self._treble + 3.0 * self._pulse)
        new = int(self._spawn)
        if new:
            self._spawn -= new
            rows = np.column_stack(
                [
                    self._rng.integers(0, 2, new),
                    np.ones(new),
                    0.12 + 0.25 * self._rng.random(new),
                    self._rng.random(new),
                    np.ones(new),
                ]
            ).astype(np.float32)
            self._flakes = np.vstack([self._flakes, rows])[-_MAX_PARTICLES:]
        flakes = self._flakes
        flakes[:, 1] -= flakes[:, 2] * dt * (0.6 + 0.8 * self._level)
        self._flakes = flakes = flakes[flakes[:, 1] > -0.05]
        frame = np.zeros((self.layout.count, 3), np.float32)
        if len(flakes):
            on_side = self._edge[:, None] == flakes[None, :, 0].astype(np.intp)
            shape = np.exp(-(((self._height[:, None] - flakes[None, :, 1]) / 0.035) ** 2)) * on_side
            colors = _sample(self._lut_linear, 0.55 + 0.45 * flakes[:, 3]) * 0.9
            frame = (shape @ colors).astype(np.float32)
        # A faint glow along the top, where the flakes come from.
        top = (self._edge == 2).astype(np.float32) * (0.05 + 0.1 * self._treble)
        result: np.ndarray = frame + _sample_one(self._lut_linear, 1.0) * top[:, None]
        return result

    def _fx_crystals(self, dt: float) -> np.ndarray:
        """
        Flare fixed shards with their own frequency band and flutter them with the treble.

        :param dt: Seconds since the previous frame.
        """
        levels = (
            self._bands[self._shard_band]
            if self._has_spectrum
            else np.full(len(self._shard_at), self._level, np.float32)
        )
        rising = levels > self._shard_flare
        self._shard_flare += (levels - self._shard_flare) * np.where(
            rising, _alpha(dt, _ATTACK_TAU_S), _alpha(dt, 0.2)
        )
        flare = self._shard_flare**2
        flutter = 1.0 - self._treble * 0.6 * self._rng.random(len(flare))
        reach = (0.25 + 1.2 * flare) / len(self._shard_at) / 2.0  # half the shard's length
        gap = np.abs((self._ring[:, None] - self._shard_at[None, :] + 0.5) % 1.0 - 0.5)
        shape = np.clip(1.0 - gap / reach[None, :], 0.0, 1.0) ** 0.7
        glint = (flare**2 * 0.6)[:, None]  # the brightest shards turn towards white
        colors = _sample_wrap(self._lut_cyclic, self._shard_color) * (1.0 - glint) + glint
        colors = colors * (flare * flutter)[:, None]
        frame: np.ndarray = (shape @ colors).astype(np.float32)
        return frame

    def _fx_braids(self, dt: float) -> np.ndarray:
        """
        Two strands of colour wind along the sides in opposite directions, crossing as they go.

        :param dt: Seconds since the previous frame.
        """
        self._braid_phase += dt * (0.15 + 0.6 * self._level_slow)
        wave = 2.0 * math.pi * (self._pos * 2.5)
        up = (0.5 + 0.5 * np.sin(wave - 2.0 * math.pi * self._braid_phase)) ** 4
        down = (0.5 + 0.5 * np.sin(wave + 2.0 * math.pi * self._braid_phase + 1.3)) ** 4
        value = 0.3 + 0.55 * self._level + 0.15 * self._pulse
        frame: np.ndarray = (
            _sample_one(self._lut_linear, 0.15) * up[:, None]
            + _sample_one(self._lut_linear, 0.85) * down[:, None]
        ) * value
        return frame

    def _fx_embers(self, dt: float) -> np.ndarray:
        """
        Glow warm and flickering, with sparks rising from the bottom on the bass.

        :param dt: Seconds since the previous frame.
        """
        count = self.layout.count
        drift = self._rng.normal(0.0, 1.0, count).astype(np.float32) * math.sqrt(dt) * 0.9
        self._ember = np.clip(self._ember + drift, 0.0, 1.0)
        glow = (0.08 + 0.22 * self._level_slow) * (0.4 + 0.6 * self._ember) * (1.2 - self._pos)
        frame = _sample(self._lut_linear, 0.15 * self._ember)[:, :] * glow[:, None]
        self._spawn += dt * (0.5 + 10.0 * self._kick + 4.0 * self._bass**2)
        new = int(self._spawn)
        if new:
            self._spawn -= new
            rows = np.column_stack(
                [
                    np.full(new, -1.0),
                    self._rng.random(new) * 0.1,
                    0.25 + 0.35 * self._rng.random(new),
                    self._rng.random(new) * 0.4,
                    self._rng.random(new) * 2.0 - 1.0,  # which side and where along it
                ]
            ).astype(np.float32)
            self._sparks = np.vstack([self._sparks, rows])[-_MAX_PARTICLES:]
        sparks = self._sparks
        sparks[:, 1] += sparks[:, 2] * dt
        self._sparks = sparks = sparks[sparks[:, 1] < 0.85]
        if len(sparks):
            # Sparks rise along the left (negative) or right half of the ring.
            left = (self._ring < 0.5)[:, None] == (sparks[None, :, 4] < 0.0)
            shape = np.exp(-(((self._pos[:, None] - sparks[None, :, 1]) / 0.03) ** 2)) * left
            heat = 1.0 - sparks[:, 1] / 0.85
            colors = _sample(self._lut_linear, sparks[:, 3]) * heat[:, None]
            frame = frame + shape @ colors
        result: np.ndarray = frame.astype(np.float32)
        return result

    def _fx_aurora(self, dt: float) -> np.ndarray:
        """
        Raise folded curtains of colour from the bottom; louder music lifts and brightens them.

        :param dt: Seconds since the previous frame.
        """
        self._aurora_phase += dt * (0.04 + 0.08 * self._level_slow)
        target = 0.3 + 0.6 * self._level_slow
        self._aurora_height += (target - self._aurora_height) * _alpha(dt, 1.5)
        # The curtains swell with the loudness, smoothed so they never flash.
        self._aurora_glow += (self._level - self._aurora_glow) * _alpha(dt, 0.9)
        rise = np.clip(1.0 - self._pos / self._aurora_height, 0.0, 1.0) ** 1.3
        phase = 2.0 * math.pi * self._aurora_phase
        folds = 0.55 + 0.45 * np.sin(2.0 * math.pi * 3.0 * self._ring + phase) * np.sin(
            2.0 * math.pi * 5.0 * self._ring - phase * 0.7
        )
        at = self._ring * 0.35 + self._pos * 0.4 + self._aurora_phase * 0.5
        value = rise * folds * (0.25 + 0.75 * self._aurora_glow)
        frame: np.ndarray = _sample_wrap(self._lut_cyclic, at) * value[:, None]
        return frame

    def _fx_horizon(self, dt: float) -> np.ndarray:
        """
        Pulse a sun at top-centre with the bass; grid lines rush down the sides towards you.

        :param dt: Seconds since the previous frame.
        """
        self._horizon_phase = (self._horizon_phase + dt * (0.25 + 0.9 * self._level)) % 1.0
        sun_width = 0.18 + 0.1 * self._bass
        sun = (self._edge == 2) * np.clip(1.0 - self._from_mid / (sun_width * 2.0), 0.0, 1.0)
        sun_value = 0.35 + 0.65 * max(self._bass, self._pulse)
        # Grid lines: evenly spaced in depth, so they bunch up near the horizon at the top.
        lines = 6
        depth = (self._height**0.5) * lines + self._horizon_phase
        line = np.clip(1.0 - np.abs(depth - np.round(depth)) / 0.18, 0.0, 1.0)
        grid = line * self._side * (0.25 + 0.6 * self._level) * (0.4 + 0.6 * (1.0 - self._height))
        frame: np.ndarray = (
            _sample_one(self._lut_linear, 0.05) * (sun * sun_value)[:, None]
            + _sample_one(self._lut_linear, 0.8) * grid[:, None]
        )
        return frame

    def _fx_side_bars(self) -> np.ndarray:
        """Bars grow from the middle of each side: the bass on the left, the treble on the right."""
        reach = np.where(self._edge == 0, self._bass, self._treble) * 1.05
        lit = np.clip((reach - self._from_mid) / 0.08 + 0.5, 0.0, 1.0) * self._side
        colors = _sample(
            self._lut_linear, np.where(self._edge == 0, 0.0, 1.0) * 0.8 + self._from_mid * 0.2
        )
        top = (self._edge == 2) * (0.04 + 0.12 * self._mid)
        frame: np.ndarray = (
            colors * lit[:, None] + _sample_one(self._lut_linear, 0.5) * top[:, None]
        )
        return frame

    def _fx_liquid(self, dt: float) -> np.ndarray:
        """
        Swirl a small picture into itself, MilkDrop style, and show its edges on the LEDs.

        Every frame the picture is zoomed out from the middle, twisted, rippled and faded a
        little; then a ring is drawn into it: the real sound wave when Music Assistant's
        MilkDrop Visualizer plugin provides it, else a ring shaped by the spectrum. So what the music drew
        a moment ago spreads out to the screen's edges as glowing trails. Beats kick the
        zoom and turn the twist round.

        :param dt: Seconds since the previous frame.
        """
        frames = dt * 60.0  # the motion is tuned per frame at 60 frames a second
        if self._new_beat > 0.0:
            turn = -1.0 if self._liquid_spin > 0.0 else 1.0
            self._liquid_spin = turn * (0.4 + 0.6 * self._new_beat)
        self._liquid_spin *= 0.985**frames
        # The colours travel round the ring, faster when louder.
        self._liquid_hue = (self._liquid_hue + dt * (0.03 + 0.12 * self._level_slow)) % 1.0
        zoom = 1.0 + (0.012 + 0.03 * self._level + 0.06 * self._pulse) * frames
        angle = (0.004 + 0.02 * self._liquid_spin + 0.006 * math.sin(self._time * 0.31)) * frames
        # Where each pixel takes its colour from: nearer the middle, turned and rippled.
        u, v = self._liquid_grid
        cos, sin = math.cos(angle), math.sin(angle)
        ripple = 0.012 * (0.4 + self._mid) * frames
        src_u = (u * cos - v * sin) / zoom + ripple * np.sin(v * 7.0 + self._time * 1.3)
        src_v = (u * sin + v * cos) / zoom + ripple * np.cos(u * 6.0 - self._time * 1.1)
        picture = _bilinear(self._liquid, src_u, src_v) * (0.955**frames)
        # The ring: its radius at each angle follows one band of the spectrum, mirrored so
        # the bass sits at the sides and the treble at the top and bottom.
        theta = np.linspace(0.0, 2.0 * math.pi, _LIQUID_RING, endpoint=False, dtype=np.float32)
        if self._wave is not None and self._time - self._wave_at < _WAVE_FRESH_S:
            # The real sound wave, bent round into a ring as MilkDrop draws it: mirrored
            # so its two ends meet, and scaled to its recent peak so quiet tracks show too.
            wave = 0.5 * (self._wave + self._wave[::-1])
            peak = float(np.abs(wave).max())
            self._liquid_peak = max(peak, self._liquid_peak * 0.97**frames, 0.05)
            shape = wave / self._liquid_peak
            radius = 0.24 + 0.16 * shape + 0.08 * self._pulse
            shade = (0.55 + 0.45 * np.abs(shape)) * self._level
        else:
            fold = np.abs(np.cos(theta)) * (SPECTRUM_BINS - 1)
            bands = np.interp(fold, np.arange(SPECTRUM_BINS), self._bands).astype(np.float32)
            radius = 0.12 + 0.3 * bands + 0.08 * self._pulse
            shade = (0.35 + 0.65 * bands) * self._level
        colors = _sample_wrap(self._lut_cyclic, theta / (2.0 * math.pi) + self._liquid_hue)
        _splat(picture, radius * np.cos(theta), radius * np.sin(theta), colors * shade[:, None])
        self._liquid = np.minimum(picture, 1.5).astype(np.float32)
        frame: np.ndarray = _bilinear(self._liquid, *self._liquid_at)
        return frame

    def has_fresh_picture(self) -> bool:
        """Whether a MilkDrop picture arrived recently enough to show."""
        return self._picture is not None and self._time - self._picture_at < _PICTURE_FRESH_S

    def synth_waveform(self) -> bytes:
        """
        Return a waveform made from the spectrum, for MilkDrop when the real one is missing.

        A sine at the middle of each band, as loud as the band, plus the beat on the bass:
        enough for the presets to tell bass, mids and treble apart and to see the beats.

        :return: 1024 unsigned bytes, 128 being zero, as Butterchurn takes them.
        """
        amplitude = self._bands.astype(np.float64) ** 1.5
        amplitude[:3] *= 1.0 + 1.5 * self._pulse
        # sin(base + shift) = sin(base) cos(shift) + cos(base) sin(shift)
        shift = 2.0 * math.pi * self._synth_freqs * self._time
        wave = (amplitude * np.cos(shift)) @ self._synth_sin + (
            amplitude * np.sin(shift)
        ) @ self._synth_cos
        peak = float(np.abs(wave).max())
        if peak < 1e-6 or self._level < 0.01:
            return wave_to_bytes(np.zeros(WAVE_SAMPLES))
        return wave_to_bytes(wave * ((0.2 + 0.7 * self._level) / peak))

    def _read_picture(self) -> np.ndarray:
        """
        Each LED's colour from the picture, averaged over a little depth from its edge.

        With the MilkDrop palette the picture is tinted as Music Assistant shows it.
        """
        assert self._picture is not None
        shape = self._picture.shape
        if self._picture_taps is None or self._picture_taps[0] != shape:
            taps = [_bilinear_taps(shape[0], shape[1], u, v) for u, v in self._picture_reads]
            index = np.concatenate([t[0] for t in taps], axis=1)
            weight = np.concatenate([t[1] for t in taps], axis=1) / len(taps)
            self._picture_taps = (shape, index, weight)
        _, index, weight = self._picture_taps
        pixels = self._picture.reshape(-1, 3)[index]
        colors: np.ndarray = np.einsum("nk,nkc->nc", weight, pixels).astype(np.float32)
        if self.settings.palette == Palette.MILKDROP and self._milkdrop_tint is not None:
            tint = np.array(self._milkdrop_tint, np.float32) / 255.0
            colors = _blend_color(colors, np.broadcast_to(tint, colors.shape))
        return colors

    def _fx_milkdrop(self, dt: float) -> np.ndarray:
        """
        Show the MilkDrop picture at the LEDs, tinted as the palette says.

        The MilkDrop palette tints it as Music Assistant does, with the cover's colour; any
        other palette tints it with its own colours round the screen. Either way it is a
        colour blend: each LED keeps the picture's brightness, the tint gives the colour.

        :param dt: Seconds since the previous frame.
        """
        if not self.has_fresh_picture():
            return self._fx_liquid(dt)
        colors = self._read_picture()
        if self.settings.palette != Palette.MILKDROP:
            colors = _blend_color(colors, _sample_wrap(self._lut_cyclic, self._ring))
        return colors

    def _follow_picture_colors(self) -> None:
        """Turn the colours round the edges of the live picture into the palette."""
        if not self.has_fresh_picture() or self._time < self._picture_palette_due:
            return
        self._picture_palette_due = self._time + _PICTURE_PALETTE_S
        colors = self._read_picture()[self._ring_order]
        # Full brightness: the effect sets how bright, the palette only which colour.
        peak = colors.max(axis=1, keepdims=True)
        lit = peak[:, 0] > 0.04
        if lit.sum() < 2:
            return
        colors = colors[lit] / peak[lit]
        ring = self._ring_sorted[lit]
        table = np.stack(
            [np.interp(self._lut_at, ring, colors[:, c], period=1.0) for c in range(3)], axis=1
        ).astype(np.float32)
        follow = _alpha(_PICTURE_PALETTE_S, _PICTURE_PALETTE_TAU)
        self._lut_cyclic += (table - self._lut_cyclic) * follow
        self._lut_linear = self._lut_cyclic

    def _solid(self, level: float) -> np.ndarray:
        """Every LED in the custom colour at ``level``."""
        color = np.array(self.settings.color, np.float32) / 255.0 * level
        return np.tile(color, (self.layout.count, 1))

    # ---------------------------------------------------------------- palettes

    @property
    def album_colors(self) -> tuple[tuple[int, int, int], ...]:
        """The artwork colours last given to :meth:`set_album_colors`."""
        return self._album_colors

    def _rebuild_palette(self) -> None:
        """Rebuild both colour lookup tables for the selected palette."""
        self._lut_linear, self._lut_cyclic = _palette_luts(
            self.settings.palette,
            self._album_colors,
            self.settings.color,
            self.settings.album_mode,
            self.settings.colors,
            steps=self.settings.steps,
        )


def palette_swatch(  # noqa: PLR0913 - one optional input per kind of palette
    palette: Palette,
    album_colors: Iterable[Sequence[int]] = (),
    count: int = 8,
    color: tuple[int, int, int] = DEFAULT_COLOR,
    album_mode: AlbumMode = AlbumMode.VIBRANT,
    *,
    colors: Sequence[Sequence[int]] = (),
) -> list[tuple[int, int, int]]:
    """
    Return colours spread across a palette's gradient, e.g. to show it in a user interface.

    :param palette: The palette.
    :param album_colors: Artwork colours, used by :attr:`Palette.ALBUM`.
    :param count: How many colours to return.
    :param color: The custom colour, used by :attr:`Palette.CUSTOM`.
    :param album_mode: How :attr:`Palette.ALBUM` picks colours from the cover.
    :param colors: The user's own colours, used by :attr:`Palette.MINE`.
    """
    cleaned = tuple((int(c[0]), int(c[1]), int(c[2])) for c in album_colors if len(c) >= 3)
    mine = tuple((int(c[0]), int(c[1]), int(c[2])) for c in colors if len(c) >= 3)
    linear, _ = _palette_luts(palette, cleaned, color, album_mode, mine)
    at = np.linspace(0.0, 1.0, max(count, 2))
    return [
        (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))
        for r, g, b in _sample(linear, at).tolist()
    ]


# ------------------------------------------------------------------- helpers


def _palette_luts(  # noqa: PLR0913 - one input per kind of palette
    palette: Palette,
    album_colors: tuple[_RGB, ...],
    color: _RGB = DEFAULT_COLOR,
    album_mode: AlbumMode = AlbumMode.VIBRANT,
    mine: tuple[_RGB, ...] = (),
    *,
    steps: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the (linear, cyclic) colour lookup tables of a palette."""
    stops: tuple[_RGB, ...] | None = None
    if palette == Palette.MINE and mine:
        if steps:
            # Blocks: every colour takes an equal share of the table, without blending.
            blocks = np.array(mine, np.float32) / 255.0
            index = np.minimum(np.arange(_LUT_SIZE) * len(mine) // _LUT_SIZE, len(mine) - 1)
            table: np.ndarray = blocks[index].astype(np.float32)
            return table, table.copy()
        stops = mine
    elif palette == Palette.CUSTOM:
        stops = _custom_stops(color)
    elif palette == Palette.ALBUM:
        # Black-and-white artwork has no colour to show: light it in shades of white
        # rather than the rainbow, whose bass end would turn the LEDs red.
        stops = _album_stops(album_colors, album_mode)
    elif palette in _PALETTE_STOPS:
        stops = _PALETTE_STOPS[palette]
    if stops:
        colors = np.array(stops, np.float32) / 255.0
        # Natural artwork colours blend directly, so no hues appear that are not on the cover.
        wheel = not (palette == Palette.ALBUM and album_mode == AlbumMode.NATURAL)
        return (
            _gradient(colors, cyclic=False, wheel=wheel),
            _gradient(colors, cyclic=True, wheel=wheel),
        )
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


def _gradient(colors: np.ndarray, *, cyclic: bool, wheel: bool = True) -> np.ndarray:
    """
    Build a lookup table blending through ``colors`` (optionally back to the first).

    Colours blend around the colour wheel (hue, saturation and brightness), not channel
    by channel: mixing e.g. red and teal channel-wise passes through a dull grey, while
    going round the wheel stays vivid. Whites and greys take the hue of their neighbour.
    With ``wheel=False`` the colours blend channel by channel.
    """
    stops = np.vstack([colors, colors[:1]]) if cyclic else colors
    if len(stops) == 1:
        return np.repeat(stops, _LUT_SIZE, axis=0).astype(np.float32)
    if not wheel:
        span = len(stops) - 1
        at = np.arange(_LUT_SIZE) / (_LUT_SIZE if cyclic else _LUT_SIZE - 1) * span
        low = np.minimum(at.astype(np.intp), span - 1)
        mix = (at - low)[:, None]
        direct: np.ndarray = (stops[low] * (1.0 - mix) + stops[low + 1] * mix).astype(np.float32)
        return direct
    hsv = np.array([colorsys.rgb_to_hsv(*map(float, c)) for c in stops])
    coloured = np.flatnonzero(hsv[:, 1] >= 0.05)
    for i in np.flatnonzero(hsv[:, 1] < 0.05):
        if coloured.size:
            hsv[i, 0] = hsv[coloured[np.argmin(np.abs(coloured - i))], 0]
    span = len(stops) - 1
    at = np.arange(_LUT_SIZE) / (_LUT_SIZE if cyclic else _LUT_SIZE - 1) * span
    low = np.minimum(at.astype(np.intp), span - 1)
    frac = at - low
    start, end = hsv[low], hsv[low + 1]
    turn = (end[:, 0] - start[:, 0] + 0.5) % 1.0 - 0.5  # the shorter way round
    hue = (start[:, 0] + turn * frac) % 1.0
    sat = start[:, 1] + (end[:, 1] - start[:, 1]) * frac
    val = start[:, 2] + (end[:, 2] - start[:, 2]) * frac
    return np.array(
        [colorsys.hsv_to_rgb(h, s, v) for h, s, v in zip(hue, sat, val, strict=True)],
        np.float32,
    )


def _sample(lut: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Look up colours at positions ``at``, clamped to 0..1 (for bars and gradients)."""
    size = len(lut)
    return lut[np.clip((at * size).astype(np.intp), 0, size - 1)]


def _sample_wrap(lut: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Look up colours at positions ``at``, wrapping around (for cyclic colour wheels)."""
    size = len(lut)
    colors: np.ndarray = lut[np.minimum((np.mod(at, 1.0) * size).astype(np.intp), size - 1)]
    return colors


def _bilinear(picture: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """
    Read a picture between its pixels, blending the four nearest; black outside it.

    :param picture: Rows of ``R G B`` pixels, spanning -1..1 both ways.
    :param u: Across, -1 (left) to 1 (right).
    :param v: Down, -1 (top) to 1 (bottom).
    """
    height, width = picture.shape[:2]
    x = (u + 1.0) * 0.5 * (width - 1)
    y = (v + 1.0) * 0.5 * (height - 1)
    inside = (x >= 0) & (x <= width - 1) & (y >= 0) & (y <= height - 1)
    x = np.clip(x, 0, width - 1)
    y = np.clip(y, 0, height - 1)
    x0 = np.minimum(x.astype(np.intp), width - 2)
    y0 = np.minimum(y.astype(np.intp), height - 2)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    top = picture[y0, x0] * (1 - fx) + picture[y0, x0 + 1] * fx
    bottom = picture[y0 + 1, x0] * (1 - fx) + picture[y0 + 1, x0 + 1] * fx
    result: np.ndarray = (top * (1 - fy) + bottom * fy) * inside[..., None]
    return result


def wave_to_bytes(wave: np.ndarray) -> bytes:
    """
    Encode a waveform as MilkDrop takes it: unsigned bytes, 128 being zero.

    :param wave: Samples in -1..1 (clipped).
    """
    return bytes(np.rint(np.clip(wave, -1.0, 1.0) * 127.0 + 128.0).astype(np.uint8))


def bytes_to_wave(data: bytes) -> np.ndarray:
    """
    Decode a waveform from unsigned bytes (128 being zero) to samples in -1..1.

    :param data: The encoded waveform.
    """
    wave: np.ndarray = (np.frombuffer(data, np.uint8).astype(np.float32) - 128.0) / 127.0
    return wave


def _bilinear_taps(
    height: int, width: int, u: np.ndarray, v: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """
    Return what :func:`_bilinear` blends at fixed points: four pixel indices and weights each.

    :param height: Rows of the picture.
    :param width: Columns of the picture.
    :param u: Across, -1 (left) to 1 (right).
    :param v: Down, -1 (top) to 1 (bottom).
    :return: Indices into the flattened picture and their weights, shaped (points, 4); a
        point outside the picture has zero weights.
    """
    x = (u + 1.0) * 0.5 * (width - 1)
    y = (v + 1.0) * 0.5 * (height - 1)
    inside = ((x >= 0) & (x <= width - 1) & (y >= 0) & (y <= height - 1)).astype(np.float32)
    x = np.clip(x, 0, width - 1)
    y = np.clip(y, 0, height - 1)
    x0 = np.minimum(x.astype(np.intp), width - 2)
    y0 = np.minimum(y.astype(np.intp), height - 2)
    fx, fy = x - x0, y - y0
    index = np.stack(
        [y0 * width + x0, y0 * width + x0 + 1, (y0 + 1) * width + x0, (y0 + 1) * width + x0 + 1],
        axis=1,
    )
    weight = (
        np.stack([(1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy], axis=1)
        * inside[:, None]
    )
    return index, weight.astype(np.float32)


def _luminosity(colors: np.ndarray) -> np.ndarray:
    """Luminosity of colours, as the CSS colour blend modes weigh it."""
    lum: np.ndarray = colors @ np.array([0.3, 0.59, 0.11], np.float32)
    return lum


def _blend_color(base: np.ndarray, tint: np.ndarray) -> np.ndarray:
    """
    CSS ``mix-blend-mode: color``: the hue and saturation of ``tint``, the brightness of ``base``.

    :param base: Colours to keep the luminosity of, rows of ``R G B`` in 0..1.
    :param tint: Colours to take hue and saturation from, the same shape.
    """
    lum = _luminosity(base)[:, None]
    colors = tint + (lum - _luminosity(tint)[:, None])
    # Clip into range while keeping the luminosity (the CSS ClipColor step).
    low = colors.min(axis=1, keepdims=True)
    high = colors.max(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        colors = np.where(low < 0.0, lum + (colors - lum) * lum / (lum - low), colors)
        high = colors.max(axis=1, keepdims=True)
        colors = np.where(high > 1.0, lum + (colors - lum) * (1.0 - lum) / (high - lum), colors)
    result: np.ndarray = np.clip(np.nan_to_num(colors), 0.0, 1.0).astype(np.float32)
    return result


def _splat(picture: np.ndarray, u: np.ndarray, v: np.ndarray, colors: np.ndarray) -> None:
    """Add each colour to the picture's pixel nearest its point (axes as in ``_bilinear``)."""
    height, width = picture.shape[:2]
    x = np.clip(np.rint((u + 1.0) * 0.5 * (width - 1)).astype(np.intp), 0, width - 1)
    y = np.clip(np.rint((v + 1.0) * 0.5 * (height - 1)).astype(np.intp), 0, height - 1)
    np.add.at(picture, (y, x), colors)


def _sample_one(lut: np.ndarray, at: float) -> np.ndarray:
    """Look up one colour; 1.0 maps to the last entry instead of wrapping."""
    size = len(lut)
    color: np.ndarray = lut[size - 1] if at >= 1.0 else lut[int((at % 1.0) * size)]
    return color


def _custom_stops(color: _RGB) -> tuple[_RGB, ...]:
    """Palette stops around the custom colour: its neighbouring hues, at full brightness."""
    hue, sat, _ = colorsys.rgb_to_hsv(color[0] / 255.0, color[1] / 255.0, color[2] / 255.0)
    if sat < 0.15:
        # A white or grey: shades of it would all look the same, so keep it plain.
        return (color,)
    shades = []
    for shift in (-0.05, 0.0, 0.05):
        r, g, b = colorsys.hsv_to_rgb((hue + shift) % 1.0, sat, 1.0)
        shades.append((round(r * 255), round(g * 255), round(b * 255)))
    return tuple(shades)


def _album_stops(colors: tuple[_RGB, ...], mode: AlbumMode) -> tuple[_RGB, ...] | None:
    """
    Make the Album art palette from the cover colours (main colour first, then the accent).

    Black-and-white artwork has no colour to show, so it is lit in shades of white rather
    than the rainbow, whose bass end would turn the LEDs red. Without any cover colours
    there are no stops and the rainbow is used.
    """
    if not colors:
        return None
    stops: tuple[_RGB, ...] | None
    if mode == AlbumMode.DOMINANT:
        stops = _vivid_stops(colors[:1])
    elif mode == AlbumMode.ACCENT:
        stops = _vivid_stops(colors[1:2] + colors[:1])
    elif mode == AlbumMode.NATURAL:
        stops = _natural_stops(colors)
    elif mode == AlbumMode.PASTEL:
        vivid = _vivid_stops(colors)
        stops = tuple(_mix(c, (255, 255, 255), 0.45) for c in vivid) if vivid else None
    elif mode == AlbumMode.CONTRAST:
        vivid = _vivid_stops(colors)
        stops = (vivid[0], _opposite(vivid[0])) if vivid else None
    else:
        stops = _vivid_stops(colors)
    return stops or _MONOCHROME_STOPS


def _natural_stops(colors: tuple[_RGB, ...]) -> tuple[_RGB, ...] | None:
    """Cover colours as they are, with dark ones lifted so the LEDs still glow."""
    stops: list[_RGB] = []
    for color in colors:
        brightest = max(color)
        if brightest < 20:
            continue
        scale = max(1.0, 140.0 / brightest)
        lifted = (
            min(255, round(color[0] * scale)),
            min(255, round(color[1] * scale)),
            min(255, round(color[2] * scale)),
        )
        if all(sum(abs(a - b) for a, b in zip(lifted, s, strict=True)) > 40 for s in stops):
            stops.append(lifted)
    return tuple(stops) or None


def _mix(color: _RGB, other: _RGB, amount: float) -> _RGB:
    """Blend ``amount`` (0..1) of ``other`` into ``color``."""
    r, g, b = (round(c * (1.0 - amount) + o * amount) for c, o in zip(color, other, strict=True))
    return r, g, b


def _opposite(color: _RGB) -> _RGB:
    """Return the colour on the other side of the colour wheel, at full brightness."""
    hue, sat, _ = colorsys.rgb_to_hsv(color[0] / 255.0, color[1] / 255.0, color[2] / 255.0)
    r, g, b = colorsys.hsv_to_rgb((hue + 0.5) % 1.0, max(sat, 0.6), 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


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
