"""
Web UI of the HyperHDR bridge: what the LEDs show now, a preview of new settings, Apply.

The page shows two pictures of the screen with its LEDs: what HyperHDR gets right now,
and a preview rendered by a second engine with the settings being tried out. The
preview follows the same music as the LEDs (or a built-in test beat when nothing
plays) and never reaches HyperHDR. Apply switches the LEDs to the previewed settings.

Inside Home Assistant the page is the add-on's Ingress panel: the address and port
come from the Supervisor, only the Supervisor may connect, and applied settings are
saved as the add-on's options so they survive restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from hyperhdr_sync.engine import Effect, EngineSettings, Palette, Response, palette_swatch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from hyperhdr_bridge import Bridge

    Saver = Callable[[dict[str, Any]], Awaitable[None]]
    DemoFeatures = Callable[[float], tuple[int, list[int], int]]

LOGGER = logging.getLogger("hyperhdr_bridge")

PAGE = Path(__file__).with_name("web_ui.html")
UI_RATE_HZ = 25
SUPERVISOR_URL = "http://supervisor"
_SUPERVISOR_TIMEOUT_S = 10.0

EFFECT_LABELS = {
    Effect.SPECTRUM: "Spectrum",
    Effect.VU: "Level meter",
    Effect.PULSE: "Pulse",
    Effect.RIPPLE: "Ripple",
    Effect.SCROLL: "Scroll",
    Effect.ENERGY: "Energy",
    Effect.AMBIENT: "Ambient",
}
PALETTE_LABELS = {
    Palette.ALBUM: "Album art",
    Palette.RAINBOW: "Rainbow",
    Palette.SUNSET: "Sunset",
    Palette.OCEAN: "Ocean",
    Palette.FIRE: "Fire",
    Palette.FOREST: "Forest",
    Palette.NEON: "Neon",
}
RESPONSE_LABELS = {
    Response.SNAPPY: "Snappy",
    Response.BALANCED: "Balanced",
    Response.SMOOTH: "Smooth",
}

BRIGHTNESS_RANGE = (0, 100)
SENSITIVITY_RANGE = (-12.0, 12.0)
LATENCY_RANGE = (0, 2000)


def settings_to_dict(settings: EngineSettings, latency_ms: int) -> dict[str, Any]:
    """
    Return settings with the names and units of the add-on options and the page.

    :param settings: Rendering settings.
    :param latency_ms: How far ahead of the music the lights are rendered.
    """
    return {
        "effect": settings.effect.value,
        "palette": settings.palette.value,
        "brightness": round(settings.brightness * 100),
        "sensitivity": float(settings.sensitivity_db),
        "response": settings.response.value,
        "latency_ms": int(latency_ms),
    }


def parse_settings(data: Any) -> tuple[EngineSettings, int]:
    """
    Validate settings sent by the page.

    :param data: Mapping with the keys of :func:`settings_to_dict`.
    :return: Rendering settings and latency in milliseconds.
    :raises ValueError: When a value is missing, unknown or out of range.
    """
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")  # noqa: TRY004 - callers expect ValueError
    try:
        effect = Effect(str(data["effect"]))
        palette = Palette(str(data["palette"]))
        response = Response(str(data["response"]))
        brightness = int(data["brightness"])
        sensitivity = float(data["sensitivity"])
        latency_ms = int(data["latency_ms"])
    except (KeyError, TypeError) as err:
        raise ValueError(f"missing or invalid setting: {err}") from err
    for name, value, (low, high) in (
        ("brightness", brightness, BRIGHTNESS_RANGE),
        ("sensitivity", sensitivity, SENSITIVITY_RANGE),
        ("latency_ms", latency_ms, LATENCY_RANGE),
    ):
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    settings = EngineSettings(
        effect=effect,
        palette=palette,
        brightness=brightness / 100.0,
        sensitivity_db=sensitivity,
        response=response,
    )
    return settings, latency_ms


class WebUi:
    """The web server behind the page."""

    def __init__(
        self,
        bridge: Bridge,
        demo: DemoFeatures,
        *,
        saver: Saver | None = None,
        allowed_remote: str | None = None,
    ) -> None:
        """
        Initialize the web UI.

        :param bridge: The running bridge.
        :param demo: Sound features of the test beat, for previews while nothing plays.
        :param saver: Stores applied settings permanently; without it they last until
            the bridge restarts.
        :param allowed_remote: The only client address allowed to connect, if any.
        """
        self._bridge = bridge
        self._demo = demo
        self._saver = saver
        self._allowed_remote = allowed_remote
        self._clients: set[web.WebSocketResponse] = set()
        self._ticker: asyncio.Task[None] | None = None
        self._swatch_key: tuple[tuple[int, int, int], ...] | None = None
        self._swatches: list[dict[str, Any]] = []

    def make_app(self) -> web.Application:
        """Return the aiohttp application."""
        app = web.Application(middlewares=[self._guard])
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/apply", self._apply)
        app.router.add_get("/api/ws", self._websocket)
        app.on_shutdown.append(self._on_shutdown)
        return app

    # ---------------------------------------------------------------- routes

    @web.middleware
    async def _guard(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        """Refuse every client but the allowed one (the Supervisor's Ingress proxy)."""
        if self._allowed_remote is not None and request.remote != self._allowed_remote:
            LOGGER.warning("Refused web UI request from %s", request.remote)
            raise web.HTTPForbidden
        return await handler(request)

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def _state(self, _request: web.Request) -> web.Response:
        layout = self._bridge.engine.layout
        return web.json_response(
            {
                "leds": {
                    "x": [round(float(v), 4) for v in layout.x],
                    "y": [round(float(v), 4) for v in layout.y],
                },
                "settings": self._current(),
                "effects": [{"id": e.value, "label": label} for e, label in EFFECT_LABELS.items()],
                "palettes": self._palettes(),
                "responses": [
                    {"id": r.value, "label": label} for r, label in RESPONSE_LABELS.items()
                ],
                "ranges": {
                    "brightness": BRIGHTNESS_RANGE,
                    "sensitivity": SENSITIVITY_RANGE,
                    "latency_ms": LATENCY_RANGE,
                },
                "playing": self._bridge.streaming,
                "saves": self._saver is not None,
            }
        )

    async def _apply(self, request: web.Request) -> web.Response:
        try:
            settings, latency_ms = parse_settings(await request.json())
        except (ValueError, json.JSONDecodeError) as err:
            return web.json_response({"error": str(err)}, status=400)
        self._bridge.apply(settings, latency_ms)
        if self._bridge.preview is not None:
            self._bridge.preview.update_settings(settings)
        result: dict[str, Any] = {"settings": self._current(), "saved": False}
        if self._saver is not None:
            try:
                await self._saver(self._current())
                result["saved"] = True
            except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
                LOGGER.warning("Applied, but could not save the settings: %s", err)
                result["error"] = f"Applied, but not saved: {err}"
        return web.json_response(result)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Stream LED frames to the page and take the settings to preview."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        self._bridge.start_preview()
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick())
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                await self._on_message(ws, message.data)
        finally:
            self._clients.discard(ws)
            if not self._clients:
                if self._ticker is not None:
                    self._ticker.cancel()
                    self._ticker = None
                self._bridge.stop_preview()
        return ws

    async def _on_message(self, ws: web.WebSocketResponse, text: str) -> None:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("type") != "preview":
                return
            settings, _ = parse_settings(data.get("settings"))
        except (ValueError, json.JSONDecodeError) as err:
            await ws.send_json({"type": "error", "message": str(err)})
            return
        self._bridge.start_preview().update_settings(settings)

    async def _on_shutdown(self, _app: web.Application) -> None:
        for ws in list(self._clients):
            await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Bridge stopping")

    # ------------------------------------------------------------- streaming

    async def _tick(self) -> None:
        """
        Send both pictures to every open page, as one binary message per frame.

        The message holds the current LED frame followed by the preview frame, each
        ``R G B`` per LED. A JSON status message goes out whenever playback or the
        album colours change.
        """
        period = 1.0 / UI_RATE_HZ
        start = last = time.perf_counter()
        last_beat = -1
        status: dict[str, Any] | None = None
        while self._clients:
            await asyncio.sleep(period)
            now = time.perf_counter()
            dt, last = now - last, now
            preview = self._bridge.preview
            if preview is None:
                continue
            if not self._bridge.streaming:
                loudness, spectrum, beat = self._demo(now - start)
                preview.feed(
                    loudness=loudness, spectrum=spectrum, onset=200 if beat != last_beat else None
                )
                last_beat = beat
            frame = self._bridge.last_rgb + preview.render(dt)
            current = {"type": "status", "playing": self._bridge.streaming}
            current["palettes"] = self._palettes()
            for ws in list(self._clients):
                with contextlib.suppress(ConnectionResetError, RuntimeError):
                    if current != status:
                        await ws.send_json(current)
                    await ws.send_bytes(frame)
            status = current

    # --------------------------------------------------------------- helpers

    def _current(self) -> dict[str, Any]:
        return settings_to_dict(self._bridge.engine.settings, self._bridge.latency_ms)

    def _palettes(self) -> list[dict[str, Any]]:
        """Return the palettes with sample colours; the album one follows the artwork."""
        album = self._bridge.engine.album_colors
        if album != self._swatch_key:
            self._swatch_key = album
            self._swatches = [
                {
                    "id": palette.value,
                    "label": label,
                    "colors": [
                        f"#{r:02x}{g:02x}{b:02x}" for r, g, b in palette_swatch(palette, album)
                    ],
                }
                for palette, label in PALETTE_LABELS.items()
            ]
        return self._swatches


