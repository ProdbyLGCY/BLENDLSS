"""Live DLSS 5 in the 3D viewport.

DLSS 5 runs on the viewport image Blender itself has just drawn - no second
render - so it costs the same in Solid, Material Preview, EEVEE and Cycles:

  1. In the viewport's own draw callback (POST_PIXEL) we note that the
     viewport redrew and, in the default mode, copy its overlay layer
     (grid, outlines, gizmos, camera frame...).
  2. Once Blender has put the whole window together, a window-level callback
     reads the finished viewport pixels, removes the overlay layer from them
     (default mode) and scales them to the DLSS resolution, runs DLSS 5, and
     draws the result over the viewport - with the untouched overlay layer
     back on top. Header, toolbar and sidebar are drawn after that, so they
     stay crisp.

Compatibility mode skips the overlay separation and runs DLSS 5 on the
viewport exactly as displayed (overlays included).

Work is only done when the viewport actually redrew (orbit, EEVEE/Cycles
samples, edits) or a DLSS setting changed; otherwise the last result is
redrawn. Each viewport keeps its own DLSS history.
"""

import collections
import time
import zlib

import blf
import bpy
import gpu
import numpy as np
from bpy.app.handlers import persistent

from . import engine, node as dnode, prefs

_handle = None           # POST_PIXEL handler (per viewport)
_handle_view = None      # POST_VIEW handler (depth)
_timer_on = False
USE_GUIDES = True        # viewport depth + camera motion (tests flip this)
_cursor = None           # window-level callback
_states = {}             # region pointer -> _State
_busy = False
_encode = {}             # window pointer -> framebuffer wants linear output (probed once)
_scene_rev = [0]         # bumped on every scene change / frame change


class _State:
    def __init__(self, stream):
        self.stream = stream
        self.active = False
        self.win = 0
        self.rect = None            # (x, y, w, h) in window pixels
        self.post_seq = 0           # bumped every time the viewport redraws
        self.done_seq = -1
        self.view = None            # view matrices at the last viewport redraw
        self.done_view = None
        self.done_rev = -1
        self.done_settings = None
        self.compat = False
        self.use_ovl = False
        self.ovl_seq = -1
        self.ovl_tex = None
        self.tex = None
        self.tex_size = None
        self.hash = None
        self.bufs = {}
        self.ms = 0.0
        self.error = ""
        self.count = 0              # DLSS evaluations
        self.paints = 0             # window-level draws
        self.stage = {}
        self.last_paint = 0.0
        self.fallback_draws = 0
        self.area_ptr = 0
        self.post_time = 0.0
        self.paint_seq = -1         # post_seq the window callback last saw
        self.contam = False         # our DLSS image is drawn into the viewport's own buffer
        self.want_contam = False
        self.depth = None           # viewport depth (h, w) float32 from POST_VIEW
        self.depth_seq = -1
        self.vp = None              # projection * view (row-major 16 floats) at that redraw
        self.done_vp = None
        self.guided = False
        self.eval_times = collections.deque(maxlen=240)
        self.last_tm = {}


def _state_for(region):
    k = region.as_pointer()
    st = _states.get(k)
    if st is None:
        st = _states[k] = _State(len(_states) + 1)
    return st


# --------------------------------------------------------------------------
# Buffers / textures. Images go to the GPU "packed": each RGBA8 pixel's 4
# bytes are the bits of one R32F texel (a plain memcpy); the shader unpacks.
# --------------------------------------------------------------------------

def _packed_buffer(st, name, w, h):
    b = st.bufs.get(name)
    if b is None or b[0] != (w, h):
        buf = gpu.types.Buffer("FLOAT", w * h)
        view = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        b = st.bufs[name] = ((w, h), buf, view)
    return b[1], b[2]


def _packed_texture(buf, w, h):
    return gpu.types.GPUTexture((w, h), format="R32F", data=buf)


