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
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
from aiohttp import WSMsgType, web

from hyperhdr_sync.engine import WAVE_SAMPLES

LOGGER = logging.getLogger("hyperhdr_bridge.milkdrop")

HERE = Path(__file__).resolve().parent
PAGE = HERE / "milkdrop_page.html"
VENDOR = HERE / "vendor"
VENDOR_FILES = (
    "butterchurn.min.js",
    "butterchurn-presets.min.js",
    "butterchurn-presets-extra.min.js",
)
CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
# The picture Butterchurn draws: small, as only its edges reach the LEDs, and 16:9.
WIDTH, HEIGHT = 128, 72
LIVE, PREVIEW = 0, 1
SLOTS = 2
# How long a preset change blends, as in Music Assistant's visualizer.
BLEND_S = 2.7
# A picture older than this is not shown (the renderer stalled or restarted).
FRESH_S = 0.5
IDLE_STOP_S = 60.0
RESTART_S = 5.0
# While pictures are asked for, a page silent this long (gone, or broken by a script error)
# gets Chromium restarted; a request this recent counts as being asked.
PAGE_TIMEOUT_S = 15.0
ASKING_S = 2.0
# An unanswered request blocks the next one this long, so a slow page is not flooded.
IN_FLIGHT_S = 0.25


def find_chromium(path: str | None = None) -> str | None:
    """
    Return the Chromium to run: ``path`` if given, else the first one on the PATH.

    :param path: A Chromium executable chosen by the user.
    """
    if path:
        return path if Path(path).is_file() else shutil.which(path)
    return next((found for name in CHROMIUM_NAMES if (found := shutil.which(name))), None)


@dataclass(frozen=True)
class VisualizerPrefs:
    """
    Music Assistant's visualizer settings.

    Its web interface keeps them in the user's preferences; the defaults are its own.
    """

    mode: str = "random"
    """``random``, ``random_favorites`` or ``fixed``."""
    preset: str = ""
    """The preset of the ``fixed`` mode."""
    favorites: tuple[str, ...] = ()
    """The presets starred in Music Assistant."""
    beat_switch: bool = False
    """Change to another preset on a downbeat once ``beat_dwell_s`` have passed."""
    beat_dwell_s: float = 30.0
    from_music_assistant: bool = False
    """Whether these were read from Music Assistant (else they are its defaults)."""

    @classmethod
    def from_preferences(cls, preferences: dict[str, Any]) -> VisualizerPrefs:
        """
        Read the visualizer settings from a Music Assistant user's preferences.

        :param preferences: The ``preferences`` of Music Assistant's ``/auth/me``.
        """
        mode = str(preferences.get("visualizer_preset_mode") or "random")
        favorites = preferences.get("visualizer_favorites") or []
        try:
            dwell = float(preferences.get("visualizer_beat_dwell", 30))
        except TypeError, ValueError:
            dwell = 30.0
        return cls(
            mode=mode if mode in ("random", "random_favorites", "fixed") else "random",
            preset=str(preferences.get("visualizer_preset") or ""),
            favorites=tuple(str(f) for f in favorites if isinstance(f, str)),
            beat_switch=bool(preferences.get("visualizer_beat_switch", False)),
            beat_dwell_s=max(1.0, dwell),
            from_music_assistant=True,
        )

    def describe(self) -> str:
        """Say in a few words how presets are picked."""
        if self.mode == "fixed" and self.preset:
            text = f"always {self.preset}"
        elif self.mode == "random_favorites" and self.favorites:
            text = f"a random one of your {len(self.favorites)} favourites"
        else:
            text = "a random preset"
        if self.beat_switch:
            text += f", changing on the beat every {self.beat_dwell_s:.0f} s or more"
        return text


