"""Readers for the HyperHDR Sync playback settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_EFFECT,
    CONF_LATENCY_MS,
    CONF_PALETTE,
    CONF_RESPONSE,
    CONF_SENSITIVITY_DB,
    DEFAULT_BRIGHTNESS,
    DEFAULT_LATENCY_MS,
    DEFAULT_SENSITIVITY_DB,
)
from .engine import (
    DEFAULT_COLOR,
    Effect,
    EngineSettings,
    Palette,
    Response,
    hex_color,
    parse_hex_color,
)

if TYPE_CHECKING:
    from enum import StrEnum

    from music_assistant_models.config_entries import ProviderConfig

DEFAULT_EFFECT = Effect.SPECTRUM
DEFAULT_PALETTE = Palette.ALBUM
DEFAULT_RESPONSE = Response.BALANCED
DEFAULT_COLOR_HEX = hex_color(DEFAULT_COLOR)


class HyperHdrSettings(NamedTuple):
    """The playback settings of one HyperHDR light player."""

    engine: EngineSettings
    latency_ms: int


def get_settings(config: ProviderConfig) -> HyperHdrSettings:
    """
    Return the playback settings stored in a provider config.

    :param config: Provider config to read the settings from.
    """
    brightness = cast("int", config.get_value(CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS))
    return HyperHdrSettings(
        engine=EngineSettings(
            effect=_enum(Effect, config.get_value(CONF_EFFECT), DEFAULT_EFFECT),
            palette=_enum(Palette, config.get_value(CONF_PALETTE), DEFAULT_PALETTE),
            brightness=max(0.0, min(1.0, float(brightness) / 100.0)),
            sensitivity_db=float(
                cast("int", config.get_value(CONF_SENSITIVITY_DB, DEFAULT_SENSITIVITY_DB))
            ),
            response=_enum(Response, config.get_value(CONF_RESPONSE), DEFAULT_RESPONSE),
            color=_color(config.get_value(CONF_COLOR)),
        ),
        latency_ms=int(cast("int", config.get_value(CONF_LATENCY_MS, DEFAULT_LATENCY_MS))),
    )


def _color(value: object) -> tuple[int, int, int]:
    """Return a ``#rrggbb`` setting as RGB, or the default colour when it is not one."""
    try:
        return parse_hex_color(str(value))
    except ValueError:
        return DEFAULT_COLOR


def _enum[T: StrEnum](enum: type[T], value: object, default: T) -> T:
    """Return ``value`` as a member of ``enum``, or ``default`` for an unknown value."""
    try:
        return enum(str(value))
    except ValueError:
        return default