def _read_into(view, x, y, w, h):
    """Read w x h RGBA8 pixels at (x, y) of the bound framebuffer into a uint8 view."""
    fb = gpu.state.active_framebuffer_get()
    buf = fb.read_color(x, y, w, h, 4, 0, "UBYTE")
    buf.dimensions = w * h * 4
    try:
        src = np.frombuffer(buf, dtype=np.uint8)
    except Exception:  # noqa: BLE001
        src = np.array(buf.to_list(), dtype=np.uint8)
    np.copyto(view.reshape(-1), src)
    return view


_shader = None
_batch = None

_VERT = """
void main() {
  gl_Position = vec4(pos, 0.0, 1.0);
}
"""

_FRAG = """
vec4 texel(sampler2D t, ivec2 p, ivec2 size) {
  p = clamp(p, ivec2(0), size - 1);
  uint u = floatBitsToUint(texelFetch(t, p, 0).r);
  return vec4(float(u & 0xFFu), float((u >> 8u) & 0xFFu), float((u >> 16u) & 0xFFu), float(u >> 24u)) / 255.0;
}
vec3 srgb_to_linear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}
void main() {
  if (use_overlay < 0) {           /* calibration: plain mid grey */
    FragColor = vec4(0.5, 0.5, 0.5, 1.0);
    return;
  }
  vec2 px = gl_FragCoord.xy - vec2(region_origin);
  vec2 sp = (px + 0.5) * vec2(scene_size) / vec2(region_size) - 0.5;
  ivec2 i = ivec2(floor(sp));
  vec2 f = sp - floor(sp);
  vec3 a = texel(scene_tex, i, scene_size).rgb;
  vec3 b = texel(scene_tex, i + ivec2(1, 0), scene_size).rgb;
  vec3 c = texel(scene_tex, i + ivec2(0, 1), scene_size).rgb;
  vec3 d = texel(scene_tex, i + ivec2(1, 1), scene_size).rgb;
  vec3 col = mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
  if (use_overlay > 0) {
    vec4 o = texel(overlay_tex, ivec2(px), region_size);
    col = mix(col, o.rgb, o.a);
  }
  /* Our pixels are display-encoded already; if the target sRGB-encodes on
     write, hand it linear values so they come back out 1:1. */
  FragColor = vec4(linear_out != 0 ? srgb_to_linear(col) : col, 1.0);
}
"""


def _get_shader():
    global _shader, _batch
    if _shader is None:
        from gpu_extras.batch import batch_for_shader
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("IVEC2", "scene_size")
        info.push_constant("IVEC2", "region_size")
        info.push_constant("IVEC2", "region_origin")
        info.push_constant("INT", "use_overlay")
        info.push_constant("INT", "linear_out")
        info.sampler(0, "FLOAT_2D", "scene_tex")
        info.sampler(1, "FLOAT_2D", "overlay_tex")
        info.vertex_in(0, "VEC2", "pos")
        info.fragment_out(0, "VEC4", "FragColor")
        info.vertex_source(_VERT)
        info.fragment_source(_FRAG)
        _shader = gpu.shader.create_from_info(info)
        _batch = batch_for_shader(_shader, "TRI_FAN", {"pos": ((-1, -1), (1, -1), (1, 1), (-1, 1))})
    return _shader, _batch


def _draw_image(st, origin, size, linear_out):
    shader, batch = _get_shader()
    gpu.state.blend_set("NONE")
    shader.bind()
    shader.uniform_int("scene_size", st.tex_size)
    shader.uniform_int("region_size", size)
    shader.uniform_int("region_origin", origin)
    use_ovl = st.use_ovl and st.ovl_tex is not None
    shader.uniform_int("use_overlay", 1 if use_ovl else 0)
    shader.uniform_int("linear_out", 1 if linear_out else 0)
    shader.uniform_sampler("scene_tex", st.tex)
    shader.uniform_sampler("overlay_tex", st.ovl_tex if use_ovl else st.tex)
    batch.draw(shader)


