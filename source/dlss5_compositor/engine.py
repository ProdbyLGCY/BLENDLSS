"""Pixel back-ends.

NgxBackend drives bin/blendlss5_nvngx.dll, which loads nvngx_dlssnr.dll
(NVIDIA DLSS 5 Neural Rendering) and runs it on its own D3D12 device.
PreviewBackend is a numpy approximation for machines without an RTX GPU.

Both take and return RGBA8 display-referred images, rows bottom-up.
"""

import ctypes
import os
import sys
import time

import bpy
import numpy as np

from . import cpu_preview, prefs

ABI_VERSION = 3
MAX_PASSES = 16

_backend = None
_last_error = ""


class DLSS5B_Frame(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("color", ctypes.POINTER(ctypes.c_uint8)),
        ("depth", ctypes.POINTER(ctypes.c_float)),
        ("motion", ctypes.POINTER(ctypes.c_float)),
        ("reset", ctypes.c_int32),
        ("stream", ctypes.c_int32),
        ("passes", ctypes.c_int32),
        ("hide_indicator", ctypes.c_int32),
    ]


class DLSS5B_Settings(ctypes.Structure):
    _fields_ = [
        ("intensity", ctypes.c_float),
        ("local_structure", ctypes.c_float),
        ("local_tone", ctypes.c_float),
        ("skin_structure", ctypes.c_float),
        ("style", ctypes.c_int32),
        ("auto_mask", ctypes.c_int32),
        ("preset", ctypes.c_int32),
    ]


def _passes(settings):
    return max(1, min(MAX_PASSES, int(settings["passes"])))


def _ptr(arr, ctype):
    if arr is None:
        return ctypes.POINTER(ctype)()
    return arr.ctypes.data_as(ctypes.POINTER(ctype))


class NgxBackend:
    name = "NVIDIA DLSS 5"
    is_nvidia = True

    def __init__(self, dlssnr_path):
        if sys.platform != "win32":
            raise RuntimeError("DLSS 5 needs Windows and an NVIDIA RTX GPU")
        if not os.path.isfile(prefs.BRIDGE_DLL):
            raise RuntimeError("blendlss5_nvngx.dll is missing from the add-on's bin folder")
        if not os.path.isfile(dlssnr_path):
            raise RuntimeError(f"nvngx_dlssnr.dll not found: {dlssnr_path}")
        self.lib = lib = ctypes.CDLL(prefs.BRIDGE_DLL)
        lib.dlss5b_abi_version.restype = ctypes.c_int32
        lib.dlss5b_init.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        lib.dlss5b_init.restype = ctypes.c_int32
        lib.dlss5b_last_error.restype = ctypes.c_char_p
        lib.dlss5b_adapter_name.restype = ctypes.c_char_p
        lib.dlss5b_process.argtypes = [ctypes.POINTER(DLSS5B_Frame), ctypes.POINTER(DLSS5B_Settings),
                                       ctypes.POINTER(ctypes.c_uint8)]
        lib.dlss5b_process.restype = ctypes.c_int32
        lib.dlss5b_release_stream.argtypes = [ctypes.c_int32]
        lib.dlss5b_shutdown.restype = None
        u8p = ctypes.POINTER(ctypes.c_uint8)
        lib.dlss5b_prepare.argtypes = [u8p, ctypes.c_int32, ctypes.c_int32, u8p, u8p, ctypes.c_int32, ctypes.c_int32]
        lib.dlss5b_prepare.restype = None
        fp = ctypes.POINTER(ctypes.c_float)
        self.has_motion = hasattr(lib, "dlss5b_camera_motion")
        if self.has_motion:
            lib.dlss5b_camera_motion.argtypes = [fp, ctypes.c_int32, ctypes.c_int32, fp, fp, fp, fp,
                                                 ctypes.c_int32, ctypes.c_int32]
            lib.dlss5b_camera_motion.restype = None
        if lib.dlss5b_abi_version() != ABI_VERSION:
            raise RuntimeError("blendlss5_nvngx.dll does not match this add-on version")
        os.makedirs(prefs.data_dir(), exist_ok=True)
        if lib.dlss5b_init(dlssnr_path, prefs.data_dir()) != 0:
            raise RuntimeError(self._err() or "DLSS 5 failed to initialise")
        self.adapter = (lib.dlss5b_adapter_name() or b"").decode("utf-8", "replace")

    def _err(self):
        e = self.lib.dlss5b_last_error()
        return e.decode("utf-8", "replace") if e else ""

    def process(self, color, settings, depth=None, motion=None, reset=True, stream=0):
        h, w = color.shape[:2]
        color = np.ascontiguousarray(color, dtype=np.uint8)
        depth = None if depth is None else np.ascontiguousarray(depth, dtype=np.float32)
        motion = None if motion is None else np.ascontiguousarray(motion, dtype=np.float32)
        out = np.empty_like(color)
        fr = DLSS5B_Frame(w, h, _ptr(color, ctypes.c_uint8), _ptr(depth, ctypes.c_float),
                          _ptr(motion, ctypes.c_float), 1 if reset else 0, stream,
                          _passes(settings), 1)
        st = DLSS5B_Settings(settings["intensity"], settings["structure"], settings["tone"],
                             settings.get("skin", -1.0), settings["style"],
                             1 if settings.get("auto_mask", True) else 0, settings.get("preset", 1))
        if self.lib.dlss5b_process(ctypes.byref(fr), ctypes.byref(st), _ptr(out, ctypes.c_uint8)) != 0:
            raise RuntimeError(self._err() or "DLSS 5 evaluation failed")
        out[..., 3] = color[..., 3]
        return out

    def camera_motion(self, depth_src, inv_vp_cur, vp_prev, depth_out, motion_out):
        """Viewport depth (sh x sw float32) -> depth + camera motion at depth_out's size."""
        if not self.has_motion:
            raise RuntimeError("blendlss5_nvngx.dll is out of date (restart Blender)")
        sh, sw = depth_src.shape[:2]
        h, w = depth_out.shape[:2]
        a = None if inv_vp_cur is None else np.ascontiguousarray(inv_vp_cur, dtype=np.float32)
        b = None if vp_prev is None else np.ascontiguousarray(vp_prev, dtype=np.float32)
        self.lib.dlss5b_camera_motion(_ptr(depth_src, ctypes.c_float), sw, sh, _ptr(a, ctypes.c_float),
                                      _ptr(b, ctypes.c_float), _ptr(depth_out, ctypes.c_float),
                                      _ptr(motion_out, ctypes.c_float), w, h)

    def process_into(self, color, out, settings, reset=True, stream=0, depth=None, motion=None):
        """Like process(), writing straight into `out` (uint8 H x W x 4, contiguous)."""
        h, w = color.shape[:2]
        fr = DLSS5B_Frame(w, h, _ptr(color, ctypes.c_uint8), _ptr(depth, ctypes.c_float),
                          _ptr(motion, ctypes.c_float), 1 if reset else 0, stream,
                          _passes(settings), 1)
        st = DLSS5B_Settings(settings["intensity"], settings["structure"], settings["tone"],
                             settings.get("skin", -1.0), settings["style"],
                             1 if settings.get("auto_mask", True) else 0, settings.get("preset", 1))
        if self.lib.dlss5b_process(ctypes.byref(fr), ctypes.byref(st), _ptr(out, ctypes.c_uint8)) != 0:
            raise RuntimeError(self._err() or "DLSS 5 evaluation failed")
        return out

    def prepare(self, src, dst, overlay=None):
        """Remove the overlay layer from a captured viewport image (optional) and
        resample it to dst's size. All uint8 (h, w, 4), contiguous."""
        sh, sw = src.shape[:2]
        dh, dw = dst.shape[:2]
        self.lib.dlss5b_prepare(_ptr(src, ctypes.c_uint8), sw, sh, _ptr(overlay, ctypes.c_uint8),
                                _ptr(dst, ctypes.c_uint8), dw, dh)
        return dst

    def release_stream(self, stream):
        self.lib.dlss5b_release_stream(stream)

    def close(self):
        try:
            self.lib.dlss5b_shutdown()
        except Exception:
            pass


