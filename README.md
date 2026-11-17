# HyperHDR Sync for Music Assistant

Makes the ambient LEDs driven by [HyperHDR](https://github.com/awawa-dev/HyperHDR) react to the
music [Music Assistant](https://music-assistant.io) plays, the way the built-in WLED Audio Sync
and Hue Lights Sync plugins do for their lights.

Music Assistant already analyses the audio it plays (loudness, a frequency spectrum, onsets, beats
and the colours of the album cover) for its Sendspin visualizer pipeline. This project takes that
data, renders light effects for your LED layout, and streams one colour per LED to HyperHDR's
**UDP raw receiver**. When the music stops, HyperHDR goes back to what it showed before (usually
the screen grabber) three seconds later.

It comes in two forms that share the same rendering code:

| | Standalone bridge | Music Assistant plugin |
|---|---|---|
| Where it runs | Any machine, e.g. the PC running HyperHDR | Inside Music Assistant |
| Needs | Python 3.12+ | A Music Assistant version with community providers |
| Shows up in Music Assistant as | A visualizer player | A light player |
| Settings | Command line | Music Assistant's settings pages, changeable while playing |
| Status | Tested on real hardware with Music Assistant 2.11.0b9 | Tested on real hardware with a dev-branch server; needs a server with community providers |

## Prepare HyperHDR

1. In HyperHDR open **Advanced > Network Services** (for the LED instance you want to drive).
2. Under **UDP raw receiver**, tick **Activate** and save. Keep port `5568`. The priority
   channel (default `109`) must be a lower number than your grabber's so music wins while it plays.
3. Only when HyperHDR runs on a *different* machine than the bridge or Music Assistant: create a
   token under **Token Management**. Reading the LED layout over the network needs it, and
   HyperHDR refuses remote connections altogether while it still has its default password.

HyperHDR's own smoothing and colour calibration still apply to the music colours.

## Standalone bridge

```sh
python -m venv .venv
.venv/Scripts/pip install -r standalone/requirements.txt     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python standalone/hyperhdr_bridge.py --server ws://MUSIC-ASSISTANT-HOST:8927/sendspin
```

A player named **HyperHDR LEDs** appears in Music Assistant. Group it with the speaker the lights
should follow and play something. The bridge reconnects by itself and keeps the same identity
(stored in `~/.hyperhdr-sync-bridge`), so the grouping survives restarts.

Useful options (`--help` lists all):

| Option | Meaning |
|---|---|
| `--demo` | Play a built-in test beat on the LEDs, without Music Assistant |
| `--effect` | `spectrum`, `vu`, `pulse`, `ripple`, `scroll`, `energy`, `ambient` |
| `--palette` | `album` (cover colours), `rainbow`, `sunset`, `ocean`, `fire`, `forest`, `neon` |
| `--brightness` | Percent, on top of HyperHDR's own brightness |
| `--sensitivity` | dB, -12..12. Raise it if the lights look flat |
| `--response` | `snappy`, `balanced`, `smooth` |
| `--latency-ms` | Render this far ahead of the music. Raise it if the lights lag |
| `--hyperhdr-host`, `--udp-port`, `--web-port`, `--token` | Where HyperHDR is (default: this machine) |

`aiosendspin` in `standalone/requirements.txt` should stay at the version your Music Assistant
server uses.

## Music Assistant plugin

The folder `hyperhdr_sync/` is a Music Assistant
[community provider](https://github.com/music-assistant/community-provider-template). Until it is
listed in the community catalog you can load it in a development server:

```sh
uv sync
scripts/run.sh          # Linux/macOS or the dev container; the server does not run on Windows
```

or with the development image, straight from a repository:

```sh
docker run --rm --network host -v ma-dev-data:/data -e PROVIDER_REPO=owner/repository ghcr.io/music-assistant/server:dev
```

Add **HyperHDR Sync** under Settings > Plugins, enter HyperHDR's address, then group the new
**HyperHDR (address)** light player with a speaker. Effect, colours, brightness, sensitivity,
response and timing are changed in the plugin's settings and apply immediately.

The plugin keeps a copy of the LED layout, so it still loads when the HyperHDR machine is switched
off while Music Assistant starts, and re-reads the layout when music starts.

## Effects

| Effect | What you see |
|---|---|
| Spectrum | Bass at the bottom of both sides, rising to treble at top-centre |
| Level meter | Loudness climbing both sides, with a falling peak marker |
| Pulse | All LEDs breathe with the bass and flash on beats; the colour steps on each beat |
| Ripple | Every beat launches a wave from the bottom of both sides up to the top |
| Scroll | The colour of the current sound flows upwards |
| Energy | Three overlapping bars for bass, mids and treble |
| Ambient | Slowly rotating colours whose brightness gently follows the music |

Brightness adapts to each track by itself: every frequency band is scaled against its own recent
maximum, so quiet and loud recordings both use the full range.

## Limits

- At most 500 LEDs per HyperHDR instance (the UDP raw receiver takes 1500 bytes per frame).
- The sound features are mono; there is no left/right separation.
- Beat-grid data only arrives when the **Smart Fades** audio-analysis provider is enabled in
  Music Assistant and has analysed the track; otherwise onsets detected in the audio are used.

## Development

```sh
uv sync
uv run pytest        # on Windows add --noconftest and leave out tests/test_provider.py
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run codespell
```

`tests/test_provider.py` boots a real Music Assistant server with the plugin and a fake HyperHDR;
it needs Linux or macOS. The other tests cover the rendering engine, the LED layout and the
HyperHDR client and run anywhere.

| Path | What it is |
|---|---|
| `hyperhdr_sync/engine.py` | Sound features in, LED colours out (numpy only) |
| `hyperhdr_sync/layout.py` | LED positions around the screen |
| `hyperhdr_sync/hyperhdr.py` | Reads the LED layout, sends UDP frames |
| `hyperhdr_sync/bridge.py`, `provider.py`, `setup_flow.py`, `settings.py` | The Music Assistant plugin |
| `standalone/hyperhdr_bridge.py` | The standalone bridge |

## Licence

Apache-2.0, like Music Assistant.
