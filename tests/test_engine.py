"""Tests of the rendering engine and the LED layout; they need no server."""

import numpy as np
import pytest

from hyperhdr_sync.engine import (
    SPECTRUM_BINS,
    Effect,
    Engine,
    EngineSettings,
    Palette,
    Response,
    hex_color,
    palette_swatch,
    parse_hex_color,
)
from hyperhdr_sync.layout import LedLayout

DT = 1 / 40
LOUD = 50000
QUIET = 30000


def _tv_layout() -> LedLayout:
    """Return a TV layout without bottom LEDs: up the left, across the top, down the right."""
    left = [(0.02, y) for y in np.linspace(0.9, 0.1, 14)]
    top = [(x, 0.03) for x in np.linspace(0.07, 0.93, 26)]
    right = [(0.98, y) for y in np.linspace(0.1, 0.9, 14)]
    points = np.array(left + top + right, np.float32)
    return LedLayout.from_centres(points[:, 0], points[:, 1])


def _frame(engine: Engine, spectrum: list[int] | None, loudness: int | None = LOUD) -> np.ndarray:
    """Feed one set of features, render, and return the frame as an (LEDs, 3) array."""
    engine.feed(loudness=loudness, spectrum=spectrum)
    return np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3)


def _settle(
    engine: Engine, spectrum: list[int] | None, loudness: int | None = LOUD, *, frames: int
) -> np.ndarray:
    """Feed the same features for a number of frames and return the last rendered frame."""
    frame = _frame(engine, spectrum, loudness)
    for _ in range(frames - 1):
        frame = _frame(engine, spectrum, loudness)
    return frame


def _play(engine: Engine, seconds: float = 1.5, *, beat_every: int = 20) -> np.ndarray:
    """Feed a steady bass-heavy sound with regular onsets; return the brightest value per frame."""
    spectrum = [LOUD] * 4 + [QUIET] * (SPECTRUM_BINS - 4)
    peaks = []
    for step in range(int(seconds / DT)):
        if step % beat_every == 0:
            engine.feed(onset=200)
        peaks.append(_frame(engine, spectrum).max())
    return np.array(peaks)


def test_layout_orders_a_tv_strip_around_the_screen() -> None:
    """The ring position rises along the strip and the mirror position is left/right symmetric."""
    layout = _tv_layout()
    assert layout.count == 54
    assert np.all(np.diff(layout.ring) > 0)
    assert layout.mirror[0] == pytest.approx(layout.mirror[-1], abs=1e-3)
    assert layout.mirror.argmax() in (26, 27)


def test_layout_survives_storing() -> None:
    """A layout stored in the config comes back with the same geometry."""
    layout = _tv_layout()
    restored = LedLayout.from_list(layout.to_list())
    assert restored.count == layout.count
    assert np.allclose(restored.ring, layout.ring, atol=1e-3)


def test_layout_rejects_nothing_to_drive() -> None:
    """An empty LED list is refused instead of producing an engine without LEDs."""
    with pytest.raises(ValueError, match="no LEDs"):
        LedLayout.from_serverinfo([])
    with pytest.raises(ValueError, match="incomplete"):
        LedLayout.from_list([0.1, 0.2, 0.3])


def test_fallback_strip_layout() -> None:
    """The fallback layout spreads LEDs evenly around the whole perimeter."""
    layout = LedLayout.strip(60)
    assert layout.count == 60
    assert np.all(np.diff(layout.ring) > 0)
    assert layout.x.min() >= 0.0
    assert layout.x.max() <= 1.0


@pytest.mark.parametrize("effect", [effect for effect in Effect if effect != Effect.MANUAL])
@pytest.mark.parametrize("palette", [Palette.RAINBOW, Palette.FIRE, Palette.ALBUM, Palette.CUSTOM])
def test_every_effect_lights_up_with_music_and_goes_dark_in_silence(
    effect: Effect, palette: Palette
) -> None:
    """Music lights the LEDs; after the music stops they fade to black."""
    layout = _tv_layout()
    engine = Engine(layout, EngineSettings(effect=effect, palette=palette))
    engine.set_album_colors([(200, 30, 30), (20, 40, 180)])
    peaks = _play(engine)
    assert len(engine.render(DT)) == layout.count * 3
    assert peaks[20:].max() > 60, "music should light the LEDs clearly"

    for _ in range(int(4 / DT)):
        engine.feed(loudness=0, spectrum=[0] * SPECTRUM_BINS)
        engine.render(DT)
    assert engine.is_dark