class PreviewBackend:
    name = "CPU preview (not DLSS)"
    is_nvidia = False
    adapter = "CPU"

    def process(self, color, settings, depth=None, motion=None, reset=True, stream=0):
        img = color.astype(np.float32) / 255.0
        lin = img.copy()
        lin[..., :3] = cpu_preview.srgb_to_linear(img[..., :3])
        for _ in range(_passes(settings)):
            lin = cpu_preview.enhance(lin, {}, settings)
        out = lin.copy()
        out[..., :3] = cpu_preview.linear_to_srgb(lin[..., :3])
        out[..., 3] = img[..., 3]
        return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)

    def release_stream(self, stream):
        pass

    def close(self):
        pass


def get_backend():
    global _backend, _last_error
    if _backend is not None:
        return _backend
    p = prefs.get_prefs()
    mode = p.backend if p else "AUTO"
    if mode != "PREVIEW":
        try:
            _backend = NgxBackend(prefs.dll_path())
            _last_error = ""
            print("[DLSS 5] ready on", _backend.adapter)
            return _backend
        except Exception as ex:  # noqa: BLE001
            _last_error = str(ex)
            print("[DLSS 5] runtime unavailable:", ex)
            if mode == "NVIDIA":
                raise
    _backend = PreviewBackend()
    return _backend


def shutdown():
    global _backend
    if _backend is not None:
        _backend.close()
    _backend = None


def status():
    b = _backend
    if b is None:
        try:
            b = get_backend()
        except Exception as ex:  # noqa: BLE001
            return {"nvidia": False, "message": f"DLSS 5 unavailable: {ex}"}
    if b.is_nvidia:
        return {"nvidia": True, "message": f"DLSS 5 ready on {b.adapter}"}
    msg = "CPU preview (not DLSS)"
    if _last_error:
        msg += f" - {_last_error}"
    return {"nvidia": False, "message": msg}


def run(color, settings, **kw):
    t0 = time.perf_counter()
    b = get_backend()
    out = b.process(color, settings, **kw)
    return out, b.name, time.perf_counter() - t0


def redraw_all():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "NODE_EDITOR", "IMAGE_EDITOR", "PREFERENCES"}:
                area.tag_redraw()