def _label_at(x, y, w, text, color=(1.0, 1.0, 1.0, 0.85)):
    blf.size(0, 12)
    blf.color(0, *color)
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    tw, _th = blf.dimensions(0, text)
    blf.position(0, x + max(12, w - tw - 16), y + 12, 0)
    blf.draw(0, text)
    blf.disable(0, blf.SHADOW)


def _wanted(context):
    p = prefs.get_prefs()
    if p is None or not p.viewport_live:
        return None
    space = context.space_data
    if space is None or space.type != "VIEW_3D":
        return None
    mode = getattr(space.shading, "use_compositor", "DISABLED")
    if mode == "DISABLED":
        return None
    rv3d = context.region_data
    if mode == "CAMERA" and (rv3d is None or rv3d.view_perspective != "CAMERA"):
        return None
    return dnode.active_node(context.scene)


def _view_key(rv3d):
    return (tuple(v for row in rv3d.view_matrix for v in row),
            tuple(v for row in rv3d.window_matrix for v in row))


# --------------------------------------------------------------------------
# 0. Viewport callback (POST_VIEW): the depth buffer, for DLSS depth + motion
# --------------------------------------------------------------------------

def draw_view():
    context = bpy.context
    region = context.region
    st = _states.get(region.as_pointer())
    if st is None or not st.active:
        return
    if _wanted(context) is None:
        return
    w, h = region.width, region.height
    try:
        fb = gpu.state.active_framebuffer_get()
        buf = fb.read_depth(0, 0, w, h)
        buf.dimensions = w * h
        d = np.frombuffer(buf, dtype=np.float32)
        if st.depth is None or st.depth.shape != (h, w):
            st.depth = np.empty((h, w), np.float32)
        np.copyto(st.depth.reshape(-1), d)
        rv3d = context.region_data
        m = rv3d.window_matrix @ rv3d.view_matrix
        st.vp = np.array([v for row in m for v in row], np.float32)
        st.depth_seq = st.post_seq + 1          # belongs to the POST_PIXEL that follows
    except Exception as ex:  # noqa: BLE001
        st.depth_seq = -1
        st.error = f"depth: {ex}"


# --------------------------------------------------------------------------
# 1. Viewport callback: remember that it redrew, grab the overlay layer
# --------------------------------------------------------------------------

def draw():
    context = bpy.context
    region = context.region
    node = _wanted(context)
    if node is None:
        st = _states.get(region.as_pointer())
        if st is not None:
            st.active = False
        return
    w, h = region.width, region.height
    if w < 32 or h < 32:
        return
    _ensure_cursor()
    st = _state_for(region)
    p = prefs.get_prefs()
    st.active = True
    st.win = context.window.as_pointer()
    st.rect = (region.x, region.y, w, h)
    st.view = _view_key(context.region_data)
    st.compat = bool(p and p.viewport_compat)
    st.use_ovl = (not st.compat) and context.space_data.overlay.show_overlays
    st.area_ptr = context.area.as_pointer()
    st.post_seq += 1
    st.post_time = time.perf_counter()
    try:
        if st.use_ovl:
            _obuf, oview = _packed_buffer(st, "overlay", w, h)
            _read_into(oview, 0, 0, w, h)
    except Exception as ex:  # noqa: BLE001
        st.error = f"overlay: {ex}"
        st.use_ovl = False

    # Blender only runs the window-level callback while the window has an
    # active region - not while the pointer is on an area border, another app
    # or monitor. Then the plain viewport would flash through. So whenever the
    # view is idle (or the window callback missed the last redraw), the last
    # DLSS 5 frame is also drawn into the viewport's own buffer. The window
    # callback knows not to capture such a frame.
    missed = st.paint_seq < st.post_seq - 1
    usable = (st.tex is not None and st.tex_size is not None and st.done_view == st.view
              and st.done_rev == _scene_rev[0])
    st.contam = False
    if usable and (st.want_contam or missed):
        try:
            if st.use_ovl:
                obuf, _ov = _packed_buffer(st, "overlay", w, h)
                st.ovl_tex = _packed_texture(obuf, w, h)
                st.ovl_seq = st.post_seq
            _draw_image(st, (0, 0), (w, h), True)   # the viewport's overlay buffer is sRGB
            st.contam = True
            st.fallback_draws += 1
        except Exception:  # noqa: BLE001
            pass
    st.want_contam = False


