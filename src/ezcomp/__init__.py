"""ezcomp — interactive two-video quality comparison tool."""
import ctypes
import os

# Preload Homebrew libmpv BEFORE python-mpv is imported anywhere, otherwise its
# ctypes lookup fails to find the dylib on macOS. Must happen at package import
# time (before ezcomp.compositor pulls in `mpv`).
for _p in ("/opt/homebrew/lib/libmpv.2.dylib", "/usr/local/lib/libmpv.2.dylib"):
    if os.path.exists(_p):
        ctypes.CDLL(_p, mode=ctypes.RTLD_GLOBAL)
        break

__version__ = "0.1.0"
