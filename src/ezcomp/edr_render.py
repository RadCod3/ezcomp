"""EDR-capable renderer: libmpv -> float FBOs -> composite, drawn into a custom
NSOpenGLView whose window colorspace we control (BT.709 for SDR, PQ for HDR).

This replaces the QOpenGLWidget path, which on macOS cannot do wide-gamut color
or HDR (plain CALayer). Verified approach: float NSOpenGLView + window
colorspace tag + mpv output transfer matched to the tag.
"""
import ctypes
import sys
import time

import objc
from Cocoa import (
    NSOpenGLView, NSOpenGLPixelFormat, NSTimer, NSRunLoop, NSRunLoopCommonModes,
    NSMakeRect, NSTextField, NSSlider, NSColor, NSColorSpace, NSFont,
    NSViewMinYMargin, NSViewMaxXMargin, NSViewWidthSizable, NSViewMaxYMargin,
)
from Quartz import CGColorSpaceCreateWithName, kCGColorSpaceExtendedSRGB
from Cocoa import (
    NSOpenGLPFAColorFloat, NSOpenGLPFAColorSize, NSOpenGLPFADoubleBuffer,
    NSOpenGLPFAAccelerated, NSOpenGLPFAOpenGLProfile, NSOpenGLProfileVersion3_2Core,
)
from OpenGL import GL
from PySide6 import QtGui, QtWidgets
import mpv

from . import constants as C
from . import shaders

_GL = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")


def _get_proc_address(_ctx, name):
    try:
        fn = getattr(_GL, bytes(name).decode())
    except AttributeError:
        return 0
    return ctypes.cast(fn, ctypes.c_void_p).value or 0


def _make_player():
    return mpv.MPV(
        vo="libmpv", hwdec="auto-copy", keep_open="yes", pause="yes",
        scale="ewa_lanczossharp", cscale="ewa_lanczossharp", dscale="mitchell",
        correct_downscaling="yes", linear_downscaling="yes", sigmoid_upscaling="yes",
        tone_mapping="bt.2390", osc="no", input_default_bindings="no",
        input_vo_keyboard="no", terminal="no",
    )


