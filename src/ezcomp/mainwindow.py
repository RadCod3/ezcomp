"""Main window: Qt shell (toolbar, keys) hosting the EDR NSOpenGLView renderer.

Stage 1 of the EDR migration: native view embedded via createWindowContainer,
HUD shown in the title bar (Cocoa overlay HUD + mouse-wipe come next).
"""
import os
import sys
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import constants as C
from . import edr_render


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path_a=None, path_b=None):
        super().__init__()
        self.setWindowTitle("ezcomp")
        self.resize(1280, 720)

        self.engine = edr_render.Engine(
            on_state_change=self._refresh_title,
            on_colorspace_change=self._set_window_colorspace,
        )
        container, self.view = edr_render.make_container(self.engine, self)
        self.setCentralWidget(container)

        self.engine.edr_available = self._edr_available()
        self.engine.force_sdr = not self.engine.edr_available

        self._build_toolbar()
        self._sdr_action.setChecked(self.engine.force_sdr)
        self._cs_applied = False
        self._hud_timer = QtCore.QTimer(self)
        self._hud_timer.timeout.connect(self._refresh_title)
        self._hud_timer.start(150)

        if path_a or path_b:
            self.engine.set_sources(path_a, path_b)
            QtCore.QTimer.singleShot(800, self.engine.detect_hdr)

    def _edr_available(self):
        if sys.platform != "darwin":
            return False
        try:
            from AppKit import NSScreen
            s = NSScreen.mainScreen()
            return bool(s and
                        s.maximumPotentialExtendedDynamicRangeColorComponentValue() > 1.0)
        except Exception:  # noqa: BLE001
            return False

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
        act("A", lambda: self.engine.set_mode(C.MODE_A), "Show A (1)")
        act("B", lambda: self.engine.set_mode(C.MODE_B), "Show B (2)")
        act("Wipe", lambda: self.engine.set_mode(C.MODE_WIPE), "Split wipe (3)")
        act("Diff", lambda: self.engine.set_mode(C.MODE_DIFF), "Difference (4)")
        act("Onion", lambda: self.engine.set_mode(C.MODE_ONION), "Onion-skin (5)")
        tb.addSeparator()
        act("⏯", self.engine.play_pause, "Play/Pause (Space)")
        act("−", lambda: self.engine.change_zoom(-0.35), "Zoom out (-)")
        act("+", lambda: self.engine.change_zoom(+0.35), "Zoom in (+)")
        act("Reset", self.engine.reset_view, "Reset zoom/pan (0)")
        tb.addSeparator()
        self._sdr_action = act("Force SDR", self.toggle_force_sdr,
                               "Tonemap everything to SDR (H). HDR is otherwise "
                               "auto per source.")
        self._sdr_action.setCheckable(True)

    # ---- files ----
    def open_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select reference (A) then encode (B)", "",
            "Video (*.mkv *.mp4 *.mov *.m2ts *.ts *.webm *.avi);;All files (*)")
        if files:
            a = files[0]
            b = files[1] if len(files) >= 2 else self.engine.paths[1]
            self.engine.set_sources(a, b)
            QtCore.QTimer.singleShot(800, self.engine.detect_hdr)

    # ---- HDR / colorspace ----
    def toggle_force_sdr(self):
        self.engine.set_force_sdr(not self.engine.force_sdr)
        self._sdr_action.setChecked(self.engine.force_sdr)

    def _set_window_colorspace(self, hdr):
        if sys.platform != "darwin":
            return
        try:
            import objc
            from AppKit import NSColorSpace
            from Quartz import (
                CGColorSpaceCreateWithName, kCGColorSpaceITUR_709,
                kCGColorSpaceITUR_2100_PQ,
            )
            name = kCGColorSpaceITUR_2100_PQ if hdr else kCGColorSpaceITUR_709
            cs = NSColorSpace.alloc().initWithCGColorSpace_(
                CGColorSpaceCreateWithName(name))
            view = objc.objc_object(c_void_p=int(self.winId()))
            win = view.window()
            if win is not None:
                win.setColorSpace_(cs)
            # The GL drawable caches the colorspace from when it was created, so
            # changing the window colorspace alone leaves it "wonky" until a
            # resize. Detach/reattach the context's view to force the drawable to
            # be recreated with the new colorspace (programmatic resize trick).
            gl = self.view.openGLContext()
            if gl is not None:
                gl.clearDrawable()
                gl.setView_(self.view)
                gl.update()
            self.view.setOsdHdr_(hdr)
            self.view.setNeedsDisplay_(True)
        except Exception as ex:  # noqa: BLE001
            print("colorspace set failed:", ex, file=sys.stderr)

    # ---- screenshot (native per-source) ----
    def take_screenshot(self):
        s = self.engine.status()
        frame = s["frame"] if s else 0
        ts = time.strftime("%H%M%S")
        mode = self.engine.mode
        if mode == C.MODE_A:
            targets = [(0, "A")]
        elif mode == C.MODE_B:
            targets = [(1, "B")]
        else:
            targets = [(0, "A"), (1, "B")]
        outdir = os.path.expanduser("~/Desktop")
        if not os.path.isdir(outdir):
            outdir = os.path.expanduser("~")
        for idx, label in targets:
            name = f"ezcomp_{label}_f{frame}_{ts}.png"
            self.engine.screenshot_source(idx, os.path.join(outdir, name))

    # ---- HUD via title ----
    def _refresh_title(self):
        s = self.engine.status()
        if s is None:
            return
        pstr = f" {s['param']:.2f}" if s["param"] is not None else ""
        state = "paused" if s["paused"] else "playing"
        cs = "HDR" if s["hdr"] else "SDR"
        forced = " (forced)" if s["force_sdr"] else ""
        srcs = "".join(["H" if x else "S" for x in s["hdr_src"]])
        self.setWindowTitle(
            f"ezcomp [{cs}{forced}] — {C.MODE_NAMES[s['mode']]}{pstr} | {state} "
            f"| f{s['frame']} t{s['time']:.3f}s | zoom {s['zoom']:.2f}× "
            f"| A:{s['a']}({srcs[0]}) B:{s['b']}({srcs[1]})")
        self.view.updateOsd_(
            f"[{cs}{forced}]  {state}  mode {C.MODE_NAMES[s['mode']]}{pstr}\n"
            f"frame {s['frame']}   {C.timecode(s['time'])}   zoom {s['zoom']:.2f}×\n"
            f"A  {s['a']} ({srcs[0]})\nB  {s['b']} ({srcs[1]})")

    def showEvent(self, e):
        super().showEvent(e)
        if not self._cs_applied:
            self._cs_applied = True
            self.engine._apply_color()

    # ---- keys ----
    def keyPressEvent(self, e):
        K = QtCore.Qt
        k = e.key()
        g = self.engine
        if k in (K.Key_Q, K.Key_Escape):
            self.close()
        elif k == K.Key_O:
            self.open_files()
        elif k == K.Key_F:
            self.toggle_fullscreen()
        elif k == K.Key_H:
            self.toggle_force_sdr()
        elif k == K.Key_I:
            self.view.toggleOsd()
        elif k == K.Key_C:
            self.take_screenshot()
        elif k == K.Key_Space:
            g.play_pause()
        elif k == K.Key_Tab:
            g.toggle_ab()
        elif k == K.Key_1:
            g.set_mode(C.MODE_A)
        elif k == K.Key_2:
            g.set_mode(C.MODE_B)
        elif k == K.Key_3:
            g.set_mode(C.MODE_WIPE)
        elif k == K.Key_4:
            g.set_mode(C.MODE_DIFF)
        elif k == K.Key_5:
            g.set_mode(C.MODE_ONION)
        elif k == K.Key_BracketLeft:
            g.adjust_param(-1)
        elif k == K.Key_BracketRight:
            g.adjust_param(+1)
        elif k == K.Key_Period:
            g.frame_step(False)
        elif k == K.Key_Comma:
            g.frame_step(True)
        elif k == K.Key_Right:
            g.seek(2)
        elif k == K.Key_Left:
            g.seek(-2)
        elif k in (K.Key_Plus, K.Key_Equal):
            g.change_zoom(+0.35)
        elif k in (K.Key_Minus, K.Key_Underscore):
            g.change_zoom(-0.35)
        elif k == K.Key_0:
            g.reset_view()
        elif k == K.Key_W:
            g.pan(0, +0.05)
        elif k == K.Key_S:
            g.pan(0, -0.05)
        elif k == K.Key_A:
            g.pan(+0.05, 0)
        elif k == K.Key_D:
            g.pan(-0.05, 0)
        else:
            super().keyPressEvent(e)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def closeEvent(self, e):
        self._hud_timer.stop()
        self.engine.shutdown()
        super().closeEvent(e)
