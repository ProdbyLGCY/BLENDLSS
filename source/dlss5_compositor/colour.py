"""Scene-linear <-> display conversion with Blender's own colour management.

DLSS 5 works on display-referred 8-bit images (what you see on screen), but
the compositor works in scene-linear light. To put DLSS 5 into the compositor
we take the node's scene-linear input, apply the scene's view transform
(exposure, look, view, display, gamma) exactly like Blender does, run DLSS 5,
and map the result back with the inverse transform.

Only the *change* DLSS 5 made is mapped back (linear = input + inv(dlss) -
inv(display)), so pixels DLSS 5 leaves alone come back bit-exact and HDR
highlights above the display range survive for glare, exposure nodes etc.
"""

import os

import bpy
import numpy as np

_cache = {"key": None, "fwd": None, "inv": None}


def available():
    try:
        import PyOpenColorIO  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _config():
    import PyOpenColorIO as OCIO
    path = os.environ.get("OCIO")
    if not path or not os.path.isfile(path):
        path = os.path.join(bpy.utils.resource_path("LOCAL"), "datafiles", "colormanagement", "config.ocio")
    return OCIO.Config.CreateFromFile(path)


def _processors(scene):
    import PyOpenColorIO as OCIO
    vs, ds = scene.view_settings, scene.display_settings
    key = (vs.view_transform, vs.look, round(vs.exposure, 6), round(vs.gamma, 6), ds.display_device)
    if _cache["key"] == key:
        return _cache["fwd"], _cache["inv"]
    cfg = _config()
    group = OCIO.GroupTransform()
    if abs(vs.exposure) > 1e-6:
        s = 2.0 ** vs.exposure
        group.appendTransform(OCIO.MatrixTransform(matrix=[s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1]))
    look = vs.look
    if look and look != "None":
        name = look if cfg.getLook(look) else f"{vs.view_transform} - {look}"
        if cfg.getLook(name):
            group.appendTransform(OCIO.LookTransform(src="scene_linear", dst="scene_linear", looks=name))
    group.appendTransform(OCIO.DisplayViewTransform(src="scene_linear", display=ds.display_device,
                                                    view=vs.view_transform))
    if abs(vs.gamma - 1.0) > 1e-6:
        g = 1.0 / vs.gamma
        group.appendTransform(OCIO.ExponentTransform(value=[g, g, g, 1.0]))
    fwd = cfg.getProcessor(group).getDefaultCPUProcessor()
    inv = cfg.getProcessor(group, OCIO.TRANSFORM_DIR_INVERSE).getDefaultCPUProcessor()
    _cache.update(key=key, fwd=fwd, inv=inv)
    return fwd, inv


def _apply(proc, rgba):
    buf = np.ascontiguousarray(rgba, dtype=np.float32).copy()
    proc.applyRGBA(buf.reshape(-1))
    return buf


def to_display(scene, linear):
    """Scene-linear float RGBA (H, W, 4) -> display RGBA8 as DLSS 5 wants it."""
    fwd, _inv = _processors(scene)
    rgb = np.nan_to_num(linear.astype(np.float32), nan=0.0, posinf=1e4, neginf=0.0)
    disp = _apply(fwd, rgb)
    out = np.clip(disp * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    return out


def to_linear(scene, display_u8):
    """Display RGBA8 -> scene-linear float RGBA through the inverse view transform."""
    _fwd, inv = _processors(scene)
    f = display_u8.astype(np.float32) / 255.0
    f[..., 3] = 1.0
    return _apply(inv, f)


def merge(linear, display_in, display_out, scene, inv_in=None):
    """Scene-linear result: the input plus the change DLSS 5 made, mapped back to linear."""
    if inv_in is None:
        inv_in = to_linear(scene, display_in)
    out = linear.astype(np.float32).copy()
    delta = to_linear(scene, display_out)[..., :3] - inv_in[..., :3]
    out[..., :3] = np.maximum(out[..., :3] + delta, 0.0)
    return out
