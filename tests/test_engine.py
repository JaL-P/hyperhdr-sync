"""Tests of the rendering engine and the LED layout; they need no server."""

import itertools
import math

import numpy as np
import pytest

from hyperhdr_sync.engine import (
    SPECTRUM_BINS,
    AlbumMode,
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
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.LUMINA, palette=Palette.ALBUM))
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
    for effect in (Effect.SPECTRUM, Effect.LUMINA, Effect.ENERGY):
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


def test_album_modes_pick_colours_differently() -> None:
    """Each artwork mode makes a different palette from the same cover colours."""
    cover = [(200, 30, 30), (20, 60, 200), (240, 230, 220), (30, 20, 20)]  # red, blue accent
    swatches = {
        mode: palette_swatch(Palette.ALBUM, cover, count=5, album_mode=mode) for mode in AlbumMode
    }
    assert len({tuple(s) for s in swatches.values()}) == len(AlbumMode), "all modes differ"
    assert all(r > b for r, _, b in swatches[AlbumMode.DOMINANT]), "dominant stays red"
    accent = swatches[AlbumMode.ACCENT][0]
    assert accent[2] > accent[0], "accent starts with the blue accent"
    assert min(min(c) for c in swatches[AlbumMode.PASTEL]) > 80, "pastel is soft"
    contrast = swatches[AlbumMode.CONTRAST]
    assert contrast[0][0] > contrast[0][2]
    assert contrast[-1][2] > contrast[-1][0], "the opposite of red is blue-green"
    natural = palette_swatch(Palette.ALBUM, [(90, 90, 90)], count=3, album_mode=AlbumMode.NATURAL)
    assert all(r == g == b for r, g, b in natural), "natural keeps a grey cover grey"


def test_changing_the_album_mode_rebuilds_the_palette() -> None:
    """Switching the artwork mode while playing changes the colours straight away."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.LUMINA, palette=Palette.ALBUM))
    engine.set_album_colors([(200, 30, 30), (20, 60, 200)])
    red = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=60).astype(int).sum(axis=0)
    engine.update_settings(
        EngineSettings(effect=Effect.LUMINA, palette=Palette.ALBUM, album_mode=AlbumMode.ACCENT)
    )
    blue = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=60).astype(int).sum(axis=0)
    assert red[0] > red[2]
    assert blue[2] > blue[0]


def _effect_signature(effect: Effect) -> dict[str, np.ndarray]:
    """Play the same 12 s of music through an effect and describe how its LEDs behave."""
    engine = Engine(_tv_layout(), EngineSettings(effect=effect, palette=Palette.RAINBOW))
    frames = []
    for i in range(int(12 / DT)):
        t = i * DT
        kick = math.exp(-((t * 2.0) % 1.0) * 7.0)
        hat = math.exp(-((t * 2.0 + 0.5) % 1.0) * 10.0)
        sweep = 0.5 + 0.5 * math.sin(t * 0.9)
        level = 0.25 if 6.0 <= t < 8.0 else 1.0  # a quiet break
        spectrum = []
        for band in range(SPECTRUM_BINS):
            x = band / (SPECTRUM_BINS - 1)
            value = max(
                kick * math.exp(-(((x - 0.08) / 0.12) ** 2)),
                (0.5 + 0.3 * math.sin(t * 2.3 + band))
                * math.exp(-(((x - 0.2 - 0.6 * sweep) / 0.15) ** 2)),
                hat * math.exp(-(((x - 0.9) / 0.1) ** 2)),
            )
            spectrum.append(int((0.3 + 0.55 * value * level) * 65535))
        engine.feed(loudness=int((0.5 + 0.35 * kick * level) * 65535), spectrum=spectrum)
        frames.append(np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3).max(axis=1))
    light = np.array(frames[80:], float) / 255.0  # (time, LED), after settling
    return {
        "pattern": light,
        "time": light.mean(axis=1),
        "space": light.mean(axis=0),
        "motion": np.abs(np.diff(light, axis=0)).mean(axis=0),
    }


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    scale = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / scale) if scale > 1e-9 else 0.0


def test_no_two_effects_do_the_same_thing() -> None:
    """
    Every pair of effects looks clearly different on the same music.

    Similarity averages how alike two effects are in their LED pattern, their brightness
    over time, where the light sits and where it moves. Bars that simply grow with the
    loudness, for example, score about 0.8 against each other.
    """
    # MilkDrop shows pictures drawn outside the engine; without them it is Liquid.
    effects = [effect for effect in Effect if effect not in (Effect.MANUAL, Effect.MILKDROP)]
    signatures = {effect: _effect_signature(effect) for effect in effects}
    worst = []
    for a, b in itertools.combinations(effects, 2):
        score = (
            sum(
                max(0.0, _correlation(signatures[a][key], signatures[b][key]))
                for key in ("pattern", "time", "space", "motion")
            )
            / 4
        )
        worst.append((round(score, 2), a.value, b.value))
    worst.sort(reverse=True)
    assert worst[0][0] < 0.7, f"effects too alike: {worst[:3]}"


@pytest.mark.parametrize("count", [8, 16, 24, 60, 120])
def test_retro_keeps_its_colours_on_short_strips(count: int) -> None:
    """Retro's peak dots never cover a strip: most lit LEDs keep their band colour."""
    engine = Engine(LedLayout.strip(count), EngineSettings(effect=Effect.RETRO))
    frame = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=60).astype(int)
    lit = frame[frame.max(axis=1) > 40]
    white = lit.min(axis=1) > 150
    assert len(lit) >= count // 4
    assert white.sum() <= len(lit) // 2, f"{white.sum()} of {len(lit)} lit LEDs are white"


