"""
Minimal HyperHDR client: read the LED layout, then stream raw LED colours.

Two HyperHDR services are used:

* The JSON API over HTTP (``POST /json-rpc`` on the web port, default 8090) is asked
  once for ``serverinfo`` to learn how many LEDs there are and where they sit.
* The "UDP raw receiver" (default port 5568) takes one datagram per frame holding
  ``R G B`` bytes for every LED in order. It must be switched on in HyperHDR under
  Network Services. HyperHDR shows the stream on that receiver's priority channel
  and drops it by itself 3 seconds after the last datagram, so the screen grabber
  (or whatever else is active) simply takes over again when the music stops.

Only aiohttp and the standard library are needed, so this works both inside Music
Assistant and in the standalone bridge.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

DEFAULT_WEB_PORT: int = 8090
DEFAULT_UDP_PORT: int = 5568

# HyperHDR's raw receiver ignores datagrams above 1500 bytes (RawUdpServer.cpp).
MAX_UDP_PAYLOAD: int = 1500
MAX_LEDS: int = MAX_UDP_PAYLOAD // 3

_REQUEST_TIMEOUT_S: float = 6.0


class HyperHdrError(Exception):
    """HyperHDR could not be reached or refused the request."""


class HyperHdrAuthError(HyperHdrError):
    """HyperHDR wants an API token (or rejected the one given)."""


async def fetch_serverinfo(
    session: aiohttp.ClientSession,
    host: str,
    web_port: int = DEFAULT_WEB_PORT,
    token: str | None = None,
) -> dict[str, Any]:
    """
    Return the ``info`` object of HyperHDR's ``serverinfo`` reply.

    :param session: aiohttp session to use.
    :param host: Hostname or IP address of the HyperHDR machine.
    :param web_port: HyperHDR's web/JSON-RPC port.
    :param token: API token, needed when HyperHDR runs on another machine and has
        "API authentication" enabled (its default).
    """
    import aiohttp  # noqa: PLC0415

    headers = {"Authorization": f"token {token}"} if token else {}
    url = f"http://{host}:{web_port}/json-rpc"
    try:
        async with session.post(
            url,
            json={"command": "serverinfo", "tan": 1},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S),
        ) as response:
            response.raise_for_status()
            # HyperHDR does not always label the reply as JSON.
            reply = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise HyperHdrError(f"Cannot reach HyperHDR at {url}: {err or type(err).__name__}") from err
    if not isinstance(reply, dict) or not reply.get("success"):
        error = str(reply.get("error", "unknown error")) if isinstance(reply, dict) else "bad reply"
        if "authorization" in error.lower():
            raise HyperHdrAuthError(error)
        raise HyperHdrError(f"HyperHDR refused the request: {error}")
    info = reply.get("info")
    if not isinstance(info, dict):
        raise HyperHdrError("HyperHDR sent a serverinfo reply without an info object")
    return info


class HyperHdrUdpSender:
    """Sends raw LED colour frames to HyperHDR's UDP raw receiver."""

    def __init__(self, host: str, port: int = DEFAULT_UDP_PORT) -> None:
        """
        Initialize the sender.

        :param host: Hostname or IP address of the HyperHDR machine.
        :param port: Port of HyperHDR's UDP raw receiver.
        """
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None

    async def open(self) -> None:
        """Open the UDP socket (resolves the hostname once)."""
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=(self.host, self.port)
            )
        except OSError as err:
            raise HyperHdrError(
                f"Cannot open UDP socket to {self.host}:{self.port}: {err}"
            ) from err

    def send(self, rgb: bytes) -> None:
        """
        Send one frame.

        :param rgb: ``R G B`` bytes for every LED, in HyperHDR's LED order.
        """
        if self._transport is None or self._transport.is_closing():
            return
        if not rgb or len(rgb) % 3 or len(rgb) > MAX_UDP_PAYLOAD:
            raise ValueError(f"Frame must be 3..{MAX_UDP_PAYLOAD} bytes in RGB triplets")
        self._transport.sendto(rgb)

    def close(self) -> None:
        """Close the UDP socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