class Engine:
    """Holds the two players, their render contexts, GL resources, and all
    rendering/compositing. UI-agnostic; the view drives render()."""

    def __init__(self, on_state_change=None, on_colorspace_change=None):
        self.players = [_make_player(), _make_player()]
        self.paths = [None, None]
        self._ctx = [None, None]
        self._fbo = [None, None]
        self._tex = [None, None]
        self._fbo_size = (0, 0)
        self._prog = None
        self._vao = None
        self._loc = {}
        self._inited = False
        self._pending = None
        self.on_state_change = on_state_change or (lambda: None)
        self.on_colorspace_change = on_colorspace_change or (lambda hdr: None)

        self.mode = C.MODE_A
        self.params = dict(C.DEFAULT_PARAMS)
        self.zoom = 0.0
        self.panx = 0.0
        self.pany = 0.0
        self.frame_index = 0
        self._frame_count_cache = None
        self.hdr_src = [False, False]   # per-source: is this file HDR?
        self.edr_available = True       # set by the UI from NSScreen
        self.force_sdr = False          # manual override / no-EDR fallback
        self._win_hdr = False           # current window colorspace state
        self._fps_cache = None

    # ---- GL setup (context must be current) ----
    def _init_gl(self):
        vs = GL.glCreateShader(GL.GL_VERTEX_SHADER)
        GL.glShaderSource(vs, shaders.VERTEX)
        GL.glCompileShader(vs)
        if not GL.glGetShaderiv(vs, GL.GL_COMPILE_STATUS):
            raise RuntimeError("vertex: " + GL.glGetShaderInfoLog(vs).decode())
        fs = GL.glCreateShader(GL.GL_FRAGMENT_SHADER)
        GL.glShaderSource(fs, shaders.FRAGMENT)
        GL.glCompileShader(fs)
        if not GL.glGetShaderiv(fs, GL.GL_COMPILE_STATUS):
            raise RuntimeError("fragment: " + GL.glGetShaderInfoLog(fs).decode())
        self._prog = GL.glCreateProgram()
        GL.glAttachShader(self._prog, vs)
        GL.glAttachShader(self._prog, fs)
        GL.glLinkProgram(self._prog)
        if not GL.glGetProgramiv(self._prog, GL.GL_LINK_STATUS):
            raise RuntimeError("link: " + GL.glGetProgramInfoLog(self._prog).decode())
        for u in ("texA", "texB", "uMode", "uParam"):
            self._loc[u] = GL.glGetUniformLocation(self._prog, u)
        self._vao = GL.glGenVertexArrays(1)

        for i, p in enumerate(self.players):
            self._proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)
            self._ctx[i] = mpv.MpvRenderContext(
                p, "opengl", opengl_init_params={"get_proc_address": self._proc_fn},
            )
            self._ctx[i].update_cb = self._on_mpv_update
        self._inited = True
        if self._pending:
            self.set_sources(*self._pending)
            self._pending = None

    def _make_fbo(self, w, h):
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA16F, w, h, 0,
                        GL.GL_RGBA, GL.GL_FLOAT, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, tex, 0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        return fbo, tex

    def _ensure_fbos(self, w, h):
        if self._fbo_size == (w, h) and all(self._fbo):
            return
        for i in range(2):
            if self._fbo[i]:
                GL.glDeleteFramebuffers(1, [self._fbo[i]])
                GL.glDeleteTextures(1, [self._tex[i]])
            self._fbo[i], self._tex[i] = self._make_fbo(w, h)
        self._fbo_size = (w, h)

    # ---- the per-frame render (called by the view, context current) ----
    def render(self, w, h):
        if not self._inited:
            self._init_gl()
        if w <= 0 or h <= 0:
            return
        self._ensure_fbos(w, h)
        for i in range(2):
            self._ctx[i].render(flip_y=True,
                                 opengl_fbo={"w": w, "h": h, "fbo": int(self._fbo[i])})
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glViewport(0, 0, w, h)
        GL.glClearColor(0.0, 0.0, 0.0, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glUseProgram(self._prog)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex[0])
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex[1])
        GL.glUniform1i(self._loc["texA"], 0)
        GL.glUniform1i(self._loc["texB"], 1)
        GL.glUniform1i(self._loc["uMode"], int(self.mode))
        GL.glUniform1f(self._loc["uParam"], float(self.params.get(self.mode, 0.0)))
        GL.glBindVertexArray(self._vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)

    def _on_mpv_update(self):
        v = getattr(self, "_view", None)
        if v is not None:
            v.performSelectorOnMainThread_withObject_waitUntilDone_(
                "setNeedsDisplay:", True, False)

    # ---- files ----
    def set_sources(self, path_a, path_b):
        if not self._inited:
            self._pending = (path_a, path_b)
            return
        for i, path in enumerate((path_a, path_b)):
            if path:
                self.paths[i] = path
                self.players[i].play(path)
        self._fps_cache = None
        self._frame_count_cache = None
        self.frame_index = 0
        self.on_state_change()

    # ---- sync / nav ----
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
        self.on_state_change()

    def _sync_index_from_master(self):
        fr = self.players[0].estimated_frame_number
        if fr is not None:
            self.frame_index = int(fr)

    def frame_count(self):
        if self._frame_count_cache:
            return self._frame_count_cache
        p = self.players[0]
        n = 0
        try:
            n = int(p.estimated_frame_count or 0)
        except Exception:  # noqa: BLE001
            n = 0
        if not n:
            try:
                dur = p.duration
                n = int(dur * self._fps()) if dur else 0
            except Exception:  # noqa: BLE001
                n = 0
        if n:
            self._frame_count_cache = n
        return n

    def seek_to_frame(self, frame):
        self._seek_both_to_index(int(frame))

    def is_paused(self):
        return bool(self.players[0].pause)

    def play_pause(self):
        if not self.players[0].pause:
            self._both(lambda p: setattr(p, "pause", True))
            self._sync_index_from_master()
            self._seek_both_to_index(self.frame_index)
        else:
            self._both(lambda p: setattr(p, "pause", False))
        self.on_state_change()

    def frame_step(self, back=False):
        if not self.players[0].pause:
            self._both(lambda p: setattr(p, "pause", True))
            self._sync_index_from_master()
        self._seek_both_to_index(self.frame_index + (-1 if back else 1))

    def seek(self, secs):
        if not self.players[0].pause:
            self._sync_index_from_master()
        self._seek_both_to_index(self.frame_index + round(secs * self._fps()))

    # ---- view transform ----
    def apply_view(self):
        self._both(lambda p: setattr(p, "video_zoom", self.zoom))
        self._both(lambda p: setattr(p, "video_pan_x", self.panx))
        self._both(lambda p: setattr(p, "video_pan_y", self.pany))

    def change_zoom(self, d):
        self.zoom = max(-2.0, min(6.0, self.zoom + d))
        self.apply_view()
        self.on_state_change()

    def pan(self, dx, dy):
        self.panx = max(-2.0, min(2.0, self.panx + dx))
        self.pany = max(-2.0, min(2.0, self.pany + dy))
        self.apply_view()

    def reset_view(self):
        self.zoom = self.panx = self.pany = 0.0
        self.apply_view()
        self.on_state_change()

    # ---- modes ----
    def set_mode(self, m):
        self.mode = m
        self._apply_color()
        self.on_state_change()

    def toggle_ab(self):
        self.set_mode(C.MODE_B if self.mode == C.MODE_A else C.MODE_A)

    def adjust_param(self, d):
        if self.mode == C.MODE_WIPE:
            self.params[C.MODE_WIPE] = _clamp(self.params[C.MODE_WIPE] + d * 0.05, 0, 1)
        elif self.mode == C.MODE_DIFF:
            self.params[C.MODE_DIFF] = _clamp(self.params[C.MODE_DIFF] + d * 2.0, 1, 40)
        elif self.mode == C.MODE_ONION:
            self.params[C.MODE_ONION] = _clamp(self.params[C.MODE_ONION] + d * 0.1, 0, 1)
        self.on_state_change()

    def set_wipe(self, x):
        self.params[C.MODE_WIPE] = _clamp(x, 0, 1)
        self.on_state_change()

    # ---- HDR / color management ----
    def detect_hdr(self):
        """Detect each source's dynamic range from its decoded color params."""
        for i, p in enumerate(self.players):
            if not self.paths[i]:
                continue
            try:
                vp = p.video_params or {}
            except Exception:  # noqa: BLE001
                vp = {}
            gamma = vp.get("gamma")
            prim = vp.get("primaries")
            self.hdr_src[i] = (gamma in ("pq", "st2084", "hlg", "arib-std-b67")
                               or prim == "bt.2020")
        self._apply_color()
        self.on_state_change()

    def _set_player_space(self, idx, hdr):
        p = self.players[idx]
        if hdr:
            p.target_trc = "pq"
            p.target_prim = "bt.2020"
            p.tone_mapping = "clip"
        else:
            p.target_trc = "auto"
            p.target_prim = "auto"
            p.tone_mapping = "bt.2390"

    def _apply_color(self):
        """Pick per-player output space + window colorspace for the current mode.
        Toggle modes render each source in its native space and the window
        follows the shown source; composite modes use one common space."""
        edr = self.edr_available and not self.force_sdr
        hdr = [edr and self.hdr_src[0], edr and self.hdr_src[1]]
        if self.mode == C.MODE_A:
            self._set_player_space(0, hdr[0])
            win = hdr[0]
        elif self.mode == C.MODE_B:
            self._set_player_space(1, hdr[1])
            win = hdr[1]
        else:  # composite: both must share one space
            common = hdr[0] or hdr[1]
            self._set_player_space(0, common)
            self._set_player_space(1, common)
            win = common
        self._win_hdr = win
        self.on_colorspace_change(win)

    def set_force_sdr(self, on):
        self.force_sdr = bool(on)
        self._apply_color()
        self.on_state_change()

    def status(self):
        p = self.players[0]
        try:
            t = p.time_pos or 0.0
            fr = p.estimated_frame_number or 0
        except mpv.ShutdownError:
            return None
        import os
        return {
            "mode": self.mode, "param": self.params.get(self.mode),
            "time": t, "frame": fr, "zoom": 2 ** self.zoom,
            "paused": self.is_paused(), "hdr": self._win_hdr,
            "force_sdr": self.force_sdr, "hdr_src": tuple(self.hdr_src),
            "a": os.path.basename(self.paths[0]) if self.paths[0] else "—",
            "b": os.path.basename(self.paths[1]) if self.paths[1] else "—",
        }

    def screenshot_source(self, idx, path):
        self.players[idx].command("screenshot-to-file", path, "video")

    def shutdown(self):
        for i in range(2):
            if self._ctx[i] is not None:
                self._ctx[i].free()
                self._ctx[i] = None
            self.players[i].terminate()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class MpvGLView(NSOpenGLView):
    def drawRect_(self, _rect):
        gl = self.openGLContext()
        gl.makeCurrentContext()
        b = self.convertRectToBacking_(self.bounds())
        try:
            self.engine.render(int(b.size.width), int(b.size.height))
        except Exception as e:  # noqa: BLE001
            print("render error:", e, file=sys.stderr)
        gl.flushBuffer()

    def tick_(self, _t):
        self.refreshScrubber()
        self.setNeedsDisplay_(True)

    def acceptsFirstResponder(self):
        return False  # let Qt keep keyboard focus

    # ---- mouse input (Qt can't deliver events to a native GL view) ----
    def scrollWheel_(self, e):
        dy = e.deltaY()
        if dy:
            self.engine.change_zoom(max(-0.4, min(0.4, dy * 0.05)))

    def mouseDown_(self, e):
        self._wipe_from_event(e)

    def mouseDragged_(self, e):
        self._wipe_from_event(e)

    def _wipe_from_event(self, e):
        if self.engine.mode != C.MODE_WIPE:
            return
        p = self.convertPoint_fromView_(e.locationInWindow(), None)
        w = self.bounds().size.width
        if w > 0:
            self.engine.set_wipe(p.x / w)

    # ---- OSD overlay ----
    def updateOsd_(self, text):
        if getattr(self, "osd", None) is not None:
            self.osd.setStringValue_(text)

    def toggleOsd(self):
        if getattr(self, "osd", None) is None:
            return
        hidden = not self.osd.isHidden()
        self.osd.setHidden_(hidden)
        if getattr(self, "scrubber", None) is not None:
            self.scrubber.setHidden_(hidden)

    # ---- scrubber (seek bar) ----
    def scrub_(self, sender):
        self._scrub_active = time.time()
        self.engine.seek_to_frame(sender.doubleValue())

    def refreshScrubber(self):
        s = getattr(self, "scrubber", None)
        if s is None or s.isHidden():
            return
        if time.time() - getattr(self, "_scrub_active", 0.0) < 0.3:
            return  # don't fight the user while dragging
        total = self.engine.frame_count()
        if total > 0:
            if s.maxValue() != total:
                s.setMaxValue_(float(total))
            s.setDoubleValue_(float(self.engine.frame_index))

    def setOsdHdr_(self, hdr):
        """In a PQ window, sRGB white (1.0) maps to only ~SDR-white nits and the
        OSD looks dim against bright HDR. Use an extended-range white (>1.0) so
        it maps to higher nits on the EDR display."""
        if getattr(self, "osd", None) is None:
            return
        if hdr:
            ext = NSColorSpace.alloc().initWithCGColorSpace_(
                CGColorSpaceCreateWithName(kCGColorSpaceExtendedSRGB))
            txt = NSColor.colorWithColorSpace_components_count_(
                ext, [3.0, 3.0, 3.0, 1.0], 4)
        else:
            txt = NSColor.colorWithCalibratedWhite_alpha_(1.0, 1.0)
        self.osd.setTextColor_(txt)
        self.osd.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55))


