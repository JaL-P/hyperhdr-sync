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
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from hyperhdr_sync.engine import (
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

# (label, short description) per effect, in the order the page shows them.
EFFECT_LABELS = {
    Effect.LUMINA: ("Lumina", "One calm colour that gets brighter as the music gets louder"),
    Effect.COLORA: ("Colora", "Steady brightness; the colour follows the sound, bass to treble"),
    Effect.SPECTRUM: ("Spectrum", "Bass at the bottom rising to treble at the top"),
    Effect.RETRO: (
        "Retro",
        "Graphic equaliser: eight frequency blocks with peak dots, like an old hi-fi",
    ),
    Effect.SCANNER: ("Scanner", "A light sweeping back and forth, faster when louder"),
    Effect.RHYTHM: ("Rhythm", "Colour blocks that swap on every beat"),
    Effect.PARTY: ("Party", "Random colour bursts on beats, sparkles on the treble"),
    Effect.VU: (
        "Level meter",
        "One meter climbing both sides with the loudness, with a falling peak",
    ),
    Effect.PULSE: ("Pulse", "Every LED flashes on the beat and goes dark in between"),
    Effect.RIPPLE: ("Ripple", "A wave from the bottom to the top on every beat"),
    Effect.SCROLL: ("Scroll", "The colour of the sound flowing upwards"),
    Effect.ENERGY: (
        "Energy",
        "Bass rises from the bottom, treble drops from the top; they meet in between",
    ),
    Effect.AMBIENT: (
        "Ambient",
        "Calm colours turning around the screen; the music sets the speed, not the brightness",
    ),
    Effect.FLOW: ("Flow", "Comets of light circling the screen, faster when louder"),
    Effect.CENTRE_PULSE: (
        "Centre pulse",
        "Every beat sends a pulse from the middle of each edge to the corners",
    ),
    Effect.FIREFLIES: ("Fireflies", "Small lights drifting around the screen; beats speed them up"),
    Effect.SNOWFALL: ("Snowfall", "Glowing flakes falling down the sides, more with the treble"),
    Effect.CRYSTALS: (
        "Crystals",
        "Shards that flare with their own frequency and flutter with the treble",
    ),
    Effect.BRAIDS: (
        "Braids",
        "Two strands of colour winding along the sides, crossing as they move",
    ),
    Effect.EMBERS: ("Embers", "A flickering glow with sparks rising from the bottom on the bass"),
    Effect.AURORA: ("Aurora", "Slow, folded curtains of colour rising from the bottom"),
    Effect.HORIZON: (
        "Horizon",
        "A sun at the top pulsing with the bass, grid lines rushing down the sides",
    ),
    Effect.SIDE_BARS: (
        "Side bars",
        "Bars growing from the middle of each side: bass left, treble right",
    ),
    Effect.MANUAL: ("Manual", "Every LED in the custom colour; does not follow the music"),
}
PALETTE_LABELS = {
    Palette.ALBUM: "Album art",
    Palette.CUSTOM: "Custom colour",
    Palette.RAINBOW: "Rainbow",
    Palette.CLASSIC: "Classic",
    Palette.SUNSET: "Sunset",
    Palette.OCEAN: "Ocean",
    Palette.AURORA: "Aurora",
    Palette.ICE: "Ice",
    Palette.FIRE: "Fire",
    Palette.LAVA: "Lava",
    Palette.WARM: "Warm",
    Palette.FOREST: "Forest",
    Palette.NEON: "Neon",
    Palette.CANDY: "Candy",
    Palette.PASTEL: "Pastel",
}
# Palette.MINE is left out: the user makes it on the page instead of picking it.
ALBUM_MODE_LABELS = {
    AlbumMode.VIBRANT: ("Vibrant", "Every cover colour, boosted to full brightness"),
    AlbumMode.DOMINANT: ("Dominant", "Only the main colour of the cover, with nearby shades"),
    AlbumMode.ACCENT: ("Accent", "The cover's accent colour first, then its main colour"),
    AlbumMode.NATURAL: ("Natural", "The colours as they look on the cover, greys included"),
    AlbumMode.PASTEL: ("Pastel", "The cover colours softened towards white"),
    AlbumMode.CONTRAST: ("Contrast", "The main colour against its opposite"),
}
RESPONSE_LABELS = {
    Response.SNAPPY: "Snappy",
    Response.BALANCED: "Balanced",
    Response.SMOOTH: "Smooth",
}

BRIGHTNESS_RANGE = (0, 100)
SENSITIVITY_RANGE = (-12.0, 12.0)
LATENCY_RANGE = (0, 2000)
NIGHT_LIGHT_RANGE = (0, 100)
# Colours of a palette of the user's own, and how many such palettes the page keeps.
PALETTE_COLORS_RANGE = (2, 6)
MAX_MY_PALETTES = 12
MAX_PALETTE_NAME = 24


@dataclass(frozen=True)
class UiSettings:
    """Everything the page can change."""

    engine: EngineSettings
    latency_ms: int
    night_light: float
    """Night light brightness, 0..1 (0: hand the LEDs back to HyperHDR when idle)."""
    my_palettes: tuple[MyPalette, ...] = ()
    """The palettes the user made on the page, to pick from again."""


@dataclass(frozen=True)
class MyPalette:
    """A palette the user made: a name and its colours, blended or as blocks."""

    name: str
    colors: tuple[tuple[int, int, int], ...]
    steps: bool = False


def settings_to_dict(settings: UiSettings) -> dict[str, Any]:
    """
    Return settings with the names and units of the add-on options and the page.

    :param settings: The settings.
    """
    engine = settings.engine
    return {
        "effect": engine.effect.value,
        "palette": engine.palette.value,
        "brightness": round(engine.brightness * 100),
        "sensitivity": float(engine.sensitivity_db),
        "response": engine.response.value,
        "latency_ms": int(settings.latency_ms),
        "color": hex_color(engine.color),
        "night_light": round(settings.night_light * 100),
        "album_mode": engine.album_mode.value,
        "colors": [hex_color(c) for c in engine.colors],
        "steps": engine.steps,
        # Add-on options take no list inside a list: each palette's colours are one string.
        "my_palettes": [
            {"name": p.name, "colors": ",".join(hex_color(c) for c in p.colors), "steps": p.steps}
            for p in settings.my_palettes
        ],
    }


def parse_settings(data: Any) -> UiSettings:
    """
    Validate settings sent by the page.

    :param data: Mapping with the keys of :func:`settings_to_dict`.
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
        color = parse_hex_color(str(data["color"]))
        night_light = int(data["night_light"])
        album_mode = AlbumMode(str(data["album_mode"]))
        colors = _parse_colors(data.get("colors", []))
        steps = bool(data.get("steps", False))
        my_palettes = _parse_my_palettes(data.get("my_palettes", []))
    except (KeyError, TypeError) as err:
        raise ValueError(f"missing or invalid setting: {err}") from err
    if palette == Palette.MINE and len(colors) < PALETTE_COLORS_RANGE[0]:
        raise ValueError(f"your own palette needs at least {PALETTE_COLORS_RANGE[0]} colours")
    for name, value, (low, high) in (
        ("brightness", brightness, BRIGHTNESS_RANGE),
        ("sensitivity", sensitivity, SENSITIVITY_RANGE),
        ("latency_ms", latency_ms, LATENCY_RANGE),
        ("night_light", night_light, NIGHT_LIGHT_RANGE),
    ):
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    engine = EngineSettings(
        effect=effect,
        palette=palette,
        brightness=brightness / 100.0,
        sensitivity_db=sensitivity,
        response=response,
        color=color,
        album_mode=album_mode,
        colors=colors,
        steps=steps,
    )
    return UiSettings(engine, latency_ms, night_light / 100.0, my_palettes)


def _parse_colors(value: Any) -> tuple[tuple[int, int, int], ...]:
    """
    Parse the colours of a palette: a list of ``#rrggbb``, or one comma-separated string.

    :param value: The colours; empty for none.
    :raises ValueError: When a colour is invalid or there are too many.
    """
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        raise ValueError("colours must be a list")  # noqa: TRY004 - callers expect ValueError
    colors = tuple(parse_hex_color(str(item)) for item in items if str(item).strip())
    if len(colors) > PALETTE_COLORS_RANGE[1]:
        raise ValueError(f"a palette has at most {PALETTE_COLORS_RANGE[1]} colours")
    return colors


def _parse_my_palettes(value: Any) -> tuple[MyPalette, ...]:
    """
    Parse the user's saved palettes.

    :param value: A list of objects with ``name``, ``colors`` and ``steps``.
    :raises ValueError: When a palette is invalid or there are too many.
    """
    if not isinstance(value, list | tuple):
        raise ValueError("my_palettes must be a list")  # noqa: TRY004 - callers expect ValueError
    if len(value) > MAX_MY_PALETTES:
        raise ValueError(f"keep at most {MAX_MY_PALETTES} palettes of your own")
    palettes = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each palette must be an object")  # noqa: TRY004
        name = str(item.get("name", "")).strip()
        colors = _parse_colors(item.get("colors", []))
        if not name or len(name) > MAX_PALETTE_NAME:
            raise ValueError(f"a palette needs a name of 1 to {MAX_PALETTE_NAME} characters")
        if len(colors) < PALETTE_COLORS_RANGE[0]:
            raise ValueError(f"palette {name!r} needs at least {PALETTE_COLORS_RANGE[0]} colours")
        palettes.append(MyPalette(name, colors, bool(item.get("steps", False))))
    return tuple(palettes)


class WebUi:
    """The web server behind the page."""

    def __init__(
        self,
        bridge: Bridge,
        demo: DemoFeatures,
        *,
        saver: Saver | None = None,
        allowed_remote: str | None = None,
        my_palettes: tuple[MyPalette, ...] = (),
    ) -> None:
        """
        Initialize the web UI.

        :param bridge: The running bridge.
        :param demo: Sound features of the test beat, for previews while nothing plays.
        :param saver: Stores applied settings permanently; without it they last until
            the bridge restarts.
        :param allowed_remote: The only client address allowed to connect, if any.
        :param my_palettes: The palettes the user made, as saved last time.
        """
        self._my_palettes = my_palettes
        self._bridge = bridge
        self._demo = demo
        self._saver = saver
        self._allowed_remote = allowed_remote
        self._clients: set[web.WebSocketResponse] = set()
        self._ticker: asyncio.Task[None] | None = None
        self._swatch_key: tuple[object, ...] | None = None
        self._swatches: list[dict[str, Any]] = []
        self._album_key: tuple[tuple[int, int, int], ...] | None = None
        self._album_swatches: list[dict[str, Any]] = []
        # What the preview shows: "music" (the music, or the test beat while nothing
        # plays) or "idle" (what the LEDs do once the music stops: the night light).
        self._preview_mode = "music"
        self._preview_night_light = bridge.night_light
        # While no music plays the preview runs on its own engine with the test beat (or
        # shows the night light); during music it is rendered by the bridge in lockstep.
        self._preview_settings = bridge.engine.settings
        self._idle_engine: Engine | None = None

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
        """
        Refuse requests that may come from another web site.

        Behind Ingress only the Supervisor may connect (Home Assistant does the login).
        Standalone, the page has no login, so other sites open in the same browser must
        not drive it: the Host must be an address or this machine's name (no DNS
        rebinding), a cross-site Origin is refused (also for the websocket), and Apply
        only takes JSON, which a plain cross-site form cannot send.
        """
        if self._allowed_remote is not None:
            if request.remote != self._allowed_remote:
                LOGGER.warning("Refused web UI request from %s", request.remote)
                raise web.HTTPForbidden
            return await handler(request)
        if not _is_local_host(request.host):
            raise web.HTTPForbidden(text="Unknown host name")
        origin = request.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc.lower() != request.host.lower():
            raise web.HTTPForbidden(text="Cross-site request")
        if request.method == "POST" and request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Send JSON")
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
                "effects": [
                    {"id": e.value, "label": label, "description": description}
                    for e, (label, description) in EFFECT_LABELS.items()
                ],
                "palettes": self._palettes(),
                "album_modes": self._album_modes(),
                "responses": [
                    {"id": r.value, "label": label} for r, label in RESPONSE_LABELS.items()
                ],
                "ranges": {
                    "brightness": BRIGHTNESS_RANGE,
                    "sensitivity": SENSITIVITY_RANGE,
                    "latency_ms": LATENCY_RANGE,
                    "night_light": NIGHT_LIGHT_RANGE,
                    "palette_colors": PALETTE_COLORS_RANGE,
                    "my_palettes": (0, MAX_MY_PALETTES),
                },
                **self._status(),
                "saves": self._saver is not None,
            }
        )

    async def _apply(self, request: web.Request) -> web.Response:
        try:
            settings = parse_settings(await request.json())
        except (ValueError, json.JSONDecodeError) as err:
            return web.json_response({"error": str(err)}, status=400)
        self._bridge.apply(settings.engine, settings.latency_ms, settings.night_light)
        self._my_palettes = settings.my_palettes
        self._set_preview(settings.engine)
        self._preview_night_light = settings.night_light
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
        self._set_preview(self._preview_settings)
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
                self._idle_engine = None
        return ws

    async def _on_message(self, ws: web.WebSocketResponse, text: str) -> None:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("type") != "preview":
                return
            settings = parse_settings(data.get("settings"))
        except (ValueError, json.JSONDecodeError) as err:
            await ws.send_json({"type": "error", "message": str(err)})
            return
        self._set_preview(settings.engine)
        self._preview_night_light = settings.night_light
        if data.get("mode") in ("music", "idle"):
            self._preview_mode = data["mode"]

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
        told: set[web.WebSocketResponse] = set()
        while self._clients:
            await asyncio.sleep(period)
            now = time.perf_counter()
            dt, last = now - last, now
            idle = self._idle()
            idle.set_album_colors(self._bridge.engine.album_colors)
            if self._preview_mode == "idle":
                # Without a night light the LEDs go back to HyperHDR: shown as dark.
                preview_rgb = (
                    idle.render_night_light(dt, self._preview_night_light)
                    if self._preview_night_light > 0.0
                    else idle.render_black()
                )
            elif self._bridge.streaming:
                # Rendered by the bridge in the same step as the frame on the LEDs.
                preview_rgb = self._bridge.last_preview_rgb
            else:
                loudness, spectrum, beat = self._demo(now - start)
                idle.feed(
                    loudness=loudness,
                    spectrum=spectrum,
                    onset=200 if beat != last_beat else None,
                )
                last_beat = beat
                preview_rgb = idle.render(dt)
            # Read both at once, so the two pictures show the same moment.
            frame = self._bridge.last_rgb + preview_rgb
            current = {"type": "status", **self._status()}
            current["palettes"] = self._palettes()
            current["album_modes"] = self._album_modes()
            if current != status:
                told.clear()
                status = current
            for ws in list(self._clients):
                with contextlib.suppress(ConnectionResetError, RuntimeError):
                    # Every page hears the status once, also one that opened later.
                    if ws not in told:
                        await ws.send_json(current)
                        told.add(ws)
                    await ws.send_bytes(frame)
            told &= self._clients

    # --------------------------------------------------------------- helpers

    def _status(self) -> dict[str, Any]:
        """Return whether music plays, where it comes from and the player's name."""
        bridge = self._bridge
        return {"playing": bridge.streaming, "source": bridge.source, "player": bridge.name}

    def _set_preview(self, settings: EngineSettings) -> None:
        self._preview_settings = settings
        self._bridge.set_preview(settings)
        if self._idle_engine is not None:
            self._idle_engine.update_settings(settings)

    def _idle(self) -> Engine:
        """Return the engine that renders the preview while no music plays."""
        if self._idle_engine is None:
            self._idle_engine = Engine(self._bridge.engine.layout, self._preview_settings)
        return self._idle_engine

    def _current(self) -> dict[str, Any]:
        bridge = self._bridge
        return settings_to_dict(
            UiSettings(
                bridge.engine.settings, bridge.latency_ms, bridge.night_light, self._my_palettes
            )
        )

    def _album_modes(self) -> list[dict[str, Any]]:
        """Return the artwork modes with the colours each takes from the current cover."""
        album = self._bridge.engine.album_colors
        if album != self._album_key:
            self._album_key = album
            self._album_swatches = [
                {
                    "id": mode.value,
                    "label": label,
                    "description": description,
                    "colors": [
                        hex_color(c) for c in palette_swatch(Palette.ALBUM, album, album_mode=mode)
                    ],
                }
                for mode, (label, description) in ALBUM_MODE_LABELS.items()
            ]
        return self._album_swatches

    def _palettes(self) -> list[dict[str, Any]]:
        """Return the palettes with sample colours; the album one follows the artwork."""
        album = self._bridge.engine.album_colors
        engine_settings = self._bridge.engine.settings
        color = engine_settings.color
        key = (album, color, engine_settings.album_mode)
        if key != self._swatch_key:
            self._swatch_key = key
            self._swatches = [
                {
                    "id": palette.value,
                    "label": label,
                    "colors": [
                        hex_color(c)
                        for c in palette_swatch(
                            palette, album, color=color, album_mode=engine_settings.album_mode
                        )
                    ],
                }
                for palette, label in PALETTE_LABELS.items()
            ]
        return self._swatches