# --------------------------------------------------------------------------
# 2. Window callback: read the finished viewport, DLSS 5, draw it back
# --------------------------------------------------------------------------

def _find_region(win, ptr):
    for area in win.screen.areas:
        if area.type != "VIEW_3D":
            continue
        for r in area.regions:
            if r.as_pointer() == ptr:
                return r
    return None


def _linear_out(win_ptr, x, y):
    """Does the window framebuffer sRGB-encode what we draw? Probed once per window
    by drawing mid grey into one pixel (which we then draw over) and reading it back."""
    v = _encode.get(win_ptr)
    if v is not None:
        return v
    shader, batch = _get_shader()
    gpu.state.scissor_set(x, y, 1, 1)
    shader.bind()
    shader.uniform_int("use_overlay", -1)
    shader.uniform_int("linear_out", 0)
    shader.uniform_int("region_origin", (0, 0))
    shader.uniform_int("region_size", (1, 1))
    shader.uniform_int("scene_size", (1, 1))
    dummy = gpu.types.GPUTexture((1, 1), format="R32F")
    shader.uniform_sampler("scene_tex", dummy)
    shader.uniform_sampler("overlay_tex", dummy)
    batch.draw(shader)
    px = gpu.state.active_framebuffer_get().read_color(x, y, 1, 1, 4, 0, "UBYTE")
    px.dimensions = 4
    v = _encode[win_ptr] = int(px[0]) > 160     # ~128 = stored as-is, ~188 = sRGB-encoded
    return v


