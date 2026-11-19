# Changelog

## 0.4.0

- The add-on builds from the latest code on `main` instead of a release tag of the same
  version, so updates install as soon as a change is merged.
- A simpler web UI. It shows six popular effects first, with the rest behind "Show all"
  and grouped by mood; each effect's description sits behind an info button.
- Colours start with where they come from (Album cover, Palette or Your colour), and only the
  matching options appear: Artwork colours only for the album cover, the colour picker only
  for your colour, the solid colour or the night light.
- Make your own palettes in the web UI: 2 to 6 colours, blended as a gradient or shown as
  separate swatches. They are saved with the add-on's options.
- The night light is now a "Keep a soft glow" switch; sensitivity, response and latency are
  under Fine-tuning; Apply appears only when something has changed.
- Light and dark mode, following the system or chosen in the page; the page also picks up
  the colours of the LEDs. One preview, drawn as a monitor with the LED layout from HyperHDR.

## 0.3.4

- Retro keeps its colours on short strips: blocks of fewer than four LEDs show their band by
  brightness, and the peak dots and gaps take one LED each instead of covering whole blocks.
- Retro, Colora and Spectrum follow the music on streams that send only the loudness: Retro
  and Spectrum rise with the level (they were dark), and Colora's colour follows the loudness
  (it was a fixed light).

## 0.3.3

- Every effect now does something of its own; four overlapped with others:
  - Retro is a graphic equaliser: eight frequency blocks with peak dots (it was a segmented
    Level meter).
  - Energy pits bass rising from the bottom against treble dropping from the top (it was bars
    growing like the Level meter).
  - Pulse only flashes on the beat (it breathed with the bass like Lumina).
  - Ambient keeps a steady brightness and the music sets how fast its colours turn.
  - Colora keeps a steady brightness; only its colour follows the sound.

## 0.3.2

- The preview now stays in step with the LEDs while music plays: with unchanged settings it
  shows exactly what the LEDs show, and a change starts from the LEDs' current state (levels,
  colours, even Party's random bursts), so only the change itself differs.

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
