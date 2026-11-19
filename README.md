# HyperHDR Sync for Music Assistant

Makes the ambient LEDs driven by [HyperHDR](https://github.com/awawa-dev/HyperHDR) react to the
music [Music Assistant](https://music-assistant.io) plays, the way the built-in WLED Audio Sync
and Hue Lights Sync plugins do for their lights.

Music Assistant already analyses the audio it plays (loudness, a frequency spectrum, onsets, beats
and the colours of the album cover) for its Sendspin visualizer pipeline. This project takes that
data, renders light effects for your LED layout, and streams one colour per LED to HyperHDR's
**UDP raw receiver**. When the music stops, HyperHDR goes back to what it showed before (usually
the screen grabber) three seconds later.

It comes in three forms that share the same rendering code:

| | Home Assistant add-on | Standalone bridge | Music Assistant plugin |
|---|---|---|---|
| Where it runs | Next to the Music Assistant add-on | Any machine, e.g. the PC running HyperHDR | Inside Music Assistant |
| Needs | Home Assistant OS or Supervised | Python 3.12+ | A Music Assistant version with community providers |
| Shows up in Music Assistant as | A visualizer player | A visualizer player | A light player |
| Settings | The add-on's Configuration tab | Command line | Music Assistant's settings pages, changeable while playing |

The add-on and the standalone bridge are the same program and work with Music Assistant 2.11
today. The plugin needs a Music Assistant version that can load community providers.

## Home Assistant add-on

[![Add the repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FJaL-P%2Fhyperhdr-sync)

1. Prepare HyperHDR (below).
2. In Home Assistant open **Settings > Add-ons > Add-on Store**, choose **Repositories** in the
   ⋮ menu and add `https://github.com/JaL-P/hyperhdr-sync` (or use the button above).
3. Install **HyperHDR Sync**, enter HyperHDR's IP address under **Configuration** and start it.
4. In Music Assistant, group the new **HyperHDR LEDs** player with a speaker and play something.

The default Sendspin address `ws://127.0.0.1:8927/sendspin` reaches a Music Assistant add-on on
the same Home Assistant. Its folder is [`hyperhdr_sync_addon/`](hyperhdr_sync_addon); the
add-on builds from the latest code on `main`, so no release tag is needed.

## Prepare HyperHDR

1. In HyperHDR open **Advanced > Network Services** (for the LED instance you want to drive).
2. Under **UDP raw receiver**, tick **Activate** and save. Keep port `5568`. The priority
   channel (default `109`) must be a lower number than your grabber's so music wins while it plays.
3. Only when the bridge, add-on or Music Assistant is on a *different subnet* than HyperHDR:
   create a token under **Token Management** and give it with `--token` or the add-on's token
   option. HyperHDR answers machines on its own subnet without one, and refuses the others
   without a token.

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
| `--effect` | `lumina`, `colora`, `spectrum`, `retro`, `scanner`, `rhythm`, `party`, `vu`, `pulse`, `ripple`, `scroll`, `energy`, `ambient`, `manual` |
| `--palette` | `album` (cover colours), `custom` (shades of `--color`), `rainbow`, `classic`, `sunset`, `ocean`, `aurora`, `ice`, `fire`, `lava`, `warm`, `forest`, `neon`, `candy`, `pastel` |
| `--album-mode` | How `album` picks colours from the cover: `vibrant`, `dominant`, `accent`, `natural`, `pastel`, `contrast` |
| `--color` | Custom colour `#rrggbb` for `manual`, `custom` and the night light (default warm white) |
| `--night-light` | 0-100: glow in the custom colour while no music plays, instead of handing the LEDs back to HyperHDR |
| `--brightness` | Percent, on top of HyperHDR's own brightness |
| `--sensitivity` | dB, -12..12. Raise it if the lights look flat |
| `--response` | `snappy`, `balanced`, `smooth` |
| `--latency-ms` | Render this far ahead of the music. Raise it if the lights lag |
| `--hyperhdr-host`, `--udp-port`, `--web-port`, `--token` | Where HyperHDR is (default: this machine) |
| `--milkdrop-preset`, `--chromium` | The MilkDrop effect's preset (default: picked as Music Assistant picks it) and the Chromium that draws it (see below) |
| `--ma-token` (or `MA_TOKEN`), `--ma-url`, `--milkdrop-player` | Give Liquid and the MilkDrop presets the real sound wave from Music Assistant's MilkDrop Visualizer plugin (see below) |
| `--ui-port` | Serve the web UI (live view, previews, Apply) on this port, e.g. `8080`, then open `http://127.0.0.1:8080`. Add `--ui-host 0.0.0.0` to reach it from other machines; it has no login |

The Home Assistant add-on has the same web UI in the sidebar, where applied settings are also
saved in the add-on's configuration.

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
| Lumina | One calm colour that gets brighter as the music gets louder |
| Colora | Steady brightness; the colour follows the sound, bass to treble |
| Retro | Graphic equaliser: eight frequency blocks with peak dots, like an old hi-fi (try the Classic colours) |
| Scanner | A light sweeping back and forth with a trail; faster when louder, flashing on beats |
| Rhythm | Colour blocks that swap on every beat |
| Party | Random colour bursts on beats and sparkles on the treble |
| Manual | Every LED in the custom colour; does not follow the music |
| Spectrum | Bass at the bottom of both sides, rising to treble at top-centre |
| Level meter | One meter climbing both sides with the loudness, with a falling peak |
| Pulse | Every LED flashes on the beat and goes dark in between |
| Ripple | Every beat launches a wave from the bottom of both sides up to the top |
| Scroll | The colour of the current sound flows upwards |
| Energy | Bass rises from the bottom, treble drops from the top; they meet in between |
| Ambient | Calm colours turning around the screen; the music sets the speed, not the brightness |
| Flow | Comets of light circling the screen, faster when louder. Inspired by Paraline |
| Centre pulse | Every beat sends a pulse from the middle of each edge to the corners. Inspired by Paraline |
| Fireflies | Small lights drifting around the screen; beats speed them up. Inspired by Paraline |
| Snowfall | Glowing flakes falling down the sides, more with the treble. Inspired by Paraline |
| Crystals | Shards that flare with their own frequency and flutter with the treble. Inspired by Paraline |
| Braids | Two strands of colour winding along the sides, crossing as they move. Inspired by Paraline |
| Embers | A flickering glow with sparks rising from the bottom on the bass. Inspired by Paraline |
| Aurora | Slow, folded curtains of colour rising from the bottom. Inspired by Paraline |
| Horizon | A sun at the top pulsing with the bass, grid lines rushing down the sides. Inspired by Paraline |
| Side bars | Bars growing from the middle of each side: bass left, treble right. Inspired by Paraline |
| Liquid | Swirling trails that zoom, twist and fade into themselves. In the style of MilkDrop; draws the real sound wave with the MilkDrop plugin (below) |
| MilkDrop | The real MilkDrop presets, as in Music Assistant's visualizer, seen by the LEDs like a screen grabber (below) |

### The real MilkDrop presets

The **MilkDrop** effect shows the same presets as Music Assistant's MilkDrop visualizer, as if
HyperHDR were grabbing a screen showing it: each LED takes the colour at its edge of the
picture. Music Assistant draws those pictures in your browser, so the bridge draws its own:
it runs [Butterchurn](https://github.com/jberg/butterchurn) (MilkDrop in WebGL) with the
[butterchurn-presets](https://www.npmjs.com/package/butterchurn-presets) in a headless
Chromium, on a small 128x72 picture, only while MilkDrop is in use. It mirrors what Music
Assistant uses:

- **The same presets**: the versions and packs Music Assistant's web interface bundles
  (Butterchurn 3.0.0-beta.5, butterchurn-presets 2.4.7 base and extra: 245 presets) and its
  default "high" quality.
- **Picked the same way**: the preset picker's default, **Same as Music Assistant**, follows
  your visualizer settings in Music Assistant: random, random from your starred favourites,
  or a fixed preset, and with beat switch on, a new preset on a downbeat once the set time has
  passed (2.7 s blends, like Music Assistant). It reads them from your Music Assistant user
  with the token below; without one it uses Music Assistant's defaults (a random preset,
  picked when the music starts). You can also pick a preset for the LEDs only.
- **Coloured the same way**: **Colours > MilkDrop** tints the picture with the cover's colour,
  as Music Assistant's visualizer does (a colour blend with the track palette's `on_light`,
  which Sendspin also sends; untinted when the cover has none). **Album cover**, a
  **Palette** or **Your colour** tint it with those colours instead.
- With another effect, **Colours > MilkDrop** gives it the colours round the edges of the live
  MilkDrop picture, so they change with it.
- With the Music Assistant token below, the presets dance to the real sound wave; without it,
  to a wave made from the spectrum Sendspin sends.

The add-on includes Chromium and Butterchurn. For the standalone bridge, install Chromium
(or give `--chromium`) and run `python standalone/fetch_butterchurn.py` once: it downloads the
pinned Butterchurn releases from npm and checks them against their published checksums.
Without them, MilkDrop shows Liquid instead.

### The real sound wave for MilkDrop and Liquid

Music Assistant's **MilkDrop Visualizer** plugin streams the sound wave of what a player plays
(small 8-bit snapshots, about 43 a second, taken from the audio Music Assistant decodes anyway).
With it, the MilkDrop presets dance to that wave, as in Music Assistant, and Liquid draws it as
its ring, both in time with the lights.

1. In Music Assistant, enable the plugin under **Settings > Plugins**.
2. Create a long-lived token in Music Assistant (**Settings**, your profile).
3. Put it in the add-on's **Music Assistant token** option, or give the standalone bridge
   `--ma-token` (or set `MA_TOKEN`).

The bridge looks for Music Assistant on port 8095 of the Sendspin address's host; set
`--ma-url` (`ma_url`) if it is elsewhere. It follows whichever player is playing; set
`--milkdrop-player` (`milkdrop_player`) to a player id to pick one. Without a token Liquid uses
a ring shaped by the spectrum. The plugin is marked experimental in Music Assistant, so a
Music Assistant update may change it.

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
| `hyperhdr_sync_addon/`, `repository.yaml` | The Home Assistant add-on, which runs the standalone bridge |

## Credits

Flow, Centre pulse, Fireflies, Snowfall, Crystals, Braids, Embers, Aurora, Horizon and Side bars
are our own takes on effect ideas from [Paraline](https://github.com/SamXop123/Paraline) by
[SamXop123](https://github.com/SamXop123), a desktop audio visualizer that turns the edges of
the screen into light. They are written for LED strips from Paraline's descriptions of its
themes; no code is taken from Paraline, whose licence does not allow reuse.

Liquid is our own effect in the style of [MilkDrop](https://www.geisswerks.com/milkdrop/) by
Ryan Geiss, the music visualizer Music Assistant also offers as a plugin: a picture that keeps
zooming, twisting and fading into itself while the music draws on it. No MilkDrop code or
presets are used.

The MilkDrop effect runs [Butterchurn](https://github.com/jberg/butterchurn) by Jordan Berg
and the presets of [butterchurn-presets](https://www.npmjs.com/package/butterchurn-presets)
(both MIT), made by many MilkDrop preset authors. They are downloaded when the add-on is built,
not kept in this repository.

The web UI's **Credits** link lists everyone this project builds on.

## Support

Have some spare cash and want to support me? Then buy me a coffee.

[![Support JaLP on Ko-fi](https://storage.ko-fi.com/cdn/generated/zfskfgqnf/rest-8069498602ed186c3d10b21015c27d14-uaqhyms7.jpg)](https://ko-fi.com/jalp_)

[ko-fi.com/jalp_](https://ko-fi.com/jalp_)

## Licence

Apache-2.0, like Music Assistant.
