"""Application entry point: configure GL, locale, and launch the window."""
import locale
import os
import sys

from PySide6 import QtGui, QtWidgets

from .mainwindow import MainWindow

# Dev convenience: fall back to the local sample pair if no files are given.
_SAMPLES = "/Users/radith/Downloads/samples"
_DEFAULT_A = os.path.join(_SAMPLES, "DV-HDR.mkv")
_DEFAULT_B = os.path.join(_SAMPLES, "SDR.mkv")


def main():
    args = sys.argv[1:]
    a = args[0] if len(args) >= 1 else None
    b = args[1] if len(args) >= 2 else None
    if a is None and b is None and os.path.exists(_DEFAULT_A) and os.path.exists(_DEFAULT_B):
        a, b = _DEFAULT_A, _DEFAULT_B

    # macOS needs a modern core-profile GL context for mpv's renderer.
    fmt = QtGui.QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)

    app = QtWidgets.QApplication(sys.argv)
    # QApplication's constructor resets LC_NUMERIC, which libmpv rejects.
    locale.setlocale(locale.LC_NUMERIC, "C")

    win = MainWindow(a, b)
    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
