# HyperHDR Sync

Makes the LEDs driven by HyperHDR react to the music Music Assistant plays. The add-on joins
Music Assistant as a player called **HyperHDR LEDs**; group it with the speaker the lights should
follow and press play. When the music stops, HyperHDR goes back to its screen grabber after three
seconds.

## Before you start

1. In HyperHDR open **Advanced > Network Services** and tick **Activate** under
   **UDP raw receiver**. Keep port `5568`.
2. Music Assistant 2.11 or later with Sendspin (it is on by default).

## Web UI

Open **HyperHDR Sync** in the sidebar (or **Open Web UI** on the add-on page). It shows what
your LEDs show right now next to a preview. Pick an effect, colours, brightness and so on: the
preview changes straight away, following the music that is playing or a built-in test beat, while
your LEDs keep their current look. **Apply** puts the new settings on the LEDs and saves them in
the add-on's configuration.

A yellow **!** means no music reaches the LEDs. In Music Assistant, open the player you are
listening on, choose **Group members** and tick the **HyperHDR LEDs** screen (the name set in
the `name` option).

## Configuration

- **HyperHDR address** (required): the IP address of the machine running HyperHDR.
- **Music Assistant Sendspin address**: keep `ws://127.0.0.1:8927/sendspin` when Music Assistant
  runs as an add-on here. Otherwise use `ws://MUSIC-ASSISTANT-IP:8927/sendspin`.
- **API token**: only needed when HyperHDR is on a different subnet. Create one in HyperHDR under
  **Token Management**.
- **Night light**: brightness of the custom colour while no music plays. At 0 (the default) the
  LEDs go back to HyperHDR's screen grabber when the music stops; above 0 they keep glowing in
  the custom colour, so the TV picture is not shown on them until you turn it off.
- **Artwork colours**: how the Album art colours are picked from the cover: Vibrant (every
  cover colour, boosted), Dominant (the main colour), Accent, Natural (as on the cover),
  Pastel or Contrast (the main colour and its opposite).
- **Custom colour**: `#rrggbb`, for the Manual effect, the Custom colour palette and the night
  light.
- **Your palette's colours**, **Your palette as blocks**, **Saved palettes**: the palettes you
  make in the web UI (Colours > Palette > New palette) and the one in use with the `mine`
  colours. Easiest to manage in the web UI, which saves them here.
- **Effect**, **Colours**, **Brightness**, **Sensitivity**, **Response**, **Latency**: how the
  lights look. The web UI changes them without a restart; after changing them here, restart the
  add-on.

Only run one bridge per set of LEDs. If you also started the standalone bridge on a PC, stop it.

## Limits

- At most 500 LEDs per HyperHDR instance.
- Beat-grid data needs the Smart Fades audio analysis in Music Assistant; without it the lights
  follow onsets found in the audio.
