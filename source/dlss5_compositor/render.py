"""DLSS 5 on final renders.

F12 (5.0+): the DLSS 5 node's input is captured (scene-linear EXR) while the
            compositor runs, DLSS 5 runs on it after the render, and the result
            is fed back into the compositor through the node's internal group,
            so the Render Result, Viewer and saved image all contain DLSS 5.
F12 (4.x):  the Render Result is run through DLSS 5 and shown in the render
            window as "DLSS 5 Render".
Animation:  every written frame is run through DLSS 5 and written back in
            place, in the same format, frame by frame with temporal history.

Interactive renders call handlers from the render thread, so work is
deferred to the main thread with a timer. Background renders (blender -b)
run handlers on the main thread and are processed inline.
"""

import os
import shutil
import tempfile

import bpy
import numpy as np
from bpy.app.handlers import persistent

from . import colour, cpu_preview, engine, node as dnode, prefs

RESULT_IMAGE = "DLSS 5 Render"

_written = []
_rendering = False


# --------------------------------------------------------------------------
# Image helpers
# --------------------------------------------------------------------------

def load_rgba8(path):
    """Read an image file as display-referred RGBA8 (H, W, 4), rows bottom-up.
    Returns (pixels, was_float)."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        buf = np.empty(w * h * img.channels, dtype=np.float32)
        img.pixels.foreach_get(buf)
        buf = buf.reshape(h, w, img.channels)
        rgba = np.ones((h, w, 4), np.float32)
        rgba[..., :min(4, img.channels)] = buf[..., :4]
        if img.channels < 3:
            rgba[..., 1] = rgba[..., 2] = rgba[..., 0]
        was_float = bool(img.is_float)
        if was_float:  # linear EXR/HDR: encode to sRGB for DLSS
            rgba[..., :3] = cpu_preview.linear_to_srgb(rgba[..., :3])
        return np.clip(rgba * 255.0 + 0.5, 0, 255).astype(np.uint8), was_float
    finally:
        bpy.data.images.remove(img)


def save_rgba8(pixels, path, file_format, as_float=False, quality=90):
    h, w = pixels.shape[:2]
    f = pixels.astype(np.float32) / 255.0
    if as_float:
        f[..., :3] = cpu_preview.srgb_to_linear(f[..., :3])
    tmp = bpy.data.images.new("__dlss5_out", w, h, alpha=True, float_buffer=as_float)
    try:
        tmp.pixels.foreach_set(f.ravel())
        tmp.filepath_raw = path
        tmp.file_format = file_format
        try:
            tmp.save(quality=quality)
        except TypeError:
            tmp.save()
    finally:
        bpy.data.images.remove(tmp)


def _load_exr_float(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        try:
            img.colorspace_settings.is_data = True
        except Exception:
            pass
        w, h = img.size
        buf = np.empty(w * h * img.channels, dtype=np.float32)
        img.pixels.foreach_get(buf)
        return buf.reshape(h, w, img.channels)
    finally:
        bpy.data.images.remove(img)


def guides_for(scene, node, frame, width, height):
    """Depth (device depth 0..1) and motion (px to previous frame, D3D axes) from the capture."""
    p = prefs.get_prefs()
    if p is not None and not p.use_guides:
        return None, None
    files = dnode.guide_files(scene, node, frame)
    depth = motion = None
    cam = scene.camera
    if "Depth" in files and cam is not None and cam.type == "CAMERA":
        z = _load_exr_float(files["Depth"])[..., 0]
        if z.shape == (height, width):
            n, f = max(cam.data.clip_start, 1e-4), max(cam.data.clip_end, cam.data.clip_start + 1e-3)
            z = np.where(np.isfinite(z), z, f)
            depth = np.clip(f * (z - n) / (np.maximum(z, 1e-6) * (f - n)), 0.0, 1.0).astype(np.float32)
    if "Motion" in files:
        v = _load_exr_float(files["Motion"])
        if v.shape[:2] == (height, width):
            p = prefs.get_prefs()
            sign = -1.0 if (p and p.invert_motion) else 1.0
            # Blender's Vector pass: XY = movement towards the previous frame in pixels, +Y up.
            # DLSS: offset to the previous position, +Y down.
            motion = np.stack([v[..., 0] * sign, -v[..., 1] * sign], axis=-1).astype(np.float32)
    return depth, motion


def show_result(pixels):
    """Put the DLSS 5 image in the render window."""
    h, w = pixels.shape[:2]
    img = bpy.data.images.get(RESULT_IMAGE)
    if img is None or tuple(img.size) != (w, h) or img.is_float:
        if img is not None:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(RESULT_IMAGE, w, h, alpha=True, float_buffer=False)
    img.pixels.foreach_set((pixels.astype(np.float32) / 255.0).ravel())
    img.update()
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "IMAGE_EDITOR":
                sp = area.spaces.active
                if sp.image is not None and sp.image.type == "RENDER_RESULT":
                    sp.image = img
                area.tag_redraw()
    return img


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

_still = {"color": None, "depth": None}
_comp = {"lin": None, "disp": None, "inv": None, "depth": None, "frame": None, "scene": None}


def in_composite():
    return dnode.IN_COMPOSITE and colour.available()


def _back_to_render_result():
    """Image editors still showing the old separate DLSS image go back to the Render Result."""
    rr = bpy.data.images.get("Render Result")
    if rr is None:
        return
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "IMAGE_EDITOR":
                sp = area.spaces.active
                if sp.image is not None and sp.image.name == RESULT_IMAGE:
                    sp.image = rr
                area.tag_redraw()


def _process_still_comp(scene, reuse=False):
    node = dnode.find_node(scene)
    if node is None or not dnode.in_path(node):
        return None
    if not reuse or _comp["lin"] is None or _comp["scene"] != scene.name:
        files = dnode.guide_files(scene, node, scene.frame_current)
        path = files.get("Image")
        if path is None:
            raise RuntimeError("the DLSS 5 input wasn't captured during the render - render again")
        lin = _load_exr_float(path)
        h, w = lin.shape[:2]
        if lin.shape[2] < 4:
            full = np.ones((h, w, 4), np.float32)
            full[..., :lin.shape[2]] = lin
            lin = full
        disp = colour.to_display(scene, lin)
        depth, _motion = guides_for(scene, node, scene.frame_current, w, h)
        _comp.update(lin=lin, disp=disp, inv=colour.to_linear(scene, disp), depth=depth,
                     frame=scene.frame_current, scene=scene.name)
    lin, disp = _comp["lin"], _comp["disp"]
    h, w = lin.shape[:2]
    if not node.enabled or node.mute:
        dnode.set_result_state(False, _comp["frame"])
        node["dlss5_status"] = "DLSS 5 off - composite shows the original"
        return None
    settings = dnode.read_settings(node)
    out, backend, secs = engine.run(disp, settings, depth=_comp["depth"], reset=True, stream=0)
    res = colour.merge(lin, disp, out, scene, _comp["inv"])
    res[..., 3] = lin[..., 3]
    img = dnode.result_image((w, h))
    img.pixels.foreach_set(res.ravel())
    img.update()
    dnode.set_result_state(True, _comp["frame"])
    dnode.refresh_composite()
    _back_to_render_result()
    msg = f"DLSS 5 ({backend}) in the composite: {w}x{h}, {settings['passes']} pass(es), {secs:.2f}s"
    node["dlss5_status"] = msg
    print("[DLSS 5]", msg)
    return out


def process_still(scene, reuse=False):
    """Run DLSS 5 on the finished still render. reuse=True re-runs on the last
    captured render (used for live setting changes, no disk round-trip)."""
    if in_composite():
        return _process_still_comp(scene, reuse)
    node = dnode.active_node(scene)
    rr = bpy.data.images.get("Render Result")
    if node is None or (rr is None and _still["color"] is None):
        return None
    if reuse and _still["color"] is not None:
        color, depth = _still["color"], _still["depth"]
        h, w = color.shape[:2]
        return _run_still(node, color, depth)
    st = scene.render.image_settings
    saved = {k: getattr(st, k) for k in ("media_type", "file_format", "color_mode", "color_depth") if hasattr(st, k)}
    path = os.path.join(tempfile.gettempdir(), "blender_dlss5", "render_result.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if "media_type" in saved:
            st.media_type = "IMAGE"
        st.file_format = "PNG"
        st.color_mode = "RGBA"
        st.color_depth = "8"
        rr.save_render(path, scene=scene)
    finally:
        for k, v in saved.items():
            try:
                setattr(st, k, v)
            except Exception:
                pass
    color, _ = load_rgba8(path)
    h, w = color.shape[:2]
    depth, _motion = guides_for(scene, node, scene.frame_current, w, h)
    _still["color"], _still["depth"] = color, depth
    return _run_still(node, color, depth)


def _run_still(node, color, depth):
    h, w = color.shape[:2]
    settings = dnode.read_settings(node)
    out, backend, secs = engine.run(color, settings, depth=depth, reset=True, stream=0)
    show_result(out)
    msg = f"DLSS 5 ({backend}): {w}x{h}, {settings['passes']} pass(es), {secs:.2f}s"
    node["dlss5_status"] = msg
    print("[DLSS 5]", msg)
    return out


def process_frames(scene, frames):
    node = dnode.active_node(scene)
    if node is None or not frames:
        return
    if getattr(scene.render, "is_movie_format", False):
        print("[DLSS 5] video output can't be post-processed; render to an image sequence instead")
        node["dlss5_status"] = "Video output skipped: render an image sequence to get DLSS 5 frames"
        return
    p = prefs.get_prefs()
    settings = dnode.read_settings(node)
    fmt = scene.render.image_settings.file_format
    last_frame, out, done = None, None, 0
    for f in sorted(set(frames)):
        path = bpy.path.abspath(scene.render.frame_path(frame=f))
        if not os.path.isfile(path):
            continue
        try:
            color, was_float = load_rgba8(path)
            h, w = color.shape[:2]
            depth, motion = guides_for(scene, node, f, w, h)
            reset = last_frame is None or f != last_frame + 1
            out, backend, secs = engine.run(color, settings, depth=depth, motion=motion, reset=reset, stream=0)
            if p and p.keep_originals:
                keep = os.path.join(os.path.dirname(path), "no_dlss")
                os.makedirs(keep, exist_ok=True)
                shutil.copy2(path, os.path.join(keep, os.path.basename(path)))
            save_rgba8(out, path, fmt, as_float=was_float)
            last_frame = f
            done += 1
            print(f"[DLSS 5] frame {f}: {backend}, {secs:.2f}s -> {path}")
        except Exception as ex:  # noqa: BLE001
            print(f"[DLSS 5] frame {f} failed: {ex}")
            last_frame = None
    if out is not None and not bpy.app.background and not in_composite():
        show_result(out)
    # The per-frame input captures are only needed for stills; don't let them pile up.
    try:
        d = dnode.capture_dir(scene, node)
        for f in os.listdir(d):
            if f.startswith("Image") and f != "Image.exr" and f.endswith(".exr"):
                os.remove(os.path.join(d, f))
    except OSError:
        pass
    node["dlss5_status"] = f"DLSS 5 applied to {done} frame(s)"


def _process(scene_name, frames):
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        return
    try:
        if frames:
            process_frames(scene, frames)
        else:
            process_still(scene)
    except Exception as ex:  # noqa: BLE001
        print("[DLSS 5] failed:", ex)
        node = dnode.active_node(scene)
        if node is not None:
            node["dlss5_status"] = f"DLSS 5 failed: {ex}"
    engine.redraw_all()


# --------------------------------------------------------------------------
# Live updates of the render window
# --------------------------------------------------------------------------

_pending = {"scene": None}


def _render_views():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "IMAGE_EDITOR":
                sp = area.spaces.active
                if sp.image is not None and (sp.image.type == "RENDER_RESULT" or sp.image.name == RESULT_IMAGE):
                    yield area, sp


def _rerun():
    name = _pending["scene"]
    _pending["scene"] = None
    scene = bpy.data.scenes.get(name) if name else None
    if scene is None or _rendering:
        return None
    try:
        process_still(scene, reuse=True)
    except Exception as ex:  # noqa: BLE001
        print("[DLSS 5] live update failed:", ex)
    return None


def settings_changed(node):
    """A node setting moved: refresh the DLSS 5 render (debounced)."""
    if bpy.app.background:
        return
    if in_composite():
        if _comp["lin"] is None:
            return
    elif bpy.data.images.get(RESULT_IMAGE) is None or not any(True for _ in _render_views()):
        return
    scene = node.id_data and next((s for s in bpy.data.scenes if dnode.get_scene_tree(s) == node.id_data), None)
    if scene is None:
        return
    first = _pending["scene"] is None
    _pending["scene"] = scene.name
    if first:
        bpy.app.timers.register(_rerun, first_interval=0.15)


def enabled_changed(node):
    """A/B: show the DLSS 5 render or the original in every render window."""
    if in_composite():
        if _comp["lin"] is None:
            return
        if node.enabled:
            settings_changed(node)
        else:
            dnode.set_result_state(False, _comp["frame"])
            dnode.refresh_composite()
        return
    rr = bpy.data.images.get("Render Result")
    dl = bpy.data.images.get(RESULT_IMAGE)
    if node.enabled and dl is None:
        settings_changed(node)
        return
    for area, sp in list(_render_views()):
        target = dl if node.enabled else rr
        if target is not None and sp.image != target:
            sp.image = target
        area.tag_redraw()
    if node.enabled:
        settings_changed(node)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

@persistent
def on_render_init(scene, *_):
    global _rendering
    _rendering = True
    _written.clear()
    if in_composite():
        # A new render must composite the plain input (and capture it), never the last result.
        try:
            dnode.set_result_state(False, -1)
        except Exception as ex:  # noqa: BLE001
            print("[DLSS 5] couldn't reset the composite:", ex)
        _comp["lin"] = None
    if bpy.app.background:
        dnode.sync_all(scene)


@persistent
def on_render_write(scene, *_):
    _written.append(scene.frame_current)


@persistent
def on_render_complete(scene, *_):
    global _rendering
    _rendering = False
    frames = list(_written)
    _written.clear()
    if not frames and in_composite():
        n = dnode.find_node(scene)
        if n is None or not dnode.in_path(n):
            return
    elif dnode.active_node(scene) is None:
        return
    if bpy.app.background:
        _process(scene.name, frames)
        return
    name = scene.name
    bpy.app.timers.register(lambda: _process(name, frames), first_interval=0.05)


@persistent
def on_render_cancel(scene, *_):
    global _rendering
    _rendering = False
    _written.clear()


def _sync_timer():
    try:
        if _rendering or bpy.app.is_job_running("RENDER"):
            return 0.5
        if dnode.IN_COMPOSITE and any(dnode.is_dlss_node(n) for t in bpy.data.node_groups
                                      if t.bl_idname == "CompositorNodeTree" for n in t.nodes):
            dnode.shared_group()           # upgrades the internal group of older files
        for scene in bpy.data.scenes:
            tree = dnode.get_scene_tree(scene)
            if tree is not None and any(n.bl_idname in (dnode.IDNAME, "CompositorNodeOutputFile") for n in tree.nodes):
                p = prefs.get_prefs()
                for n in (dnode.iter_dlss_nodes(tree) if (p is None or p.use_guides) else ()):  # keep Depth & Motion plugged in
                    if not n.mute and dnode.in_path(n) and not dnode.guides_connected(n):
                        dnode.connect_guides(scene, n)
                dnode.sync_all(scene)
    except Exception as ex:  # noqa: BLE001
        print("[DLSS 5] sync failed:", ex)
    return 0.5


HANDLERS = (
    ("render_init", on_render_init),
    ("render_write", on_render_write),
    ("render_complete", on_render_complete),
    ("render_cancel", on_render_cancel),
)


@persistent
def on_load(*_args):
    _comp.update(lin=None, disp=None, inv=None, depth=None, frame=None, scene=None)
    try:
        dnode.set_result_state(False, -1)
    except Exception:  # noqa: BLE001
        pass


HANDLERS = HANDLERS + (("load_post", on_load),)


def register():
    for name, fn in HANDLERS:
        lst = getattr(bpy.app.handlers, name)
        if fn not in lst:
            lst.append(fn)
    if not bpy.app.background and not bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.register(_sync_timer, first_interval=1.0, persistent=True)


def unregister():
    for name, fn in HANDLERS:
        lst = getattr(bpy.app.handlers, name)
        if fn in lst:
            lst.remove(fn)
    if bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.unregister(_sync_timer)
