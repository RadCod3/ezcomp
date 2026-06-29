# ezcomp

Interactive side-by-side quality comparison of two video files — e.g. a Blu-ray
source vs an encode. Scrub a shared timeline, instantly toggle A/B on the same
frame, and zoom to pixel level to spot encoding artifacts. Like slow.pics, but
for live video instead of stills.

Built on **libmpv** (decode + libplacebo color management, incl. HDR/10-bit) and
**PySide6**. Two mpv instances render into offscreen FBOs that a fragment shader
composites, giving A/B toggle, split-wipe, difference, and onion-skin views with
correct per-source color (e.g. HDR tonemapped vs native SDR).

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
| `C` | native-res screenshot per source → Desktop (both A & B in composite modes) |
| `Shift`+`C` | window screenshot of the current composite (as displayed) → Desktop |
| `F` | fullscreen |
| `O` | open files |
| `Q` | quit |

Navigation is **frame-locked**: paused step/seek keeps both sources on the same
frame. Native playback uses two real-time clocks and can drift slightly;
pausing re-locks them. (Smooth realtime synced playback is planned.)

## Status

Early prototype. Working: the comparison engine and all view modes above.
Planned: master/follower realtime sync, alignment (frame offset, resolution/crop
mismatch), frame export + slow.pics upload, bookmarks, loupe.
