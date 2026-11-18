"""Home Assistant add-on entry point: turns the add-on options into bridge arguments."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

OPTIONS_FILE = Path("/data/options.json")
# /data survives add-on updates, so the bridge keeps its identity and its group in Music Assistant.
STATE_DIR = "/data/bridge"
BRIDGE_DIR = Path(__file__).resolve().parent / "standalone"

# add-on option -> bridge command line flag
_FLAGS = {
    "server": "--server",
    "name": "--name",
    "hyperhdr_host": "--hyperhdr-host",
    "udp_port": "--udp-port",
    "web_port": "--web-port",
    "effect": "--effect",
    "palette": "--palette",
    "brightness": "--brightness",
    "sensitivity": "--sensitivity",
    "response": "--response",
    "latency_ms": "--latency-ms",
    "color": "--color",
    "night_light": "--night-light",
}


def options_to_argv(options: dict[str, Any]) -> list[str]:
    """
    Return the bridge's command line for the add-on options.

    The token is left out: it goes through the environment, not the visible command line.

    :param options: The contents of the add-on's options.json.
    """
    argv = ["--state-dir", STATE_DIR]
    for key, flag in _FLAGS.items():
        value = options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        argv += [flag, str(value).strip()]
    argv += ["--log-level", str(options.get("log_level") or "info").upper()]
    # The Supervisor gives every add-on a token; with it the web UI becomes the Ingress panel.
    if os.environ.get("SUPERVISOR_TOKEN"):
        argv.append("--ui-supervisor")
    return argv


def main() -> None:
    """Start the bridge with the add-on options."""
    options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    if not str(options.get("hyperhdr_host") or "").strip():
        sys.exit("Set 'hyperhdr_host' in the add-on configuration to the machine HyperHDR runs on.")
    if token := str(options.get("token") or "").strip():
        os.environ["HYPERHDR_TOKEN"] = token
    sys.path.insert(0, str(BRIDGE_DIR))
    import hyperhdr_bridge  # noqa: PLC0415

    sys.argv = ["hyperhdr_bridge", *options_to_argv(options)]
    hyperhdr_bridge.main()


if __name__ == "__main__":
    main()
