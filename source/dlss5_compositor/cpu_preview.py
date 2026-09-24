"""CPU stand-in for DLSS 5 so the node works without the NVIDIA runtime.

This is a hand-written image filter, not a neural network. It maps the same
controls onto classic operations:

  Local Structure -> fine-scale local contrast plus depth-based contact
                     shadowing (when a Depth guide is connected)
  Local Tone      -> large-scale local tone mapping (lift shadows, tame
                     highlights) plus normal-based light shaping
  Style           -> a colour grade (Standard / Natural / Cinematic)
  Intensity       -> blend between the input and the enhanced result

Every array is float32 H x W x 4, scene-linear, rows bottom-up (Blender order).
"""

import numpy as np

EPS = 1e-6
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _box(a, r, axis):
    if r < 1:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(a, radius):
    """Approximate gaussian: three box blurs per axis."""
    r = max(1, int(round(radius / 1.7)))
    for _ in range(3):
        a = _box(a, r, 0)
        a = _box(a, r, 1)
    return a


def srgb_to_linear(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055).astype(np.float32)


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


STYLES = {
    # sat, contrast, split-tone shadows, split-tone highlights, warmth
    0: dict(sat=1.06, contrast=1.05, shadow=(1.0, 1.0, 1.0), high=(1.0, 1.0, 1.0), toe=0.0),
    1: dict(sat=0.94, contrast=0.96, shadow=(1.0, 1.0, 1.02), high=(1.03, 1.01, 0.97), toe=0.02),
    2: dict(sat=1.12, contrast=1.18, shadow=(0.93, 1.01, 1.09), high=(1.08, 1.0, 0.9), toe=0.0),
}


def enhance(rgba, guides, s):
    rgb = np.maximum(rgba[..., :3], 0.0)
    h, w = rgb.shape[:2]
    short = float(min(h, w))
    k = s["intensity"]
    if k <= 0.0:
        return rgba.copy()

    lum = rgb @ LUMA
    loglum = np.log2(lum + EPS)

    # --- Local tone: compress the large-scale base, keep detail ------------
    base = blur(loglum, short * 0.05)
    mid = np.median(base)
    tone = s["tone"]
    new_log = mid + (base - mid) * (1.0 - 0.45 * tone) + (loglum - base)
    shadows = 1.0 - _smoothstep((base - (mid - 3.0)) / 3.0)
    new_log += shadows * 0.6 * tone                 # lift deep shadows

    normal = guides.get("Normal")
    if normal is not None and tone > 0:
        n = normal[..., :3]
        ln = np.linalg.norm(n, axis=-1, keepdims=True) + EPS
        # Soft key light from upper-left, in whatever space the pass uses.
        key = np.clip((n / ln) @ np.array([-0.35, 0.55, 0.75], np.float32), -1, 1)
        new_log += 0.35 * tone * key

    # --- Local structure: fine detail + contact shadows -------------------
    st = s["structure"]
    fine = blur(new_log, max(1.0, short * 0.004))
    new_log = new_log + (new_log - fine) * (1.6 * st)
    medium = blur(new_log, short * 0.015)
    new_log = new_log + (new_log - medium) * (0.6 * st)

    depth = guides.get("Depth")
    if depth is not None and st > 0:
        z = depth[..., 0]
        finite = np.isfinite(z) & (z < 1e9)
        if finite.any():
            zc = np.where(finite, z, z[finite].max())
            zb = blur(zc, short * 0.01)
            occl = np.clip((zc - zb) / (zb + EPS) * 8.0, 0.0, 1.0)
            new_log -= occl * 1.2 * st

    # --- Back to RGB, preserving hue -------------------------------------
    ratio = np.exp2(new_log - loglum)[..., None]
    out = rgb * ratio

    # --- Style grade -----------------------------------------------------
    p = STYLES.get(s["style"], STYLES[0])
    ol = out @ LUMA
    grey = ol[..., None]
    out = grey + (out - grey) * p["sat"]
    lg = np.log2(np.maximum(ol, EPS))
    lg_c = mid + (lg - mid) * p["contrast"]
    out *= np.exp2(lg_c - lg)[..., None]
    hi_w = _smoothstep((lg_c - mid + 1.0) / 4.0)[..., None]
    out *= (1 - hi_w) * np.array(p["shadow"], np.float32) + hi_w * np.array(p["high"], np.float32)
    out += p["toe"]
    out = np.maximum(out, 0.0)

    res = rgba.copy()
    res[..., :3] = rgb + (out - rgb) * k
    return res.astype(np.float32)
