"""Main window: toolbar, drag-and-drop loading, HUD overlay, key bindings."""
from PySide6 import QtCore, QtGui, QtWidgets

from . import constants as C
from .compositor import Compositor

_HUD_CSS = (
    "color: #eee; background: rgba(0,0,0,150); padding: 5px 8px;"
    "border-radius: 4px; font-family: Menlo, monospace; font-size: 11px;"
)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path_a=None, path_b=None):
        super().__init__()
        self.setWindowTitle("ezcomp")
        self.resize(1280, 720)
        self.setAcceptDrops(True)

        self.comp = Compositor(self)
        self.setCentralWidget(self.comp)

        self.hud = QtWidgets.QLabel(self.comp)
        self.hud.setStyleSheet(_HUD_CSS)
        self.hud.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.hud.move(10, 10)

        self.flash = QtWidgets.QLabel(self.comp)
        self.flash.setStyleSheet(_HUD_CSS)
        self.flash.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.flash.hide()
        self._flash_timer = QtCore.QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self.flash.hide)

        self._build_toolbar()
        self.comp.state_changed.connect(self._refresh_hud)
        self._hud_timer = QtCore.QTimer(self)          # for the live clock
        self._hud_timer.timeout.connect(self._refresh_hud)
        self._hud_timer.start(150)

        if path_a or path_b:
            self.comp.set_sources(path_a, path_b)

    # ---- toolbar ----
    def _build_toolbar(self):
        tb = self.addToolBar("main")
        tb.setMovable(False)

        def act(text, slot, tip=""):
            a = QtGui.QAction(text, self)
            a.triggered.connect(slot)
            a.setToolTip(tip)
            tb.addAction(a)
            return a

        act("Open…", self.open_files, "Load two video files (O)")
        tb.addSeparator()
        act("A", lambda: self.comp.set_mode(C.MODE_A), "Show A (1)")
        act("B", lambda: self.comp.set_mode(C.MODE_B), "Show B (2)")
        act("Wipe", lambda: self.comp.set_mode(C.MODE_WIPE), "Split wipe (3)")
        act("Diff", lambda: self.comp.set_mode(C.MODE_DIFF), "Difference (4)")
        act("Onion", lambda: self.comp.set_mode(C.MODE_ONION), "Onion-skin (5)")
        tb.addSeparator()
        act("⏯", self.comp.play_pause, "Play/Pause (Space)")
        act("−", lambda: self.comp.change_zoom(-0.35), "Zoom out (-)")
        act("+", lambda: self.comp.change_zoom(+0.35), "Zoom in (+)")
        act("Reset", self.comp.reset_view, "Reset zoom/pan (0)")

    # ---- file loading ----
    def open_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select reference (A) then encode (B)", "",
            "Video (*.mkv *.mp4 *.mov *.m2ts *.ts *.webm *.avi);;All files (*)",
        )
        if files:
            self._load(files)

    def _load(self, files):
        a = files[0] if len(files) >= 1 else self.comp.paths[0]
        b = files[1] if len(files) >= 2 else self.comp.paths[1]
        self.comp.set_sources(a, b)
        self._show_flash(f"A: {_name(a)}    B: {_name(b)}")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self._load(files)

    # ---- HUD ----
    def _show_flash(self, msg):
        self.flash.setText(msg)
        self.flash.adjustSize()
        self.flash.move(10, self.comp.height() - self.flash.height() - 10)
        self.flash.show()
        self.flash.raise_()
        self._flash_timer.start(2500)

    def _refresh_hud(self):
        s = self.comp.status()
        if s is None:
            return
        pstr = f" {s['param']:.2f}" if s["param"] is not None else ""
        state = "❚❚" if s["paused"] else "▶"
        self.hud.setText(
            f"{state}  mode {C.MODE_NAMES[s['mode']]}{pstr}\n"
            f"frame {s['frame']}   t {s['time']:.3f}s   zoom {s['zoom']:.2f}×\n"
            f"A {s['a']}\nB {s['b']}"
        )
        self.hud.adjustSize()
        self.hud.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.flash.isVisible():
            self.flash.move(10, self.comp.height() - self.flash.height() - 10)

    # ---- keys ----
    def keyPressEvent(self, e):
        K = QtCore.Qt
        k = e.key()
        c = self.comp
        if k in (K.Key_Q, K.Key_Escape):
            self.close()
        elif k == K.Key_O:
            self.open_files()
        elif k == K.Key_F:
            self.toggle_fullscreen()
        elif k == K.Key_Space:
            c.play_pause()
        elif k == K.Key_Tab:
            c.toggle_ab()
        elif k == K.Key_1:
            c.set_mode(C.MODE_A)
        elif k == K.Key_2:
            c.set_mode(C.MODE_B)
        elif k == K.Key_3:
            c.set_mode(C.MODE_WIPE)
        elif k == K.Key_4:
            c.set_mode(C.MODE_DIFF)
        elif k == K.Key_5:
            c.set_mode(C.MODE_ONION)
        elif k == K.Key_BracketLeft:
            c.adjust_param(-1)
        elif k == K.Key_BracketRight:
            c.adjust_param(+1)
        elif k == K.Key_Period:
            c.frame_step(False)
        elif k == K.Key_Comma:
            c.frame_step(True)
        elif k == K.Key_Right:
            c.seek(2)
        elif k == K.Key_Left:
            c.seek(-2)
        elif k in (K.Key_Plus, K.Key_Equal):
            c.change_zoom(+0.35)
        elif k in (K.Key_Minus, K.Key_Underscore):
            c.change_zoom(-0.35)
        elif k == K.Key_0:
            c.reset_view()
        elif k == K.Key_W:
            c.pan(0, +0.05)
        elif k == K.Key_S:
            c.pan(0, -0.05)
        elif k == K.Key_A:
            c.pan(+0.05, 0)
        elif k == K.Key_D:
            c.pan(-0.05, 0)
        else:
            super().keyPressEvent(e)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def closeEvent(self, e):
        self._hud_timer.stop()
        self.comp.shutdown()
        super().closeEvent(e)


def _name(path):
    import os
    return os.path.basename(path) if path else "—"