def make_view(engine):
    attrs = [
        NSOpenGLPFAColorFloat, NSOpenGLPFAColorSize, 64,
        NSOpenGLPFADoubleBuffer, NSOpenGLPFAAccelerated,
        NSOpenGLPFAOpenGLProfile, NSOpenGLProfileVersion3_2Core, 0,
    ]
    pf = NSOpenGLPixelFormat.alloc().initWithAttributes_(attrs)
    view = MpvGLView.alloc().initWithFrame_pixelFormat_(
        NSMakeRect(0, 0, 100, 100), pf)
    view.setWantsBestResolutionOpenGLSurface_(True)
    view.engine = engine
    engine._view = view

    osd = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 10, 720, 84))
    osd.setBezeled_(False)
    osd.setEditable_(False)
    osd.setSelectable_(False)
    osd.setDrawsBackground_(True)
    osd.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.5))
    osd.setTextColor_(NSColor.whiteColor())
    osd.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0))
    osd.setUsesSingleLineMode_(False)
    osd.cell().setWraps_(True)
    osd.setAutoresizingMask_(NSViewMinYMargin | NSViewMaxXMargin)
    view.addSubview_(osd)
    view.osd = osd

    scrub = NSSlider.alloc().initWithFrame_(NSMakeRect(10, 8, 80, 18))
    scrub.setMinValue_(0.0)
    scrub.setMaxValue_(1.0)
    scrub.setContinuous_(True)
    scrub.setRefusesFirstResponder_(True)  # keep keyboard focus on Qt
    scrub.setTarget_(view)
    scrub.setAction_("scrub:")
    scrub.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
    view.addSubview_(scrub)
    view.scrubber = scrub
    view._scrub_active = 0.0

    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / 60, view, "tick:", None, True)
    NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return view


def make_container(engine, parent):
    """Embed the NSOpenGLView in a Qt widget. Returns (container_widget, view)."""
    view = make_view(engine)
    ptr = objc.pyobjc_id(view)
    qwin = QtGui.QWindow.fromWinId(ptr)
    container = QtWidgets.QWidget.createWindowContainer(qwin, parent)
    return container, view
