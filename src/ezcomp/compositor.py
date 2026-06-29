"""The comparison engine: two libmpv instances rendered into offscreen FBOs and
combined by a fragment shader into one QOpenGLWidget."""
import os
import sys

from PySide6 import QtCore, QtGui
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtOpenGL import (
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLFramebufferObject,
    QOpenGLVertexArrayObject,
)
import mpv

from . import constants as C
from . import shaders


def make_player():
    return mpv.MPV(
        vo="libmpv",                 # required for the render API
        hwdec="auto-copy",
        keep_open="yes",
        pause="yes",
        # High-quality scaling by default (matches mpv/IINA gpu-hq look);
        # `scale` is swapped to nearest dynamically when magnified past 1:1
        # for pixel-accurate artifact inspection. See Compositor._apply_scalers.
        scale="ewa_lanczossharp",
        cscale="ewa_lanczossharp",
        dscale="mitchell",
        correct_downscaling="yes",
        linear_downscaling="yes",
        sigmoid_upscaling="yes",
        tone_mapping="bt.2390",      # HDR -> SDR via libplacebo
        osc="no",
        input_default_bindings="no",
        input_vo_keyboard="no",
        terminal="no",
    )


class Compositor(QOpenGLWidget):
    """Owns the two players, their render contexts, and the GL composite pass."""

    _wakeup = QtCore.Signal()
    state_changed = QtCore.Signal()   # emitted when something the HUD shows changes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

        self.players = [make_player(), make_player()]
        self.paths = [None, None]
        self._ctx = [None, None]
        self._fbo = [None, None]
        self._gl = None
        self._prog = None
        self._vao = None
        self._pending = None

        self.mode = C.MODE_A
        self.params = dict(C.DEFAULT_PARAMS)
        self.zoom = 0.0
        self.panx = 0.0
        self.pany = 0.0
        self.frame_index = 0
        self._fps_cache = None

        self._sync_timer = QtCore.QTimer(self)
        self._sync_timer.setInterval(400)
        self._sync_timer.timeout.connect(self._playback_resync)

        self._wakeup.connect(self.update, QtCore.Qt.QueuedConnection)

    # ---- GL lifecycle ----
    def initializeGL(self):
        self._gl = self.context().functions()
        self._proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)
        self._prog = QOpenGLShaderProgram(self)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Vertex, shaders.VERTEX)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Fragment, shaders.FRAGMENT)
        self._prog.link()
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()
        for i, p in enumerate(self.players):
            self._ctx[i] = mpv.MpvRenderContext(
                p, "opengl",
                opengl_init_params={"get_proc_address": self._proc_fn},
            )
            self._ctx[i].update_cb = self._wakeup.emit
        if self._pending:
            self.set_sources(*self._pending)
            self._pending = None

    def resizeGL(self, w, h):
        self._apply_scalers()

    def _ensure_fbos(self, w, h):
        for i in range(2):
            f = self._fbo[i]
            if f is None or f.width() != w or f.height() != h:
                self._fbo[i] = QOpenGLFramebufferObject(w, h)

    def paintGL(self):
        if self._ctx[0] is None:
            return
        r = self.devicePixelRatioF()
        w = max(1, int(self.width() * r))
        h = max(1, int(self.height() * r))
        self._ensure_fbos(w, h)

        for i in range(2):
            self._ctx[i].render(
                flip_y=True,
                opengl_fbo={"w": w, "h": h, "fbo": self._fbo[i].handle()},
            )

        gl = self._gl
        gl.glBindFramebuffer(C.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        gl.glViewport(0, 0, w, h)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(C.GL_COLOR_BUFFER_BIT)

        self._prog.bind()
        gl.glActiveTexture(C.GL_TEXTURE0)
        gl.glBindTexture(C.GL_TEXTURE_2D, self._fbo[0].texture())
        gl.glActiveTexture(C.GL_TEXTURE1)
        gl.glBindTexture(C.GL_TEXTURE_2D, self._fbo[1].texture())
        # Explicit glUniform* — QOpenGLShaderProgram.setUniformValue picks the
        # wrong overload for Python floats (uParam would silently never update).
        loc = self._prog.uniformLocation
        gl.glUniform1i(loc("texA"), 0)
        gl.glUniform1i(loc("texB"), 1)
        gl.glUniform1i(loc("uMode"), int(self.mode))
        gl.glUniform1f(loc("uParam"), float(self.params.get(self.mode, 0.0)))
        self._vao.bind()
        gl.glDrawArrays(C.GL_TRIANGLES, 0, 3)
        self._vao.release()
        self._prog.release()

    # ---- file loading ----
    def set_sources(self, path_a, path_b):
        """Load (or replace) the A and B videos. Safe to call before GL init."""
        if self._ctx[0] is None:
            self._pending = (path_a, path_b)
            return
        for i, path in enumerate((path_a, path_b)):
            if path:
                self.paths[i] = path
                self.players[i].play(path)
        self._fps_cache = None
        self.frame_index = 0
        QtCore.QTimer.singleShot(300, self._apply_scalers)  # once src height known
        self.state_changed.emit()

    # ---- sync / navigation ----
    def _both(self, fn):
        for p in self.players:
            try:
                fn(p)
            except Exception as e:  # noqa: BLE001
                print("mpv cmd error:", e, file=sys.stderr)

    def _fps(self):
        if not self._fps_cache:
            self._fps_cache = self.players[0].container_fps or (24000 / 1001)
        return self._fps_cache

    def _seek_both_to_index(self, idx):
        idx = max(0, idx)
        self.frame_index = idx
        t = (idx + 0.5) / self._fps()
        self._both(lambda p: p.command("seek", t, "absolute", "exact"))
        self.state_changed.emit()

    def _sync_index_from_master(self):
        fr = self.players[0].estimated_frame_number
        if fr is not None:
            self.frame_index = int(fr)

    def is_paused(self):
        return bool(self.players[0].pause)

    def play_pause(self):
        if not self.players[0].pause:
            self._both(lambda p: setattr(p, "pause", True))
            self._sync_timer.stop()
            QtCore.QTimer.singleShot(50, self._lock_after_pause)
        else:
            self._both(lambda p: setattr(p, "pause", False))
            self._sync_timer.start()
        self.state_changed.emit()

    def _lock_after_pause(self):
        self._sync_index_from_master()
        self._seek_both_to_index(self.frame_index)

    def _playback_resync(self):
        t0 = self.players[0].time_pos
        t1 = self.players[1].time_pos
        if t0 is None or t1 is None:
            return
        if abs(t0 - t1) > 1.5 / self._fps():
            self.players[1].command("seek", t0, "absolute", "exact")

    def frame_step(self, back=False):
        if not self.players[0].pause:
            self._both(lambda p: setattr(p, "pause", True))
            self._sync_timer.stop()
            self._sync_index_from_master()
        self._seek_both_to_index(self.frame_index + (-1 if back else 1))

    def seek(self, secs):
        if not self.players[0].pause:
            self._sync_index_from_master()
        self._seek_both_to_index(self.frame_index + round(secs * self._fps()))

    # ---- view ----
    def apply_view(self):
        self._both(lambda p: setattr(p, "video_zoom", self.zoom))
        self._both(lambda p: setattr(p, "video_pan_x", self.panx))
        self._both(lambda p: setattr(p, "video_pan_y", self.pany))
        self._apply_scalers()

    def _apply_scalers(self):
        """Use crisp nearest-neighbour only when magnified past native pixels;
        otherwise high-quality scaling (so the fit/fullscreen view stays sharp)."""
        try:
            src_h = self.players[0].height or 0
        except Exception:  # noqa: BLE001
            src_h = 0
        disp_h = self.height() * self.devicePixelRatioF() * (2 ** self.zoom)
        magnify = bool(src_h) and disp_h > src_h * 1.05
        self._both(lambda p: setattr(p, "scale",
                                     "nearest" if magnify else "ewa_lanczossharp"))

    def change_zoom(self, d):
        self.zoom = max(-2.0, min(6.0, self.zoom + d))
        self.apply_view()
        self.state_changed.emit()

    def pan(self, dx, dy):
        self.panx = max(-2.0, min(2.0, self.panx + dx))
        self.pany = max(-2.0, min(2.0, self.pany + dy))
        self.apply_view()

    def reset_view(self):
        self.zoom = self.panx = self.pany = 0.0
        self.apply_view()
        self.state_changed.emit()

    # ---- modes ----
    def set_mode(self, m):
        self.mode = m
        self.update()
        self.state_changed.emit()

    def toggle_ab(self):
        self.set_mode(C.MODE_B if self.mode == C.MODE_A else C.MODE_A)

    def adjust_param(self, direction):
        if self.mode == C.MODE_WIPE:
            self.params[C.MODE_WIPE] = _clamp(self.params[C.MODE_WIPE] + direction * 0.05, 0, 1)
        elif self.mode == C.MODE_DIFF:
            self.params[C.MODE_DIFF] = _clamp(self.params[C.MODE_DIFF] + direction * 2.0, 1, 40)
        elif self.mode == C.MODE_ONION:
            self.params[C.MODE_ONION] = _clamp(self.params[C.MODE_ONION] + direction * 0.1, 0, 1)
        self.update()
        self.state_changed.emit()

    # ---- input ----
    def wheelEvent(self, e):
        self.change_zoom(0.20 if e.angleDelta().y() > 0 else -0.20)

    def mouseMoveEvent(self, e):
        if self.mode == C.MODE_WIPE and self.width() > 0:
            self.params[C.MODE_WIPE] = _clamp(e.position().x() / self.width(), 0, 1)
            self.update()
            self.state_changed.emit()

    # ---- status for HUD ----
    def status(self):
        p = self.players[0]
        try:
            t = p.time_pos or 0.0
            fr = p.estimated_frame_number or 0
        except mpv.ShutdownError:
            return None
        return {
            "mode": self.mode,
            "param": self.params.get(self.mode),
            "time": t,
            "frame": fr,
            "zoom": 2 ** self.zoom,
            "paused": self.is_paused(),
            "a": os.path.basename(self.paths[0]) if self.paths[0] else "—",
            "b": os.path.basename(self.paths[1]) if self.paths[1] else "—",
        }

    # ---- screenshots ----
    def screenshot_source(self, idx, path):
        """Native-resolution screenshot of one source via mpv's own pipeline.
        Each source is captured at its own resolution (handles A/B of differing
        scales). Needs our GL context current for the render-API screenshot."""
        self.makeCurrent()
        try:
            self.players[idx].command("screenshot-to-file", path, "video")
        finally:
            self.doneCurrent()

    def grab_window(self):
        """As-displayed capture of the current composite (mode, zoom, letterbox)
        at window resolution."""
        return self.grabFramebuffer()

    def shutdown(self):
        self._sync_timer.stop()
        for i in range(2):
            if self._ctx[i] is not None:
                self._ctx[i].free()
                self._ctx[i] = None
            self.players[i].terminate()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _get_proc_address(_ctx, name):
    glctx = QtGui.QOpenGLContext.currentContext()
    if glctx is None:
        return 0
    addr = glctx.getProcAddress(bytes(name).decode("utf-8"))
    return int(addr) if addr else 0