@pytest.mark.parametrize("count", [32, 33, 34, 40, 50, 60, 120, 301])
def test_retro_shows_a_peak_dot_on_every_block_at_full_scale(count: int) -> None:
    """With every band at its maximum each Retro block still has its white peak dot."""
    layout = LedLayout.strip(count)
    engine = Engine(layout, EngineSettings(effect=Effect.RETRO))
    frame = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=80).astype(int)
    dots = (frame.min(axis=1) > 150) & (frame.max(axis=1) == frame.min(axis=1))
    ring = (layout.ring - layout.ring.min()) / (layout.ring.max() - layout.ring.min())
    block = np.minimum((ring * 8).astype(int), 7)
    per_block = np.bincount(block[dots], minlength=8)
    assert per_block.min() >= 1, f"blocks without a peak dot: {per_block.tolist()}"
    assert per_block.max() <= 3, f"peak dots too wide: {per_block.tolist()}"


def _play_loudness(effect: Effect, *, kicks: bool) -> np.ndarray:
    """Play 6 s of loudness without spectrum, steady or with kicks; return the frames."""
    engine = Engine(_tv_layout(), EngineSettings(effect=effect, palette=Palette.RAINBOW))
    frames = []
    for i in range(int(6 / DT)):
        t = i * DT
        kick = math.exp(-((t * 2.0) % 1.0) * 7.0) if kicks else 0.5
        level = 0.3 if kicks and 3.0 <= t < 4.0 else 1.0
        frame = _frame(engine, None, loudness=int((0.45 + 0.4 * kick * level) * 65535))
        frames.append(frame.astype(float) / 255.0)
    return np.array(frames[40:])  # (time, LED, colour), after settling


@pytest.mark.parametrize("effect", [effect for effect in Effect if effect != Effect.MANUAL])
def test_every_effect_follows_a_loudness_only_stream(effect: Effect) -> None:
    """Without spectrum data every effect still lights up and shows how the music goes."""
    steady = _play_loudness(effect, kicks=False)
    kicking = _play_loudness(effect, kicks=True)
    assert kicking.max() > 0.25, "dark without spectrum data"
    assert np.abs(kicking - steady).mean() > 0.05, "the same light whatever the music does"


def test_own_palette_blends_or_shows_blocks() -> None:
    """The user's palette blends its colours, or as blocks shows only the colours themselves."""
    red, blue = (255, 0, 0), (0, 0, 255)
    blended = palette_swatch(Palette.MINE, count=9, colors=[red, blue])
    assert blended[0] == red
    assert blended[-1] == blue
    assert any(c not in (red, blue) for c in blended), "a gradient has colours in between"
    engine = Engine(
        _tv_layout(), EngineSettings(palette=Palette.MINE, colors=(red, blue), steps=True)
    )
    swatch = engine._lut_linear  # noqa: SLF001 - the table the effects draw from
    colours = {tuple(int(v * 255 + 0.5) for v in row) for row in swatch}
    assert colours == {red, blue}
    assert palette_swatch(Palette.MINE) == palette_swatch(Palette.RAINBOW), "no colours: rainbow"


def _picture(left: tuple[int, int, int], right: tuple[int, int, int]) -> np.ndarray:
    """Return a 96x54 picture: one colour on the left half, another on the right."""
    picture = np.zeros((54, 96, 3), np.float32)
    picture[:, :48] = np.array(left, np.float32) / 255.0
    picture[:, 48:] = np.array(right, np.float32) / 255.0
    return picture


