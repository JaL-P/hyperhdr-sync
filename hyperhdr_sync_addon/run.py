"""Home Assistant add-on entry point: turns the add-on options into bridge arguments."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

OPTIONS_FILE = Path("/data/options.json")
# /data survives add-on updates, so the bridge keeps its identity and its group in Music Assistant.
STATE_DIR = "/data/bridge"
BRIDGE_DIR = Path(__file__).resolve().parent / "standalone"
CONTAINER_ENV_DIR = Path("/run/s6/container_environment")

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
    "album_mode": "--album-mode",
    "night_light": "--night-light",
    "colors": "--colors",
}
# add-on option -> bridge flag that is given without a value when the option is on
_SWITCHES = {"steps": "--steps"}


def options_to_argv(options: dict[str, Any]) -> list[str]:
    """
    Return the bridge's command line for the add-on options.

    The token is left out: it goes through the environment, not the visible command line.

    :param options: The contents of the add-on's options.json.
    """
    argv = ["--state-dir", STATE_DIR]
    for key, flag in _FLAGS.items():
        value = options.get(key)
        if isinstance(value, list):
            value = ",".join(str(item).strip() for item in value)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        argv += [flag, str(value).strip()]
    argv += [flag for key, flag in _SWITCHES.items() if options.get(key) is True]
    argv += ["--log-level", str(options.get("log_level") or "info").upper()]
    # The add-on always runs under the Supervisor: serve the web UI as its Ingress panel.
    argv.append("--ui-supervisor")
    return argv


def load_container_environment(env_dir: Path = CONTAINER_ENV_DIR) -> None:
    """
    Take over container variables such as ``SUPERVISOR_TOKEN`` that S6 overlay withheld.

    The base image's S6 overlay starts the bridge with a cleaned environment and keeps
    the container's variables as one file each; without the token the bridge cannot
    ask the Supervisor where to serve the web UI.

    :param env_dir: Folder with one file per variable.
    """
    for name in ("SUPERVISOR_TOKEN",):
        if os.environ.get(name):
            continue
        with contextlib.suppress(OSError):
            if value := (env_dir / name).read_text(encoding="utf-8").strip():
                os.environ[name] = value


def main() -> None:
    """Start the bridge with the add-on options."""
    load_container_environment()
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
