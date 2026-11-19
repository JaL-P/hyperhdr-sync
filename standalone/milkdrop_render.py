"""
The real MilkDrop presets, drawn by Butterchurn in a headless Chromium.

Music Assistant draws its MilkDrop visualizer in the browser with Butterchurn; the
server never makes pictures. So the bridge runs Butterchurn itself: a page in its own
headless Chromium (``milkdrop_page.html``) renders the presets from the waveform the
bridge sends each frame and sends the small picture back, and the MilkDrop effect samples
it at the LEDs, the way HyperHDR's grabber samples the edges of a screen.

Chromium starts the first time a picture is asked for and stops after a minute without
requests, so it costs nothing while another effect runs. The page is served on
127.0.0.1 under a random path, so nothing else on the machine can drive it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

LOGGER = logging.getLogger("hyperhdr_bridge.milkdrop")

HERE = Path(__file__).resolve().parent
PAGE = HERE / "milkdrop_page.html"
VENDOR = HERE / "vendor"
VENDOR_FILES = ("butterchurn.min.js", "butterchurn-presets.js")
CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
# The picture Butterchurn draws: small, as only its edges reach the LEDs, and 16:9.
WIDTH, HEIGHT = 128, 72
LIVE, PREVIEW = 0, 1
SLOTS = 2
# A random preset changes this often, blending over this long, as in Music Assistant.
CYCLE_S = 30.0
BLEND_S = 2.7
# A picture older than this is not shown (the renderer stalled or restarted).
FRESH_S = 0.5
IDLE_STOP_S = 60.0
RESTART_S = 5.0
WAVE_SAMPLES = 1024


def find_chromium(path: str | None = None) -> str | None:
    """
    Return the Chromium to run: ``path`` if given, else the first one on the PATH.

    :param path: A Chromium executable chosen by the user.
    """
    if path:
        return path if Path(path).is_file() else shutil.which(path)
    return next((found for name in CHROMIUM_NAMES if (found := shutil.which(name))), None)


class _Slot:
    """What one visualizer of the page shows: its preset choice and latest picture."""

    def __init__(self) -> None:
        self.wanted = ""  # a preset name; empty means a random one every CYCLE_S
        self.loaded: str | None = None
        self.changed_at = 0.0
        self.picture: np.ndarray | None = None
        self.picture_at = 0.0


class MilkdropRenderer:
    """Runs the Butterchurn page in a headless Chromium and trades waveforms for pictures."""

    def __init__(self, chromium: str | None = None, vendor: Path = VENDOR) -> None:
        """
        Initialize the renderer; nothing starts until a picture is asked for.

        :param chromium: The Chromium executable; none looks for one on the PATH.
        :param vendor: The folder ``fetch_butterchurn.py`` filled.
        """
        self._chromium = find_chromium(chromium)
        self._vendor = vendor
        self._secret = secrets.token_urlsafe(16)
        self._slots = [_Slot() for _ in range(SLOTS)]
        self._ws: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._task: asyncio.Task[None] | None = None
        self._asked_at = 0.0
        self._rng = random.Random()  # noqa: S311 - picks presets, nothing secret
        self.presets: list[str] = []

    @property
    def unavailable_reason(self) -> str | None:
        """Why MilkDrop cannot run here, or None when it can."""
        if self._chromium is None:
            return "MilkDrop needs Chromium; install it or give --chromium"
        missing = [name for name in VENDOR_FILES if not (self._vendor / name).is_file()]
        if missing:
            return "MilkDrop needs Butterchurn: run python standalone/fetch_butterchurn.py"
        return None

    @property
    def running(self) -> bool:
        """Whether the page is connected and drawing."""
        return self._ws is not None and not self._ws.closed

    def set_preset(self, slot: int, name: str) -> None:
        """
        Choose a slot's preset.

        :param slot: ``LIVE`` or ``PREVIEW``.
        :param name: A preset name, or empty for a random one every 30 seconds.
        """
        self._slots[slot].wanted = name

    def now_showing(self, slot: int) -> str | None:
        """Return the name of the preset a slot shows, once the page has loaded one."""
        return self._slots[slot].loaded

    def picture(self, slot: int) -> np.ndarray | None:
        """Return a slot's latest picture (rows of R G B, 0..1), or None if it is not fresh."""
        state = self._slots[slot]
        if state.picture is None or time.monotonic() - state.picture_at > FRESH_S:
            return None
        return state.picture

    async def render(self, slot: int, wave: bytes) -> None:
        """
        Ask for the next picture of a slot; it arrives a moment later (see ``picture``).

        Starts Chromium on the first call.

        :param slot: ``LIVE`` or ``PREVIEW``.
        :param wave: 1024 waveform samples, unsigned bytes with 128 as zero.
        """
        self._asked_at = time.monotonic()
        if self._task is None or self._task.done():
            if self.unavailable_reason is not None:
                return
            self._task = asyncio.create_task(self._keep_running())
        ws = self._ws
        if ws is None or ws.closed or not self.presets:
            return
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await self._update_preset(ws, slot)
            await ws.send_bytes(bytes([slot]) + wave[:WAVE_SAMPLES])

    async def close(self) -> None:
        """Stop Chromium and the page's server."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -------------------------------------------------------------- internals

    async def _update_preset(self, ws: web.WebSocketResponse, slot: int) -> None:
        state = self._slots[slot]
        now = time.monotonic()
        if state.wanted and state.wanted in self.presets:
            name = state.wanted
        elif state.loaded is None or state.wanted or now - state.changed_at >= CYCLE_S:
            choices = [p for p in self.presets if p != state.loaded] or self.presets
            name = self._rng.choice(choices)
        else:
            return
        if name == state.loaded:
            return
        blend = 0.0 if state.loaded is None else BLEND_S
        await ws.send_str(
            json.dumps({"type": "preset", "slot": slot, "name": name, "blend": blend})
        )
        state.loaded = name
        state.changed_at = now

    async def _keep_running(self) -> None:
        """Serve the page and keep Chromium running while pictures are asked for."""
        app = web.Application()
        base = f"/{self._secret}/"
        app.router.add_get(base, self._page)
        app.router.add_get(base + "ws", self._websocket)
        app.router.add_static(base + "vendor/", self._vendor)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        url = f"http://127.0.0.1:{port}{base}?w={WIDTH}&h={HEIGHT}"
        try:
            while time.monotonic() - self._asked_at < IDLE_STOP_S:
                await self._run_chromium(url)
                await asyncio.sleep(RESTART_S)
            LOGGER.info("MilkDrop not in use for a minute, stopping Chromium")
        finally:
            await self._runner.cleanup()
            self._runner = None
            self._ws = None
            for state in self._slots:
                state.loaded = None
                state.picture = None

    async def _run_chromium(self, url: str) -> None:
        """Run Chromium on the page until it exits, or until MilkDrop is no longer used."""
        assert self._chromium is not None
        with tempfile.TemporaryDirectory(prefix="hyperhdr-milkdrop-") as profile:
            try:
                process = await self._start_chromium(url, profile)
            except OSError:
                LOGGER.exception("Cannot start Chromium for MilkDrop")
                return
            LOGGER.info("Started Chromium for the MilkDrop presets")
            try:
                while process.returncode is None:
                    if time.monotonic() - self._asked_at >= IDLE_STOP_S:
                        break
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), 1.0)
                if process.returncode is not None:
                    LOGGER.warning("Chromium exited (%s), restarting it", process.returncode)
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5.0)
                    except TimeoutError:
                        process.kill()
                        await process.wait()

    async def _start_chromium(self, url: str, profile: str) -> asyncio.subprocess.Process:
        """Start Chromium on the page, headless, with a throwaway profile."""
        assert self._chromium is not None
        return await asyncio.create_subprocess_exec(
            self._chromium,
            "--headless=new",
            # The bridge often runs as root in a container, where Chromium's sandbox
            # cannot start; the page is the bridge's own, served on localhost.
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--no-first-run",
            "--disable-extensions",
            "--disable-background-networking",
            # There is no GPU in a container: WebGL runs on Chromium's software renderer.
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
            f"--user-data-dir={profile}",
            url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _page(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = ws
        for state in self._slots:
            state.loaded = None
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                self._on_picture(message.data)
            elif message.type == WSMsgType.TEXT:
                with contextlib.suppress(ValueError, TypeError):
                    data = json.loads(message.data)
                    if isinstance(data, dict) and data.get("type") == "hello":
                        self.presets = sorted(str(name) for name in data.get("presets", []))
                        LOGGER.info("MilkDrop ready with %d presets", len(self.presets))
        if self._ws is ws:
            self._ws = None
        return ws

    def _on_picture(self, data: bytes) -> None:
        if len(data) != 1 + WIDTH * HEIGHT * 3 or data[0] >= SLOTS:
            return
        state = self._slots[data[0]]
        pixels = np.frombuffer(data, np.uint8, offset=1).reshape(HEIGHT, WIDTH, 3)
        state.picture = pixels.astype(np.float32) / 255.0
        state.picture_at = time.monotonic()