def test_spectrum_puts_bass_at_the_bottom_and_treble_at_the_top() -> None:
    """With only bass playing, the bottom LEDs of both sides are lit and the top is dark."""
    layout = _tv_layout()
    engine = Engine(layout, EngineSettings(effect=Effect.SPECTRUM, palette=Palette.RAINBOW))
    bass_only = [LOUD] * 3 + [0] * (SPECTRUM_BINS - 3)
    frame = _settle(engine, bass_only, frames=40)
    brightness = frame.max(axis=1)
    assert brightness[0] > 100
    assert brightness[-1] > 100
    assert brightness[27] < 10

    treble_only = [0] * (SPECTRUM_BINS - 3) + [LOUD] * 3
    frame = _settle(engine, treble_only, frames=80)
    brightness = frame.max(axis=1)
    assert brightness[27] > 100
    assert brightness[0] < 10


def test_automatic_gain_makes_quiet_and_loud_tracks_look_alike() -> None:
    """A track mastered 12 dB quieter ends up about as bright as a loud one."""

    def settle(level: int) -> float:
        engine = Engine(_tv_layout(), EngineSettings(effect=Effect.SPECTRUM))
        frame = _settle(engine, [level] * SPECTRUM_BINS, loudness=level, frames=120)
        return float(frame.mean())

    loud, quiet = settle(52000), settle(52000 - 13107)  # 13107 steps are 12 dB
    assert quiet == pytest.approx(loud, rel=0.15)


def test_sensitivity_and_brightness_settings() -> None:
    """Higher sensitivity brightens a soft passage; brightness scales the output."""

    def soft_passage(settings: EngineSettings) -> float:
        engine = Engine(_tv_layout(), settings)
        for _ in range(40):
            _frame(engine, [LOUD] * SPECTRUM_BINS)
        frame = _settle(engine, [LOUD - 15000] * SPECTRUM_BINS, frames=40)
        return float(frame.mean())

    normal = soft_passage(EngineSettings(effect=Effect.SPECTRUM, palette=Palette.RAINBOW))
    boosted = soft_passage(
        EngineSettings(effect=Effect.SPECTRUM, palette=Palette.RAINBOW, sensitivity_db=9)
    )
    dimmed = soft_passage(
        EngineSettings(effect=Effect.SPECTRUM, palette=Palette.RAINBOW, brightness=0.5)
    )
    assert boosted > normal * 1.3
    assert dimmed == pytest.approx(normal / 2, rel=0.1)


def test_beats_flash_the_pulse_effect() -> None:
    """An onset makes the pulse effect brighter than the steady sound before it."""
    engine = Engine(
        _tv_layout(),
        EngineSettings(effect=Effect.PULSE, palette=Palette.RAINBOW, response=Response.SNAPPY),
    )
    steady = [QUIET] * SPECTRUM_BINS
    before = _settle(engine, steady, loudness=QUIET, frames=80).max()
    engine.feed(onset=255)
    after = _frame(engine, steady, loudness=QUIET).max()
    assert after > before * 1.4


def test_loudness_only_stream_still_drives_the_lights() -> None:
    """Without spectrum data the effects fall back to the overall loudness."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.ENERGY, palette=Palette.OCEAN))
    frame = _settle(engine, None, loudness=LOUD, frames=60)
    assert frame.max() > 60


def test_album_colours_are_used_and_fall_back_to_rainbow() -> None:
    """Artwork colours tint the LEDs; without artwork the rainbow is used."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.PULSE, palette=Palette.ALBUM))
    engine.set_album_colors([(180, 10, 10)])
    frame = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=40)
    red, green, blue = frame.astype(int).sum(axis=0)
    assert red > 2 * blue
    assert red > green

    none = Engine(_tv_layout(), EngineSettings(effect=Effect.SPECTRUM, palette=Palette.ALBUM))
    frame = _settle(none, [LOUD] * SPECTRUM_BINS, frames=40)
    channels = frame.astype(int).sum(axis=0)
    assert channels.min() > 0, "rainbow fallback uses all three channels"


