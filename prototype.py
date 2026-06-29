"""
ezcomp — dual-source video comparison prototype.

Two libmpv instances each render into an offscreen FBO (libmpv OpenGL render
API). A fragment shader then composites the two textures into the on-screen
QOpenGLWidget, giving: A/B toggle, split-wipe, difference, and onion-skin.

Run:
    uv run python prototype.py [REFERENCE.mkv] [ENCODE.mkv]

Keys:
    Space   play/pause            Tab    toggle A<->B
    1 A   2 B   3 wipe   4 diff   5 onion
    [ / ]   adjust mode param (wipe pos / diff gain / onion mix)
    (in wipe mode, move the mouse to drag the divider)
    . / ,   frame step           Right/Left  seek 2s
    + / -   zoom                  W A S D     pan       0 reset
    Q       quit
"""
import os
import sys
import ctypes
import locale

for _p in ("/opt/homebrew/lib/libmpv.2.dylib", "/usr/local/lib/libmpv.2.dylib"):
    if os.path.exists(_p):
        ctypes.CDLL(_p, mode=ctypes.RTLD_GLOBAL)
        break
locale.setlocale(locale.LC_NUMERIC, "C")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402
from PySide6.QtOpenGL import (  # noqa: E402
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLFramebufferObject,
    QOpenGLVertexArrayObject,
)
import mpv  # noqa: E402

SAMPLES = "/Users/radith/Downloads/samples"
DEFAULT_A = os.path.join(SAMPLES, "DV-HDR.mkv")
DEFAULT_B = os.path.join(SAMPLES, "SDR.mkv")

# GL enums (QOpenGLFunctions doesn't expose them as constants)
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_BUFFER_BIT = 0x4000
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE1 = 0x84C1
GL_TRIANGLES = 0x0004

MODE_A, MODE_B, MODE_WIPE, MODE_DIFF, MODE_ONION = range(5)
MODE_NAMES = {MODE_A: "A", MODE_B: "B", MODE_WIPE: "wipe",
              MODE_DIFF: "diff", MODE_ONION: "onion"}

VERT = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2(float((gl_VertexID & 1) << 2) - 1.0,
                  float((gl_VertexID & 2) << 1) - 1.0);
    v_uv = (p + 1.0) * 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

FRAG = """
#version 330 core
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D texA;
uniform sampler2D texB;
uniform int  uMode;
uniform float uParam;   // wipe pos | diff gain | onion mix
void main() {
    vec3 a = texture(texA, v_uv).rgb;
    vec3 b = texture(texB, v_uv).rgb;
    vec3 c;
    if (uMode == 0) {
        c = a;
    } else if (uMode == 1) {
        c = b;
    } else if (uMode == 2) {                 // wipe
        c = (v_uv.x < uParam) ? a : b;
        if (abs(v_uv.x - uParam) < 0.0015) c = vec3(1.0, 0.85, 0.0);
    } else if (uMode == 3) {                 // difference, amplified
        c = clamp(abs(a - b) * uParam, 0.0, 1.0);
    } else {                                 // onion-skin
        c = mix(a, b, uParam);
    }
    fragColor = vec4(c, 1.0);
}
"""


def _get_proc_address(_ctx, name):
    glctx = QtGui.QOpenGLContext.currentContext()
    if glctx is None:
        return 0
    addr = glctx.getProcAddress(bytes(name).decode("utf-8"))
    return int(addr) if addr else 0


def _make_player(path):
    p = mpv.MPV(
        vo="libmpv",
        hwdec="auto-copy",
        keep_open="yes",
        pause="yes",
        scale="nearest",
        cscale="nearest",
        dscale="mitchell",
        tone_mapping="bt.2390",
        osc="no",
        input_default_bindings="no",
        input_vo_keyboard="no",
        terminal="no",
    )
    p._ezpath = path
    return p


