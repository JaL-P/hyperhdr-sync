"""HyperHDR Sync provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.plugin import PluginProvider

from .bridge import HyperHdrBridge
from .constants import (
    CONF_ALBUM_MODE,
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_EFFECT,
    CONF_HOST,
    CONF_LATENCY_MS,
    CONF_LED_LAYOUT,
    CONF_PALETTE,
    CONF_RESPONSE,
    CONF_SENSITIVITY_DB,
    CONF_TOKEN,
    CONF_UDP_PORT,
    CONF_WEB_PORT,
    DEFAULT_BRIGHTNESS,
    DEFAULT_LATENCY_MS,
    DEFAULT_SENSITIVITY_DB,
    LAYOUT_REFRESH_INTERVAL_S,
)
from .engine import AlbumMode, Effect, Palette, Response
from .hyperhdr import (
    DEFAULT_UDP_PORT,
    DEFAULT_WEB_PORT,
    MAX_LEDS,
    HyperHdrError,
    HyperHdrUdpSender,
    fetch_serverinfo,
)
from .layout import LedLayout
from .settings import (
    DEFAULT_ALBUM_MODE,
    DEFAULT_COLOR_HEX,
    DEFAULT_EFFECT,
    DEFAULT_PALETTE,
    DEFAULT_RESPONSE,
    get_settings,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.providers.sendspin.provider import SendspinProvider

_SETTINGS_KEYS = (
    CONF_ALBUM_MODE,
    CONF_COLOR,
    CONF_EFFECT,
    CONF_PALETTE,
    CONF_BRIGHTNESS,
    CONF_SENSITIVITY_DB,
    CONF_RESPONSE,
    CONF_LATENCY_MS,
)
# Choices this plugin does not offer: MilkDrop needs pictures from a headless Chromium,
# which only the standalone bridge and the add-on run; the user makes their own palette
# in the web UI.
_UNOFFERED_EFFECTS = frozenset({Effect.MILKDROP})
_UNOFFERED_PALETTES = frozenset({Palette.MINE, Palette.MILKDROP})
# changed_keys arrive namespaced as 'values/<key>'.
_IMMEDIATE_APPLY_KEYS = {f"values/{key}" for key in _SETTINGS_KEYS}


class HyperHdrProvider(PluginProvider):
    """Provider that makes the LEDs of one HyperHDR instance react to MA playback."""

    _bridge: HyperHdrBridge | None = None
    _last_layout_refresh_s: float = 0.0

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the playback settings; the address of HyperHDR is asked by the setup flow."""
        return (
            ConfigEntry(
                key=CONF_EFFECT,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_EFFECT.value,
                options=[
                    ConfigValueOption(effect.value)
                    for effect in Effect
                    if effect not in _UNOFFERED_EFFECTS
                ],
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_PALETTE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_PALETTE.value,
                options=[
                    ConfigValueOption(palette.value)
                    for palette in Palette
                    if palette not in _UNOFFERED_PALETTES
                ],
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_BRIGHTNESS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_BRIGHTNESS,
                range=(0, 100),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_SENSITIVITY_DB,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SENSITIVITY_DB,
                range=(-12, 12),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_RESPONSE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_RESPONSE.value,
                options=[ConfigValueOption(response.value) for response in Response],
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_LATENCY_MS,
                range=(0, 2000),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_ALBUM_MODE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_ALBUM_MODE.value,
                options=[ConfigValueOption(mode.value) for mode in AlbumMode],
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_COLOR,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_COLOR_HEX,
                immediate_apply=True,
                category="settings",
            ),
        )

    async def handle_async_init(self) -> None:
        """Read the LED layout and start the light player."""
        host, udp_port = self._host, self._udp_port
        if not host:
            raise SetupFailedError(
                "No HyperHDR address configured",
                translation_key="cannot_connect",
                translation_owner=f"provider.{self.domain}",
            )
        for sibling in await self.mass.config.get_provider_configs(provider_domain=self.domain):
            if sibling.instance_id == self.instance_id:
                continue
            setup_value = self.mass.config.get_provider_setup_value
            if (
                setup_value(sibling.instance_id, CONF_HOST) == host
                and setup_value(sibling.instance_id, CONF_UDP_PORT) == udp_port
            ):
                raise SetupFailedError(
                    f"HyperHDR at {host}:{udp_port} is already set up",
                    translation_key="already_configured",
                    translation_owner=f"provider.{self.domain}",
                )
        layout = await self._load_layout()
        sender = HyperHdrUdpSender(host, udp_port)
        try:
            await sender.open()
        except HyperHdrError as err:
            raise SetupFailedError(
                str(err),
                translation_key="cannot_connect",
                translation_owner=f"provider.{self.domain}",
            ) from err
        sendspin_provider = cast("SendspinProvider", self.mass.get_provider("sendspin"))
        bridge = HyperHdrBridge(self, sendspin_provider, sender, layout, get_settings(self.config))
        bridge.start()
        self._bridge = bridge

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002
        """Handle unload/close of the provider."""
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Apply changed playback settings in place."""
        if self._bridge and changed_keys and changed_keys <= _IMMEDIATE_APPLY_KEYS:
            self._bridge.update_settings(get_settings(config))
            self.config = config
            return
        await super().update_config(config, changed_keys)

    def refresh_layout_soon(self) -> None:
        """Read the LED layout from HyperHDR again in the background, at most once a minute."""
        now_s = self.mass.loop.time()
        if now_s - self._last_layout_refresh_s < LAYOUT_REFRESH_INTERVAL_S:
            return
        self._last_layout_refresh_s = now_s
        self.mass.create_task(self._refresh_layout())

    # ----------------------------------------------------------------- layout

    @property
    def _host(self) -> str:
        return str(self.get_setup_value(CONF_HOST) or "").strip()

    @property
    def _udp_port(self) -> int:
        return int(cast("int", self.get_setup_value(CONF_UDP_PORT, DEFAULT_UDP_PORT)))

    async def _fetch_layout(self) -> LedLayout:
        """Read the LED layout from HyperHDR's web API."""
        web_port = int(cast("int", self.get_setup_value(CONF_WEB_PORT, DEFAULT_WEB_PORT)))
        token = str(self.get_setup_value(CONF_TOKEN) or "") or None
        info = await fetch_serverinfo(self.mass.http_session, self._host, web_port, token)
        try:
            layout = LedLayout.from_serverinfo(info.get("leds") or [])
        except (KeyError, TypeError, ValueError) as err:
            raise HyperHdrError(f"HyperHDR reported an unusable LED layout: {err}") from err
        if layout.count > MAX_LEDS:
            raise HyperHdrError(
                f"HyperHDR has {layout.count} LEDs, at most {MAX_LEDS} can be driven"
            )
        return layout

    async def _load_layout(self) -> LedLayout:
        """Return the LED layout, from HyperHDR or else the copy kept by the setup flow."""
        self._last_layout_refresh_s = self.mass.loop.time()
        try:
            layout = await self._fetch_layout()
        except HyperHdrError as err:
            stored = self.get_setup_value(CONF_LED_LAYOUT)
            if isinstance(stored, list) and stored:
                # HyperHDR often runs on a PC that is switched off while the server starts.
                self.logger.info("Using the stored LED layout, HyperHDR did not answer: %s", err)
                return LedLayout.from_list([float(value) for value in stored])
            raise SetupFailedError(
                str(err),
                translation_key="cannot_connect",
                translation_owner=f"provider.{self.domain}",
            ) from err
        self._store_layout(layout)
        return layout

    async def _refresh_layout(self) -> None:
        """Pick up a changed LED layout while running."""
        try:
            layout = await self._fetch_layout()
        except HyperHdrError as err:
            self.logger.debug("Could not refresh the LED layout: %s", err)
            return
        if self._bridge and self._store_layout(layout):
            self.logger.info("LED layout changed, now %d LEDs", layout.count)
            self._bridge.set_layout(layout)

    def _store_layout(self, layout: LedLayout) -> bool:
        """
        Keep a copy of the layout in the setup data.

        :param layout: The layout read from HyperHDR.
        :return: Whether it differs from the stored copy.
        """
        values = layout.to_list()
        if self.get_setup_value(CONF_LED_LAYOUT) == values:
            return False
        self._update_setup_data(CONF_LED_LAYOUT, values, immediate=False)
        return True