def test_black_and_white_artwork_gives_white_light() -> None:
    """Greyscale artwork lights the LEDs in shades of white, never in rainbow red."""
    bass = [LOUD] * 4 + [0] * (SPECTRUM_BINS - 4)
    for effect in (Effect.SPECTRUM, Effect.PULSE, Effect.ENERGY):
        engine = Engine(_tv_layout(), EngineSettings(effect=effect, palette=Palette.ALBUM))
        engine.set_album_colors([(240, 240, 240), (90, 90, 90), (10, 10, 10)])
        frame = _settle(engine, bass, frames=40).astype(int)
        lit = frame[frame.max(axis=1) > 40]
        assert len(lit), effect
        assert (lit.min(axis=1) >= 0.3 * lit.max(axis=1)).all(), f"{effect} shows a strong tint"


def test_changing_settings_keeps_the_music_state() -> None:
    """Switching effect while playing does not restart the gain or blank the LEDs."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.SPECTRUM, palette=Palette.NEON))
    _play(engine, 1.0)
    engine.update_settings(EngineSettings(effect=Effect.VU, palette=Palette.SUNSET))
    assert _frame(engine, [LOUD] * SPECTRUM_BINS).max() > 60


def test_odd_input_does_not_break_rendering() -> None:
    """Too few or too many spectrum bins and extreme time steps are tolerated."""
    engine = Engine(LedLayout.strip(7), EngineSettings(effect=Effect.SCROLL))
    engine.feed(spectrum=[LOUD] * 5, loudness=70000)
    assert len(engine.render(5.0)) == 21
    engine.feed(spectrum=np.full(40, LOUD, np.uint16), onset=0)
    assert len(engine.render(0.0)) == 21
    single = Engine(LedLayout.strip(1), EngineSettings(effect=Effect.RIPPLE))
    single.feed(spectrum=[LOUD] * SPECTRUM_BINS, onset=200)
    assert len(single.render(DT)) == 3


def test_manual_shows_the_custom_colour_without_music() -> None:
    """Manual lights every LED in the custom colour at the set brightness, music or not."""
    engine = Engine(
        _tv_layout(),
        EngineSettings(effect=Effect.MANUAL, color=(255, 100, 0), brightness=0.5),
    )
    frame = np.zeros((1, 3), np.uint8)
    for _ in range(int(2 / DT)):
        frame = np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3)
    assert (np.abs(frame.astype(int) - [128, 50, 0]) <= 1).all()


def test_custom_palette_follows_the_custom_colour() -> None:
    """The Custom palette is built from the custom colour, and follows a change of it."""
    blue = palette_swatch(Palette.CUSTOM, color=(0, 80, 255))
    assert all(b > r for r, _, b in blue)
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.LUMINA, palette=Palette.CUSTOM))
    engine.update_settings(
        EngineSettings(effect=Effect.LUMINA, palette=Palette.CUSTOM, color=(0, 255, 0))
    )
    frame = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=60).astype(int)
    assert frame[:, 1].sum() > 3 * frame[:, 0].sum()


def test_night_light_fades_to_the_custom_colour() -> None:
    """The night light fades from the music to the custom colour at its own brightness."""
    engine = Engine(_tv_layout(), EngineSettings(color=(255, 147, 41), brightness=0.2))
    _play(engine, 1.0)
    frame = np.zeros((1, 3), np.uint8)
    for _ in range(int(4 / DT)):
        frame = np.frombuffer(engine.render_night_light(DT, 0.4), np.uint8).reshape(-1, 3)
    assert (np.abs(frame.astype(int) - [102, 59, 16]) <= 1).all()


def test_hex_colours_round_trip() -> None:
    """Custom colours are read and written as #rrggbb."""
    assert parse_hex_color("#FF9329") == (255, 147, 41)
    assert parse_hex_color("00ff00") == (0, 255, 0)
    assert hex_color((255, 147, 41)) == "#ff9329"
    for bad in ("#fff", "red", "#gg0000"):
        with pytest.raises(ValueError):  # noqa: PT011
            parse_hex_color(bad)
