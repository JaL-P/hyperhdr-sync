"""
LED geometry for a HyperHDR instance.

HyperHDR describes every LED as a rectangle of the picture it samples
(``hmin``/``hmax``/``vmin``/``vmax``, all in [0, 1], with ``v == 0`` at the top).
The effects need a few derived coordinates per LED, computed once here:

``x``, ``y``
    Centre of the LED's rectangle (``y == 0`` is the top of the screen).
``ring``
    Position along the screen's perimeter in [0, 1): 0 at bottom-centre, then
    clockwise as seen from the front (bottom-left, left edge, top, right edge).
    Arc-length based, so LEDs keep their physical spacing around corners.
``mirror``
    0 at bottom-centre rising to 1 at top-centre, identical on the left and right
    halves. Effects built on it are left/right symmetric.

This module depends on numpy only, so it works both inside Music Assistant and in
the standalone bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

# Width / height of the surface the LEDs surround; only affects how much of the
# perimeter the horizontal edges take up compared to the vertical ones.
DEFAULT_ASPECT: float = 16 / 9


@dataclass(frozen=True)
class LedLayout:
    """Per-LED coordinates, each a float32 array of length ``count``."""

    count: int
    x: np.ndarray
    y: np.ndarray
    ring: np.ndarray
    mirror: np.ndarray

    @classmethod
    def from_serverinfo(
        cls, leds: list[dict[str, Any]], aspect: float = DEFAULT_ASPECT
    ) -> LedLayout:
        """
        Build a layout from the ``leds`` array of a HyperHDR ``serverinfo`` reply.

        :param leds: List of LED rectangles (``hmin``, ``hmax``, ``vmin``, ``vmax``).
        :param aspect: Width / height of the screen the LEDs surround.
        """
        if not leds:
            raise ValueError("HyperHDR reported no LEDs")
        x = np.array([(float(led["hmin"]) + float(led["hmax"])) / 2 for led in leds], np.float32)
        y = np.array([(float(led["vmin"]) + float(led["vmax"])) / 2 for led in leds], np.float32)
        return cls.from_centres(x, y, aspect)

    @classmethod
    def strip(cls, count: int, aspect: float = DEFAULT_ASPECT) -> LedLayout:
        """
        Build a fallback layout: ``count`` LEDs evenly spaced around the perimeter.

        Used when the real layout cannot be read. LED 0 sits at bottom-centre and the
        strip runs clockwise, which is HyperHDR's default LED order.

        :param count: Number of LEDs.
        :param aspect: Width / height of the screen the LEDs surround.
        """
        if count <= 0:
            raise ValueError("LED count must be positive")
        ring = (np.arange(count, dtype=np.float32) + 0.5) / count
        x, y = _ring_to_xy(ring, aspect)
        return cls(count, x, y, ring, _mirror(ring))

    @classmethod
    def from_centres(
        cls, x: np.ndarray, y: np.ndarray, aspect: float = DEFAULT_ASPECT
    ) -> LedLayout:
        """
        Build a layout from LED centre points.

        :param x: Horizontal centres in [0, 1], 0 at the left.
        :param y: Vertical centres in [0, 1], 0 at the top.
        :param aspect: Width / height of the screen the LEDs surround.
        """
        x = np.clip(np.asarray(x, np.float32), 0.0, 1.0)
        y = np.clip(np.asarray(y, np.float32), 0.0, 1.0)
        ring = _xy_to_ring(x, y, aspect)
        return cls(len(x), x, y, ring, _mirror(ring))

    @classmethod
    def from_list(cls, values: list[float], aspect: float = DEFAULT_ASPECT) -> LedLayout:
        """
        Rebuild a layout stored with :meth:`to_list`.

        :param values: LED centres as ``[x0, y0, x1, y1, ...]``.
        :param aspect: Width / height of the screen the LEDs surround.
        """
        if not values or len(values) % 2:
            raise ValueError("Stored LED layout is empty or incomplete")
        points = np.asarray(values, np.float32).reshape(-1, 2)
        return cls.from_centres(points[:, 0], points[:, 1], aspect)

    def to_list(self) -> list[float]:
        """Return the LED centres as ``[x0, y0, x1, y1, ...]``, for storing in a config."""
        points = np.stack([self.x, self.y], axis=1).reshape(-1)
        return [round(float(value), 4) for value in points]


def _mirror(ring: np.ndarray) -> np.ndarray:
    """Fold the ring so both halves run 0 (bottom-centre) to 1 (top-centre)."""
    return (1.0 - np.abs(1.0 - 2.0 * ring)).astype(np.float32)


def _xy_to_ring(x: np.ndarray, y: np.ndarray, aspect: float) -> np.ndarray:
    """Project each point onto the nearest screen edge and measure the perimeter position."""
    width, height = aspect, 1.0
    perimeter = 2 * (width + height)
    # Distance to each edge, in the same physical units for all four.
    d_left, d_right = x * width, (1.0 - x) * width
    d_top, d_bottom = y * height, (1.0 - y) * height
    nearest = np.argmin(np.stack([d_bottom, d_left, d_top, d_right]), axis=0)
    # Arc length from bottom-centre, clockwise: bottom (to the left), left (upwards),
    # top (to the right), right (downwards), then the rest of the bottom edge.
    on_bottom = np.where(x <= 0.5, (0.5 - x) * width, perimeter - (x - 0.5) * width)
    on_left = width / 2 + (1.0 - y) * height
    on_top = width / 2 + height + x * width
    on_right = width / 2 + height + width + y * height
    arc = np.choose(nearest, [on_bottom, on_left, on_top, on_right])
    ring: np.ndarray = ((arc / perimeter) % 1.0).astype(np.float32)
    return ring


def _ring_to_xy(ring: np.ndarray, aspect: float) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of :func:`_xy_to_ring` for points lying exactly on the perimeter."""
    width, height = aspect, 1.0
    perimeter = 2 * (width + height)
    arc = ring * perimeter
    x = np.empty_like(ring)
    y = np.empty_like(ring)
    b1, b2, b3, b4 = width / 2, width / 2 + height, width * 1.5 + height, width * 1.5 + 2 * height
    for i, a in enumerate(arc):
        if a < b1:
            x[i], y[i] = 0.5 - a / width, 1.0
        elif a < b2:
            x[i], y[i] = 0.0, 1.0 - (a - b1) / height
        elif a < b3:
            x[i], y[i] = (a - b2) / width, 0.0
        elif a < b4:
            x[i], y[i] = 1.0, (a - b3) / height
        else:
            x[i], y[i] = 1.0 - (a - b4) / width, 1.0
    return x.astype(np.float32), y.astype(np.float32)
