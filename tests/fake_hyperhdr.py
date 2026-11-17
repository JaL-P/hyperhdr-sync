"""A stand-in for HyperHDR that the tests talk to instead of a real one."""

import asyncio
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

LEDS = [
    {"hmin": 0.0, "hmax": 0.04, "vmin": 0.5, "vmax": 0.6},
    {"hmin": 0.4, "hmax": 0.6, "vmin": 0.0, "vmax": 0.06},
    {"hmin": 0.96, "hmax": 1.0, "vmin": 0.5, "vmax": 0.6},
]
TOKEN = "test-token"  # noqa: S105 - a made-up value for the fake server


class FakeHyperHdr:
    """A stand-in for HyperHDR: its JSON-RPC web endpoint and its UDP raw receiver."""

    def __init__(self, *, require_token: bool = False) -> None:
        """
        Initialize the fake.

        :param require_token: Whether the web API refuses requests without the token.
        """
        self.require_token = require_token
        self.leds: list[dict[str, float]] = LEDS
        self.frames: list[bytes] = []
        self.frame_received = asyncio.Event()
        self.web_port = 0
        self.udp_port = 0
        self._server: TestServer | None = None
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        """Start listening on free local ports."""
        app = web.Application()
        app.router.add_post("/json-rpc", self._json_rpc)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        assert self._server.port is not None
        self.web_port = self._server.port
        fake = self

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: Any) -> None:
                fake.frames.append(data)
                fake.frame_received.set()

        self._transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Receiver, local_addr=("127.0.0.1", 0)
        )
        self.udp_port = self._transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        """Stop listening."""
        if self._transport:
            self._transport.close()
        if self._server:
            await self._server.close()

    async def _json_rpc(self, request: web.Request) -> web.Response:
        command = await request.json()
        if self.require_token and request.headers.get("Authorization") != f"token {TOKEN}":
            return web.json_response(
                {"command": command["command"], "error": "No Authorization", "success": False}
            )
        return web.json_response(
            {"command": "serverinfo", "info": {"leds": self.leds}, "success": True}
        )
