"""Setup flow for the HyperHDR Sync provider: asks where HyperHDR runs and checks it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .constants import CONF_HOST, CONF_LED_LAYOUT, CONF_TOKEN, CONF_UDP_PORT, CONF_WEB_PORT
from .hyperhdr import (
    DEFAULT_UDP_PORT,
    DEFAULT_WEB_PORT,
    MAX_LEDS,
    HyperHdrAuthError,
    HyperHdrError,
    fetch_serverinfo,
)
from .layout import LedLayout

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the setup flow: ask for HyperHDR's address and read its LED layout.

    :param session: The setup session driving the flow.
    """
    previous = session.context.setup_data
    host = str(previous.get(CONF_HOST) or "")
    web_port = int(previous.get(CONF_WEB_PORT) or DEFAULT_WEB_PORT)
    udp_port = int(previous.get(CONF_UDP_PORT) or DEFAULT_UDP_PORT)
    token = str(previous.get(CONF_TOKEN) or "")
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_HOST,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=host or None,
                ),
                ConfigEntry(
                    key=CONF_UDP_PORT,
                    type=ConfigEntryType.INTEGER,
                    default_value=DEFAULT_UDP_PORT,
                    value=udp_port,
                    range=(1024, 65535),
                ),
                ConfigEntry(
                    key=CONF_WEB_PORT,
                    type=ConfigEntryType.INTEGER,
                    default_value=DEFAULT_WEB_PORT,
                    value=web_port,
                    range=(1, 65535),
                ),
                ConfigEntry(
                    key=CONF_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    required=False,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        host = str(values.get(CONF_HOST) or "").strip()
        web_port = int(values.get(CONF_WEB_PORT) or DEFAULT_WEB_PORT)  # type: ignore[arg-type]
        udp_port = int(values.get(CONF_UDP_PORT) or DEFAULT_UDP_PORT)  # type: ignore[arg-type]
        # A secret is never sent back to the form, so an empty field keeps the stored token.
        token = str(values.get(CONF_TOKEN) or token).strip()
        try:
            info = await fetch_serverinfo(session.mass.http_session, host, web_port, token or None)
            layout = LedLayout.from_serverinfo(info.get("leds") or [])
        except HyperHdrAuthError:
            errors = {CONF_TOKEN: "auth_required"}
            continue
        except HyperHdrError:
            errors = {"base": "cannot_connect"}
            continue
        except ValueError:
            errors = {"base": "no_leds"}
            continue
        if layout.count > MAX_LEDS:
            errors = {"base": "too_many_leds"}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_HOST: host,
            CONF_WEB_PORT: web_port,
            CONF_UDP_PORT: udp_port,
            CONF_TOKEN: token,
            CONF_LED_LAYOUT: layout.to_list(),
        }
        try:
            await session.finish(collected)
        except SetupFlowError as err:
            errors = {"base": err}
        else:
            return
