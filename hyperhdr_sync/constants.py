"""Constants for the HyperHDR Sync provider."""

from __future__ import annotations

from typing import Final

# Setup data (collected by the setup flow).
CONF_HOST: Final[str] = "host"
CONF_WEB_PORT: Final[str] = "web_port"
CONF_UDP_PORT: Final[str] = "udp_port"
CONF_TOKEN: Final[str] = "token"  # noqa: S105 - config key
# LED centres as [x0, y0, x1, y1, ...], kept so the lights still work when
# HyperHDR's web API cannot be reached while Music Assistant starts.
CONF_LED_LAYOUT: Final[str] = "led_layout"

# Settings (changeable while playing).
CONF_EFFECT: Final[str] = "effect"
CONF_PALETTE: Final[str] = "palette"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_SENSITIVITY_DB: Final[str] = "sensitivity_db"
CONF_RESPONSE: Final[str] = "response"
CONF_LATENCY_MS: Final[str] = "latency_ms"

DEFAULT_BRIGHTNESS: Final[int] = 100
DEFAULT_SENSITIVITY_DB: Final[int] = 0
# Covers the network hop plus HyperHDR's own smoothing, which delays the LEDs a little.
DEFAULT_LATENCY_MS: Final[int] = 120

# Frames per second rendered and sent to HyperHDR.
RENDER_RATE_HZ: Final[int] = 40

# Scheduled beats older than this when they come due are skipped (e.g. after a seek).
STALE_BEAT_US: Final[int] = 250_000

# How often the LED layout is read again from HyperHDR, at most.
LAYOUT_REFRESH_INTERVAL_S: Final[float] = 60.0
