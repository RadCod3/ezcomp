# ezcomp

Interactive side-by-side quality comparison of two video files — e.g. a Blu-ray
source vs an encode. Scrub a shared timeline, instantly toggle A/B on the same
frame, and zoom to pixel level to spot encoding artifacts. Like slow.pics, but
for live video instead of stills.

Built on **libmpv** (decode + libplacebo color management, incl. HDR/10-bit) and
**PySide6**. Two mpv instances render into offscreen float FBOs that a fragment
shader composites, giving A/B toggle, split-wipe, difference, and onion-skin
views with correct per-source color.

On macOS the composite is drawn into a custom float `NSOpenGLView` whose window
colorspace we control, which enables **EDR / true HDR passthrough**: each source
is auto-detected as HDR or SDR and rendered correctly (HDR shown bright via PQ on
an HDR-enabled display, SDR via BT.709). Toggling A↔B switches the surface
colorspace to match the shown source. `Force SDR` tonemaps everything (and is the
automatic fallback when the display has no EDR headroom).

## Requirements

- macOS (primary), Linux, Windows
- `libmpv` — macOS: `brew install mpv`
- [uv](https://docs.astral.sh/uv/)

## Run

```sh
uv sync
uv run ezcomp [REFERENCE.mkv] [ENCODE.mkv]
```

Or drop two files onto the window / use **Open…** (`O`).

## Keys

| | |
|---|---|
| `Space` | play / pause |
| `Tab` | toggle A ↔ B |
| `1` `2` `3` `4` `5` | A · B · wipe · diff · onion |
| `[` `]` | adjust mode param (wipe pos / diff gain / onion mix) |
| mouse (wipe mode) | drag the divider |
| `.` `,` | frame step forward / back |
| `←` `→` | seek ∓ / ± 2s |
| `+` `-` / wheel | zoom |
| `W A S D` | pan |
| `0` | reset zoom/pan |
| `H` | Force SDR (tonemap everything); HDR is otherwise auto per source |
| `I` | toggle the on-screen OSD (info + seek bar) |
| `C` | native-res screenshot per source → Desktop (both A & B in composite modes) |
| `F` | fullscreen |
| `O` | open files |
| `Q` | quit |

The OSD overlay shows the color state, mode, **frame number + timecode**, zoom,
and the A/B filenames, plus a **seek bar** at the bottom (drag to scrub both
sources together). Toggle it all with `I`.

Navigation is **frame-locked**: paused step/seek keeps both sources on the same
frame. Native playback uses two real-time clocks and can drift slightly;
pausing re-locks them. (Smooth realtime synced playback is planned.)

## Status

Early prototype. Working: the comparison engine, all view modes, and macOS
HDR/EDR passthrough. Planned: master/follower realtime sync, alignment (frame
offset, resolution/crop mismatch), frame export + slow.pics upload, bookmarks,
loupe.