def test_milkdrop_shows_the_picture_at_the_edges() -> None:
    """The LEDs take the colours at their edge of the picture, as a screen grabber would."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.MILKDROP, palette=Palette.MILKDROP))
    picture = _picture((255, 0, 0), (0, 0, 255))
    frame = np.zeros((1, 3), int)
    for _ in range(30):
        engine.feed(loudness=LOUD, spectrum=[LOUD] * SPECTRUM_BINS, picture=picture)
        frame = np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3).astype(int)
    left, right = frame[:14], frame[-14:]
    assert (left[:, 0] > 200).all()
    assert (left[:, 2] < 30).all()
    assert (right[:, 2] > 200).all()
    assert (right[:, 0] < 30).all()


def test_milkdrop_tint_keeps_the_brightness() -> None:
    """Another palette tints the picture like Music Assistant does: its brightness stays."""
    picture = _picture((255, 0, 0), (0, 0, 255))
    frames = {}
    for palette in (Palette.MILKDROP, Palette.OCEAN):
        engine = Engine(_tv_layout(), EngineSettings(effect=Effect.MILKDROP, palette=palette))
        frame = np.zeros((1, 3))
        for _ in range(30):
            engine.feed(loudness=LOUD, spectrum=[LOUD] * SPECTRUM_BINS, picture=picture)
            frame = np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3) / 255.0
        frames[palette] = frame
    weights = np.array([0.3, 0.59, 0.11])
    own, tinted = frames[Palette.MILKDROP], frames[Palette.OCEAN]
    assert np.allclose(own @ weights, tinted @ weights, atol=0.03), "same luminosity"
    assert not np.allclose(own, tinted, atol=0.1), "different colours"
    assert (tinted[:14, 0] < tinted[:14, 2]).all(), "ocean blues instead of red"


def test_milkdrop_falls_back_to_liquid_without_a_picture() -> None:
    """Until a picture arrives (Chromium starting), Liquid stands in rather than darkness."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.MILKDROP))
    frame = _settle(engine, [LOUD] * SPECTRUM_BINS, frames=60)
    assert frame.max() > 50
    assert engine.uses_milkdrop
    assert not engine.has_fresh_picture()


def test_milkdrop_palette_follows_the_picture() -> None:
    """With another effect, the MilkDrop palette takes the colours round the picture's edges."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.LUMINA, palette=Palette.MILKDROP))
    assert engine.uses_milkdrop
    picture = _picture((0, 255, 0), (0, 255, 0))
    frame = np.zeros((1, 3), int)
    for _ in range(120):
        engine.feed(loudness=LOUD, spectrum=[LOUD] * SPECTRUM_BINS, picture=picture)
        frame = np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3).astype(int)
    assert frame.max() > 50
    assert (frame[:, 1] >= frame[:, 0]).all()
    assert (frame[:, 1] >= frame[:, 2]).all()
    assert frame[:, 1].sum() > 3 * frame[:, 0].sum(), "green, from the picture"


def test_synth_waveform_follows_the_music() -> None:
    """The waveform made from the spectrum is flat in silence and loud with loud music."""
    engine = Engine(_tv_layout(), EngineSettings(effect=Effect.MILKDROP))
    assert engine.synth_waveform() == bytes([128]) * 1024
    _settle(engine, [LOUD] * SPECTRUM_BINS, frames=30)
    wave = np.frombuffer(engine.synth_waveform(), np.uint8).astype(int) - 128
    assert len(wave) == 1024
    assert np.abs(wave).max() > 60


def test_milkdrop_palette_tints_like_music_assistant() -> None:
    """The MilkDrop palette blends the cover's colour over the picture; its brightness stays."""
    picture = _picture((255, 0, 0), (0, 0, 255))
    frames = []
    for tint in (None, (40, 200, 90)):
        engine = Engine(
            _tv_layout(), EngineSettings(effect=Effect.MILKDROP, palette=Palette.MILKDROP)
        )
        engine.set_milkdrop_tint(tint)
        assert engine.milkdrop_tint == tint
        frame = np.zeros((1, 3))
        for _ in range(30):
            engine.feed(loudness=LOUD, spectrum=[LOUD] * SPECTRUM_BINS, picture=picture)
            frame = np.frombuffer(engine.render(DT), np.uint8).reshape(-1, 3) / 255.0
        frames.append(frame)
    own, tinted = frames
    weights = np.array([0.3, 0.59, 0.11])
    assert np.allclose(own @ weights, tinted @ weights, atol=0.03), "same brightness"
    assert (own[:14, 0] > own[:14, 1]).all(), "untinted: the preset's own red"
    assert (tinted[:14, 1] > tinted[:14, 0]).all(), "tinted: green from the cover"
