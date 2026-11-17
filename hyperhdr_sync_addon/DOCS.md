# HyperHDR Sync

Makes the LEDs driven by HyperHDR react to the music Music Assistant plays. The add-on joins
Music Assistant as a player called **HyperHDR LEDs**; group it with the speaker the lights should
follow and press play. When the music stops, HyperHDR goes back to its screen grabber after three
seconds.

## Before you start

1. In HyperHDR open **Advanced > Network Services** and tick **Activate** under
   **UDP raw receiver**. Keep port `5568`.
2. Music Assistant 2.11 or later with Sendspin (it is on by default).

## Configuration

- **HyperHDR address** (required): the IP address of the machine running HyperHDR.
- **Music Assistant Sendspin address**: keep `ws://127.0.0.1:8927/sendspin` when Music Assistant
  runs as an add-on here. Otherwise use `ws://MUSIC-ASSISTANT-IP:8927/sendspin`.
- **API token**: only needed when HyperHDR is on a different subnet. Create one in HyperHDR under
  **Token Management**.
- **Effect**, **Colours**, **Brightness**, **Sensitivity**, **Response**, **Latency**: how the
  lights look. Restart the add-on after changing them.

Only run one bridge per set of LEDs. If you also started the standalone bridge on a PC, stop it.

## Limits

- At most 500 LEDs per HyperHDR instance.
- Beat-grid data needs the Smart Fades audio analysis in Music Assistant; without it the lights
  follow onsets found in the audio.