# ------------------------------------------------------------------ Supervisor


def _supervisor_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}


async def supervisor_info(session: aiohttp.ClientSession) -> dict[str, Any]:
    """Return the add-on's own info from the Supervisor (needs ``SUPERVISOR_TOKEN``)."""
    async with session.get(
        f"{SUPERVISOR_URL}/addons/self/info",
        headers=_supervisor_headers(),
        timeout=aiohttp.ClientTimeout(total=_SUPERVISOR_TIMEOUT_S),
    ) as response:
        response.raise_for_status()
        body = await response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ValueError("the Supervisor sent no add-on info")  # noqa: TRY004
    return data


def supervisor_saver(session: aiohttp.ClientSession) -> Saver:
    """Return a saver that stores applied settings as the add-on's options."""

    async def save(changes: dict[str, Any]) -> None:
        # The Supervisor replaces all options at once, so merge into the current ones.
        options = {**(await supervisor_info(session)).get("options", {}), **changes}
        async with session.post(
            f"{SUPERVISOR_URL}/addons/self/options",
            json={"options": options},
            headers=_supervisor_headers(),
            timeout=aiohttp.ClientTimeout(total=_SUPERVISOR_TIMEOUT_S),
        ) as response:
            if response.status >= 400:
                raise ValueError(f"the Supervisor refused the options: {await response.text()}")

    return save