class Compositor(QOpenGLWidget):
    _wakeup = QtCore.Signal()

    def __init__(self, path_a, path_b, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

        self.players = [_make_player(path_a), _make_player(path_b)]
        self._ctx = [None, None]
        self._fbo = [None, None]
        self._gl = None
        self._prog = None
        self._vao = None

        self.mode = MODE_A
        self.params = {MODE_WIPE: 0.5, MODE_DIFF: 12.0, MODE_ONION: 0.5}
        self.zoom = 0.0
        self.panx = 0.0
        self.pany = 0.0
        self.frame_index = 0
        self._fps_cache = None
        self._dump = False

        # best-effort drift correction during native playback
        self._sync_timer = QtCore.QTimer(self)
        self._sync_timer.setInterval(400)
        self._sync_timer.timeout.connect(self._playback_resync)

        self._wakeup.connect(self.update, QtCore.Qt.QueuedConnection)

    # ---- GL lifecycle ----
    def initializeGL(self):
        self._gl = self.context().functions()
        self._proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)
        self._prog = QOpenGLShaderProgram(self)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Vertex, VERT)
        self._prog.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAG)
        self._prog.link()
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()

        for i, p in enumerate(self.players):
            self._ctx[i] = mpv.MpvRenderContext(
                p, "opengl",
                opengl_init_params={"get_proc_address": self._proc_fn},
            )
            self._ctx[i].update_cb = self._wakeup.emit
            p.play(p._ezpath)

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

        # 1) render each mpv into its own FBO
        for i in range(2):
            self._ctx[i].render(
                flip_y=True,
                opengl_fbo={"w": w, "h": h, "fbo": self._fbo[i].handle()},
            )

        # 2) composite the two textures to the screen
        gl = self._gl
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
        gl.glViewport(0, 0, w, h)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        if self._dump:
            self._dump = False
            self._fbo[0].toImage().save("/tmp/ezcomp_A.png")
            self._fbo[1].toImage().save("/tmp/ezcomp_B.png")
            print("dumped FBOs to /tmp/ezcomp_{A,B}.png", file=sys.stderr)

        self._prog.bind()
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self._fbo[0].texture())
        gl.glActiveTexture(GL_TEXTURE1)
        gl.glBindTexture(GL_TEXTURE_2D, self._fbo[1].texture())
        # Use explicit glUniform* — QOpenGLShaderProgram.setUniformValue picks
        # the wrong overload for Python floats, so uParam never updated.
        loc = self._prog.uniformLocation
        gl.glUniform1i(loc("texA"), 0)
        gl.glUniform1i(loc("texB"), 1)
        gl.glUniform1i(loc("uMode"), int(self.mode))
        gl.glUniform1f(loc("uParam"), float(self.params.get(self.mode, 0.0)))
        self._vao.bind()
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        self._vao.release()
        self._prog.release()

    # ---- control ----
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
        """Frame-locked navigation: both sources jump to the SAME absolute
        timestamp, so they can never desync."""
        idx = max(0, idx)
        self.frame_index = idx
        t = (idx + 0.5) / self._fps()  # half-frame offset lands inside frame
        self._both(lambda p: p.command("seek", t, "absolute", "exact"))

    def _sync_index_from_master(self):
        fr = self.players[0].estimated_frame_number
        if fr is not None:
            self.frame_index = int(fr)

    def play_pause(self):
        if not self.players[0].pause:          # -> pause
            self._both(lambda p: setattr(p, "pause", True))
            self._sync_timer.stop()
            QtCore.QTimer.singleShot(50, self._lock_after_pause)
        else:                                  # -> play
            self._both(lambda p: setattr(p, "pause", False))
            self._sync_timer.start()

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

    def wheelEvent(self, e):
        self.change_zoom(0.20 if e.angleDelta().y() > 0 else -0.20)

    def apply_view(self):
        self._both(lambda p: setattr(p, "video_zoom", self.zoom))
        self._both(lambda p: setattr(p, "video_pan_x", self.panx))
        self._both(lambda p: setattr(p, "video_pan_y", self.pany))

    def change_zoom(self, d):
        self.zoom = max(-2.0, min(6.0, self.zoom + d))
        self.apply_view()

    def pan(self, dx, dy):
        self.panx = max(-2.0, min(2.0, self.panx + dx))
        self.pany = max(-2.0, min(2.0, self.pany + dy))
        self.apply_view()

    def reset_view(self):
        self.zoom = self.panx = self.pany = 0.0
        self.apply_view()

    def set_mode(self, m):
        self.mode = m
        self.update()

    def adjust_param(self, d):
        if self.mode == MODE_WIPE:
            self.params[MODE_WIPE] = max(0.0, min(1.0, self.params[MODE_WIPE] + d * 0.05))
        elif self.mode == MODE_DIFF:
            self.params[MODE_DIFF] = max(1.0, min(40.0, self.params[MODE_DIFF] + d * 2.0))
        elif self.mode == MODE_ONION:
            self.params[MODE_ONION] = max(0.0, min(1.0, self.params[MODE_ONION] + d * 0.1))
        self.update()

    def mouseMoveEvent(self, e):
        if self.mode == MODE_WIPE and self.width() > 0:
            self.params[MODE_WIPE] = max(0.0, min(1.0, e.position().x() / self.width()))
            self.update()

    def shutdown(self):
        self._sync_timer.stop()
        for i in range(2):
            if self._ctx[i] is not None:
                self._ctx[i].free()
                self._ctx[i] = None
            self.players[i].terminate()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path_a, path_b):
        super().__init__()
        self.setWindowTitle("ezcomp prototype")
        self.resize(1280, 640)
        self.comp = Compositor(path_a, path_b, self)
        self.setCentralWidget(self.comp)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._title)
        self._timer.start(150)

    def _title(self):
        c = self.comp
        p = c.players[0]
        try:
            t = p.time_pos or 0.0
            fr = p.estimated_frame_number
        except mpv.ShutdownError:
            return
        param = c.params.get(c.mode)
        ptxt = f" {param:.2f}" if param is not None else ""
        state = "paused" if p.pause else "playing"
        self.setWindowTitle(
            f"ezcomp — mode={MODE_NAMES[c.mode]}{ptxt} | {state} "
            f"| t={t:7.3f}s frame={fr} | zoom={2 ** c.zoom:.2f}x"
        )

    def keyPressEvent(self, e):
        K = QtCore.Qt
        k = e.key()
        c = self.comp
        if k in (K.Key_Q, K.Key_Escape):
            self.close()
        elif k == K.Key_Space:
            c.play_pause()
        elif k == K.Key_Tab:
            c.set_mode(MODE_B if c.mode == MODE_A else MODE_A)
        elif k == K.Key_1:
            c.set_mode(MODE_A)
        elif k == K.Key_2:
            c.set_mode(MODE_B)
        elif k == K.Key_3:
            c.set_mode(MODE_WIPE)
        elif k == K.Key_4:
            c.set_mode(MODE_DIFF)
        elif k == K.Key_5:
            c.set_mode(MODE_ONION)
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
        elif k == K.Key_P:
            c._dump = True
            c.update()
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

    def closeEvent(self, e):
        self._timer.stop()
        self.comp.shutdown()
        super().closeEvent(e)


def main():
    a = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_A
    b = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_B
    for f in (a, b):
        if not os.path.exists(f):
            sys.exit(f"file not found: {f}")

    fmt = QtGui.QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)

    app = QtWidgets.QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")
    win = MainWindow(a, b)
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
