# Changelog

## 0.3.1

- Fix the web UI panel never starting ("The app seems to not be ready"): the add-on did
  not get its Supervisor token, so the panel was never switched on.

## 0.3.0

- Web UI in the sidebar: see what the LEDs show, preview other settings against the music or a
  test beat, and apply them without restarting.
- Black-and-white album art now lights the LEDs in shades of white instead of red.
- New effects: Lumina (adaptive brightness), Colora (adaptive colour), Retro, Scanner, Rhythm,
  Party and Manual (a fixed custom colour).
- New colours: Aurora, Pastel, Ice, Candy, Lava, Warm, Classic and Custom colour.
- Artwork colours: choose how Album art picks colours from the cover (Vibrant, Dominant,
  Accent, Natural, Pastel or Contrast).
- Colour gradients blend around the colour wheel, so they stay vivid instead of turning grey
  between opposite colours.
- Night light: glow in the custom colour while no music plays, instead of handing the LEDs back
  to HyperHDR. The web UI previews it with "When idle".

## 0.2.0

- First version of the Home Assistant add-on, running the standalone bridge.
