"""Shared constants: GL enums and comparison modes."""

# OpenGL enums (QOpenGLFunctions does not expose them as Python constants).
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_BUFFER_BIT = 0x4000
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE1 = 0x84C1
GL_TRIANGLES = 0x0004

# Comparison modes
MODE_A, MODE_B, MODE_WIPE, MODE_DIFF, MODE_ONION = range(5)
MODE_NAMES = {
    MODE_A: "A",
    MODE_B: "B",
    MODE_WIPE: "wipe",
    MODE_DIFF: "diff",
    MODE_ONION: "onion",
}

# Default tunable per-mode parameter (wipe position / diff gain / onion mix)
DEFAULT_PARAMS = {MODE_WIPE: 0.5, MODE_DIFF: 12.0, MODE_ONION: 0.5}