def _process(st, backend, settings, skey, x, y, w, h, sw, sh):
    tm = {}
    t0 = time.perf_counter()
    _cbuf, cap = _packed_buffer(st, "capture", w, h)
    _read_into(cap, x, y, w, h)
    tm["read"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    _ibuf, inp = _packed_buffer(st, "scene_in", sw, sh)
    ovl = _packed_buffer(st, "overlay", w, h)[1] if st.use_ovl else None
    backend.prepare(cap, inp, ovl)
    tm["prepare"] = time.perf_counter() - t1

    rev_changed = st.done_rev != _scene_rev[0]
    st.done_seq = st.post_seq
    st.done_rev = _scene_rev[0]
    hsh = zlib.crc32(inp[::3, ::3].tobytes())
    if hsh == st.hash and skey == st.done_settings and st.tex is not None:
        st.done_view = st.view
        return                                   # same pixels, same settings: keep the last result

    # Depth + camera motion from this redraw's depth buffer: DLSS 5 can then keep
    # its history while you orbit instead of starting over every frame.
    depth = motion = None
    p = prefs.get_prefs()
    if USE_GUIDES and (p is None or p.use_guides) and st.depth is not None and st.depth_seq == st.post_seq and st.vp is not None:
        t1 = time.perf_counter()
        try:
            dout = st.bufs.get("depth_out")
            if dout is None or dout.shape != (sh, sw):
                dout = st.bufs["depth_out"] = np.empty((sh, sw), np.float32)
                st.bufs["motion_out"] = np.empty((sh, sw, 2), np.float32)
            mout = st.bufs["motion_out"]
            inv = np.linalg.inv(st.vp.reshape(4, 4).astype(np.float64)).astype(np.float32).reshape(-1)
            prev = st.done_vp if st.done_vp is not None else st.vp
            backend.camera_motion(st.depth, inv, prev, dout, mout)
            depth, motion = dout, mout
        except Exception as ex:  # noqa: BLE001
            st.error = f"motion: {ex}"
        tm["motion"] = time.perf_counter() - t1
    st.guided = depth is not None
    if depth is not None:
        # With motion vectors only real cuts reset the history.
        reset = st.tex is None or st.tex_size != (sw, sh) or skey != st.done_settings or rev_changed
    else:
        reset = st.done_view != st.view or st.tex_size != (sw, sh)
    t1 = time.perf_counter()
    dbuf, dview = _packed_buffer(st, "scene_out", sw, sh)
    backend.process_into(inp, dview, settings, reset=reset, stream=st.stream, depth=depth, motion=motion)
    tm["dlss"] = time.perf_counter() - t1
    st.done_vp = st.vp
    t1 = time.perf_counter()
    st.tex = _packed_texture(dbuf, sw, sh)
    st.tex_size = (sw, sh)
    tm["upload"] = time.perf_counter() - t1
    st.hash, st.done_settings, st.done_view = hsh, skey, st.view
    st.ms = (time.perf_counter() - t0) * 1000.0
    st.count += 1
    st.eval_times.append(time.perf_counter())
    st.last_tm = {k_: v_ * 1000.0 for k_, v_ in tm.items()}
    for k_, v_ in tm.items():
        st.stage[k_] = st.stage.get(k_, 0.0) + v_ * 1000.0


def _paint():
    global _busy
    if _busy or not _states:
        return
    context = bpy.context
    win = context.window
    if win is None:
        return
    wptr = win.as_pointer()
    todo = [(k, st) for k, st in _states.items() if st.active and st.win == wptr and st.rect]
    if not todo:
        return
    _busy = True
    try:
        backend = engine.get_backend()
        if not backend.is_nvidia:
            return
        node = dnode.active_node(context.scene)
        p = prefs.get_prefs()
        scale = float(p.viewport_scale) if p else 0.75
        gpu.state.scissor_test_set(True)
        for k, st in todo:
            region = _find_region(win, k)
            if region is None or node is None:
                st.active = False
                continue
            x, y, w, h = region.x, region.y, region.width, region.height
            if (w, h) != st.rect[2:]:
                continue                     # being resized; the viewport redraws next
            try:
                sw, sh = max(32, int(w * scale)), max(32, int(h * scale))
                settings = dnode.read_settings(node)
                skey = (tuple(sorted(settings.items())), sw, sh, st.compat, st.use_ovl)
                if st.contam:
                    # The viewport buffer holds our own image: nothing new to capture.
                    if skey != st.done_settings:
                        st.want_contam = False
                        _tag_redraw(win, st)       # settings moved: get a clean frame
                elif st.post_seq != st.done_seq or skey != st.done_settings or st.tex is None:
                    _process(st, backend, settings, skey, x, y, w, h, sw, sh)
                if st.use_ovl and st.ovl_seq != st.post_seq:
                    obuf, _ov = _packed_buffer(st, "overlay", w, h)
                    st.ovl_tex = _packed_texture(obuf, w, h)
                    st.ovl_seq = st.post_seq
                if st.tex is None:
                    continue
                linear = _linear_out(wptr, x, y)
                gpu.state.scissor_set(x, y, w, h)
                _draw_image(st, (x, y), (w, h), linear)
                if p is not None and p.show_stats:
                    _readout(st, x, y, w, h)
            except Exception as ex:  # noqa: BLE001
                st.error = str(ex)
                st.done_seq = -1
                gpu.state.scissor_set(x, y, w, h)
                _label_at(x, y, w, f"DLSS 5 error: {ex}"[:140], (1, 0.4, 0.3, 0.9))
            st.paints += 1
            st.paint_seq = st.post_seq
            st.last_paint = time.perf_counter()
    except Exception as ex:  # noqa: BLE001
        print("[DLSS 5] viewport:", ex)
    finally:
        gpu.state.blend_set("NONE")
        gpu.state.scissor_test_set(False)
        _busy = False


def _readout(st, x, y, w, h):
    """Performance readout (Preferences / sidebar: Performance Readout)."""
    now = time.perf_counter()
    fps = sum(1 for t in st.eval_times if now - t < 1.0)
    tm = st.last_tm
    parts = [f"DLSS 5   {fps} fps", f"{st.ms:.1f} ms"]
    detail = "  ".join(f"{k} {tm[k]:.1f}" for k in ("read", "prepare", "motion", "dlss", "upload") if k in tm)
    lines = [
        "   ".join(parts) + (f"   {st.tex_size[0]}x{st.tex_size[1]}" if st.tex_size else ""),
        detail,
        ("depth + motion" if st.guided else "no depth") + ("   compatibility mode" if st.compat else ""),
    ]
    lines = [ln for ln in lines if ln]
    for i, line in enumerate(reversed(lines)):          # bottom-up
        top = i == len(lines) - 1
        _label_at(x, y + i * 16, w, line, (0.46, 0.85, 0.0, 1.0) if top else (1, 1, 1, 0.85))


def _tag_redraw(win, st):
    for area in win.screen.areas:
        if area.as_pointer() == st.area_ptr:
            area.tag_redraw()
            return


def _idle_tick():
    """Keeps the last DLSS 5 frame in the viewport's own buffer when idle, and
    catches redraws the window callback missed (pointer on an area border...)."""
    global _timer_on
    if not _states:
        _timer_on = False
        return None
    now = time.perf_counter()
    try:
        wm = bpy.context.window_manager
        wins = {w.as_pointer(): w for w in wm.windows} if wm else {}
        for st in _states.values():
            if not st.active or st.contam or st.tex is None or st.want_contam:
                continue
            win = wins.get(st.win)
            if win is None:
                continue
            idle = st.paint_seq == st.post_seq and now - st.post_time > 0.15
            missed = st.paint_seq != st.post_seq and now - st.post_time > 0.05
            if (idle or missed) and st.done_view == st.view and st.done_rev == _scene_rev[0]:
                st.want_contam = True
                _tag_redraw(win, st)
    except Exception:  # noqa: BLE001
        pass
    return 0.05


def _paint_cb(*_args):
    _paint()


def _ensure_cursor():
    global _cursor, _timer_on
    if _cursor is None:
        wm = bpy.context.window_manager
        if wm is not None:
            _cursor = wm.draw_cursor_add(_paint_cb, ())
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_idle_tick, first_interval=0.1)