class _Slot:
    """What one visualizer of the page shows: its preset choice and latest picture."""

    def __init__(self) -> None:
        self.wanted = ""  # a preset name; empty follows Music Assistant's settings
        self.loaded: str | None = None
        self.choice_key: tuple[object, ...] | None = None
        self.changed_at = 0.0
        self.repick = False
        self.beat_due = False
        self.picture: np.ndarray | None = None
        self.picture_at = 0.0
        self.asked_at = 0.0  # when the request still unanswered was sent; 0 when none is


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
        self.ma_prefs = VisualizerPrefs()
        self._page_seen_at = 0.0
        self._unknown_warned: set[str] = set()

    @cached_property
    def unavailable_reason(self) -> str | None:
        """Why MilkDrop cannot run here, or None when it can (checked once, at first use)."""
        if self._chromium is None:
            return "MilkDrop needs Chromium; install it or give --chromium"
        missing = [name for name in VENDOR_FILES if not (self._vendor / name).is_file()]
        if missing:
            return "MilkDrop needs Butterchurn: run python standalone/fetch_butterchurn.py"
        return None

    def set_preset(self, slot: int, name: str) -> None:
        """
        Choose a slot's preset.

        :param slot: ``LIVE`` or ``PREVIEW``.
        :param name: A preset name, or empty to pick them as Music Assistant does.
        """
        self._slots[slot].wanted = name

    def set_ma_prefs(self, prefs: VisualizerPrefs) -> None:
        """
        Follow Music Assistant's visualizer settings.

        :param prefs: The settings, read from Music Assistant.
        """
        self.ma_prefs = prefs

    def repick(self) -> None:
        """Pick presets afresh, as Music Assistant does when its visualizer opens."""
        for state in self._slots:
            state.repick = True

    def downbeat(self) -> None:
        """Note a downbeat: with beat switch on, Music Assistant changes preset on one."""
        for state in self._slots:
            state.beat_due = True

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
        state = self._slots[slot]
        if ws is None or ws.closed or not self.presets:
            return
        if state.asked_at and self._asked_at - state.asked_at < IN_FLIGHT_S:
            return  # the page is still drawing the last one: skip this frame
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await self._update_preset(ws, slot)
            await ws.send_bytes(bytes([slot]) + wave[:WAVE_SAMPLES])
            state.asked_at = self._asked_at

    async def close(self) -> None:
        """Stop Chromium and the page's server."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -------------------------------------------------------------- internals

    async def _update_preset(self, ws: web.WebSocketResponse, slot: int) -> None:
        """
        Load the preset a slot should show now, picked the way Music Assistant picks it.

        :param ws: The page's connection.
        :param slot: ``LIVE`` or ``PREVIEW``.
        """
        state = self._slots[slot]
        now = time.monotonic()
        prefs = self.ma_prefs
        key = (state.wanted, prefs.mode, prefs.preset, prefs.favorites)
        beat_due, state.beat_due = state.beat_due, False
        if state.loaded is None or key != state.choice_key or state.repick:
            name = self._choose(state.wanted, prefs, state.loaded, forced=False)
        elif (
            beat_due
            and prefs.beat_switch
            and not state.wanted
            and now - state.changed_at >= prefs.beat_dwell_s
        ):
            # Beat switch skips the fixed preset, as in Music Assistant.
            name = self._choose("", prefs, state.loaded, forced=True)
        else:
            return
        state.choice_key = key
        state.repick = False
        if name is None or name == state.loaded:
            return
        blend = 0.0 if state.loaded is None else BLEND_S
        await ws.send_str(
            json.dumps({"type": "preset", "slot": slot, "name": name, "blend": blend})
        )
        state.loaded = name
        state.changed_at = now

    def _choose(
        self, wanted: str, prefs: VisualizerPrefs, current: str | None, *, forced: bool
    ) -> str | None:
        """
        Pick a preset: the one asked for, else as Music Assistant's settings say.

        :param wanted: The preset chosen for this slot; empty follows Music Assistant.
        :param prefs: Music Assistant's visualizer settings.
        :param current: The preset shown now, avoided by a random pick.
        :param forced: A beat switch: skip Music Assistant's fixed preset, as it does.
        :return: The preset, or None when there are none.
        """
        if wanted in self.presets:
            return wanted
        if wanted and wanted not in self._unknown_warned:
            self._unknown_warned.add(wanted)
            LOGGER.warning(
                "MilkDrop preset %r is not one of Music Assistant's; following Music Assistant",
                wanted,
            )
        if not forced and prefs.mode == "fixed" and prefs.preset in self.presets:
            return prefs.preset
        pool = self.presets
        if prefs.mode == "random_favorites":
            pool = [name for name in prefs.favorites if name in self.presets] or pool
        choices = [name for name in pool if name != current] or pool
        return self._rng.choice(choices) if choices else None

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
        """
        Run Chromium on the page until it exits, its page goes silent, or nothing uses it.

        :param url: The page's address.
        """
        assert self._chromium is not None
        # A Chromium profile holds many files: make and remove it off the event loop.
        profile = await asyncio.to_thread(tempfile.mkdtemp, prefix="hyperhdr-milkdrop-")
        try:
            await self._run_chromium_in(url, profile)
        finally:
            await asyncio.to_thread(shutil.rmtree, profile, ignore_errors=True)

    async def _run_chromium_in(self, url: str, profile: str) -> None:
        """
        Start Chromium with ``profile`` and watch it until it should stop.

        :param url: The page's address.
        :param profile: A fresh folder for Chromium's profile.
        """
        try:
            process = await self._start_chromium(url, profile)
        except OSError:
            LOGGER.exception("Cannot start Chromium for MilkDrop")
            return
        LOGGER.info("Started Chromium for the MilkDrop presets")
        started = time.monotonic()
        try:
            while process.returncode is None:
                now = time.monotonic()
                if now - self._asked_at >= IDLE_STOP_S:
                    break
                silent = now - max(started, self._page_seen_at)
                if now - self._asked_at < ASKING_S and silent > PAGE_TIMEOUT_S:
                    LOGGER.warning("The MilkDrop page stopped answering, restarting Chromium")
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
            state.asked_at = 0.0
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                self._on_picture(message.data)
            elif message.type == WSMsgType.TEXT:
                with contextlib.suppress(ValueError, TypeError):
                    data = json.loads(message.data)
                    if isinstance(data, dict) and data.get("type") == "hello":
                        self.presets = sorted(str(name) for name in data.get("presets", []))
                        self._page_seen_at = time.monotonic()
                        LOGGER.info("MilkDrop ready with %d presets", len(self.presets))
        if self._ws is ws:
            self._ws = None
        return ws

    def _on_picture(self, data: bytes) -> None:
        """Take a picture from the page: ``[slot][R G B A per pixel, row by row]``."""
        if len(data) != 1 + WIDTH * HEIGHT * 4 or data[0] >= SLOTS:
            return
        state = self._slots[data[0]]
        pixels = np.frombuffer(data, np.uint8, offset=1).reshape(HEIGHT, WIDTH, 4)
        state.picture = pixels[..., :3].astype(np.float32) / 255.0
        state.picture_at = self._page_seen_at = time.monotonic()
        state.asked_at = 0.0