def _is_local_host(host: str) -> bool:
    """
    Whether a Host header names this machine: an IP address, localhost or its own name.

    :param host: The Host header, optionally with a port.
    """
    name = urlsplit(f"//{host}").hostname or ""
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(name)
        return True
    own = socket.gethostname().lower().split(".")[0]
    return name in {"localhost", own, f"{own}.local"} or name.endswith(".localhost")


# ------------------------------------------------------------------ Supervisor


def _supervisor_headers() -> dict[str, str]:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise ValueError("SUPERVISOR_TOKEN is not set, so the Supervisor cannot be asked")
    return {"Authorization": f"Bearer {token}"}


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
    my_palettes: tuple[MyPalette, ...] = ()
    if supervisor:
        info = await supervisor_info(session)
        # A host-network add-on is reached on the gateway of Home Assistant's internal
        # network, so the panel is not exposed on the LAN; the Supervisor itself sits
        # at the next address and is the only client that may connect.
        gateway = ipaddress.ip_address(str(info["ip_address"]))
        host, port = str(gateway), int(info["ingress_port"])
        allowed_remote = str(gateway + 1)
        saver = supervisor_saver(session)
        try:
            my_palettes = _parse_my_palettes(info.get("options", {}).get("my_palettes", []))
        except ValueError as err:
            LOGGER.warning("Ignoring the saved palettes: %s", err)
    runner = web.AppRunner(
        WebUi(
            bridge, demo, saver=saver, allowed_remote=allowed_remote, my_palettes=my_palettes
        ).make_app(),
        access_log=None,
    )
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    LOGGER.info("Web UI listening on http://%s:%d", host, port)
    return runner