@persistent
def _on_change(*_args):
    _scene_rev[0] += 1


@persistent
def _on_load(*_args):
    _states.clear()
    _encode.clear()


def stats():
    return {k: {"ms": s.ms, "evals": s.count, "paints": s.paints, "posts": s.post_seq,
                "fallback": s.fallback_draws, "stage_ms": dict(s.stage), "error": s.error,
                "compat": s.compat, "size": s.tex_size, "guided": s.guided} for k, s in _states.items()}


def register():
    global _handle, _handle_view
    if _handle is None and not bpy.app.background:
        _handle_view = bpy.types.SpaceView3D.draw_handler_add(draw_view, (), "WINDOW", "POST_VIEW")
        _handle = bpy.types.SpaceView3D.draw_handler_add(draw, (), "WINDOW", "POST_PIXEL")
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    for lst in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.frame_change_post):
        if _on_change not in lst:
            lst.append(_on_change)


def unregister():
    global _handle, _handle_view, _cursor, _timer_on
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    if _handle_view is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle_view, "WINDOW")
        _handle_view = None
    if bpy.app.timers.is_registered(_idle_tick):
        bpy.app.timers.unregister(_idle_tick)
    _timer_on = False
    if _cursor is not None:
        try:
            bpy.context.window_manager.draw_cursor_remove(_cursor)
        except Exception:  # noqa: BLE001
            pass
        _cursor = None
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for lst in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.frame_change_post):
        if _on_change in lst:
            lst.remove(_on_change)
    _states.clear()
    _encode.clear()