async def start_web_ui(  # noqa: PLR0913
    bridge: Bridge,
    demo: DemoFeatures,
    session: aiohttp.ClientSession,
    *,
    host: str,
    port: int,
    supervisor: bool,
) -> web.AppRunner:
    """
    Start the web UI.

    :param bridge: The running bridge.
    :param demo: Sound features of the test beat.
    :param session: HTTP session for talking to the Supervisor.
    :param host: Address to listen on (ignored with ``supervisor``).
    :param port: Port to listen on (ignored with ``supervisor``).
    :param supervisor: Run as the Home Assistant add-on's Ingress panel.
    """
    saver: Saver | None = None
    allowed_remote: str | None = None
    if supervisor:
        info = await supervisor_info(session)
        # A host-network add-on is reached on the gateway of Home Assistant's internal
        # network, so the panel is not exposed on the LAN; the Supervisor itself sits
        # at the next address and is the only client that may connect.
        gateway = ipaddress.ip_address(str(info["ip_address"]))
        host, port = str(gateway), int(info["ingress_port"])
        allowed_remote = str(gateway + 1)
        saver = supervisor_saver(session)
    runner = web.AppRunner(
        WebUi(bridge, demo, saver=saver, allowed_remote=allowed_remote).make_app(),
        access_log=None,
    )
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    LOGGER.info("Web UI listening on http://%s:%d", host, port)
    return runner
