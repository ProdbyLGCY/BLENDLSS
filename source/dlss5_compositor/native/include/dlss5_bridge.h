/*
 * BlenDLSS5 bridge - C ABI between the Blender add-on (Python/ctypes) and
 * NVIDIA DLSS 5 Neural Rendering (nvngx_dlssnr.dll, NGX feature 18).
 *
 * The DLL built from this is called "blendlss5_nvngx.dll" on purpose:
 * nvngx_dlssnr.dll only serves calls that return into a module whose path
 * contains "nvngx.dll".
 *
 * Images are RGBA8, display-referred (what you see on screen), rows
 * BOTTOM-UP (Blender order). Keep in sync with engine.py.
 */
#pragma once
#include <stdint.h>

#ifdef _WIN32
#define DLSS5B_API extern "C" __declspec(dllexport)
#else
#define DLSS5B_API extern "C" __attribute__((visibility("default")))
#endif

#define DLSS5B_ABI_VERSION 3
#define DLSS5B_MAX_PASSES 16

typedef struct DLSS5B_Frame {
    int32_t width;
    int32_t height;
    const uint8_t* color;   /* required, RGBA8 */
    const float* depth;     /* optional, 1 float per pixel, device depth 0..1 (0 = near) */
    const float* motion;    /* optional, 2 floats per pixel: pixel offset to the previous frame, D3D axes */
    int32_t reset;          /* 1 = drop temporal history (cut / first frame / camera jump) */
    int32_t stream;         /* 0 = final render, 1.. = viewports; each keeps its own history */
    int32_t passes;         /* 1..16, each pass has its own feature instance */
    int32_t hide_indicator; /* 1 = keep the NGX on-screen indicator out of the returned image */
} DLSS5B_Frame;

typedef struct DLSS5B_Settings {
    float intensity;        /* DLSSNR.Intensity (not clamped) */
    float local_structure;  /* DLSSNR.LocalStructureStrength 0..1 */
    float local_tone;       /* DLSSNR.LocalToneStrength     0..1 */
    float skin_structure;   /* DLSSNR.SkinStructureStrength -1 = model default */
    int32_t style;          /* DLSSNR.Style 0 Standard, 1 Natural, 2 Cinematic */
    int32_t auto_mask;      /* DLSSNR.UseAutoMask */
    int32_t preset;         /* DLSSNR.Hint.Render.Preset (1 = shipped model) */
} DLSS5B_Settings;

DLSS5B_API int32_t dlss5b_abi_version(void);
/* dlssnr_path: full path of nvngx_dlssnr.dll. data_dir: writable folder for NGX logs/cache. 0 = ok */
DLSS5B_API int32_t dlss5b_init(const wchar_t* dlssnr_path, const wchar_t* data_dir);
DLSS5B_API const char* dlss5b_adapter_name(void);
DLSS5B_API const char* dlss5b_last_error(void);
/* out: width*height*4 bytes, bottom-up. 0 = ok */
DLSS5B_API int32_t dlss5b_process(const DLSS5B_Frame* frame, const DLSS5B_Settings* settings, uint8_t* out);
DLSS5B_API void dlss5b_release_stream(int32_t stream);
DLSS5B_API void dlss5b_shutdown(void);
/* Viewport helper: optional overlay removal (overlay = straight-alpha RGBA8, same size as src)
   then bilinear resample src (sw x sh) into dst (dw x dh). All RGBA8. */
DLSS5B_API void dlss5b_prepare(const uint8_t* src, int32_t sw, int32_t sh, const uint8_t* overlay,
                               uint8_t* dst, int32_t dw, int32_t dh);
/* Viewport helper: resample the viewport depth buffer and derive camera motion vectors. */
DLSS5B_API void dlss5b_camera_motion(const float* depth_src, int32_t sw, int32_t sh,
                                     const float* inv_vp_cur, const float* vp_prev,
                                     float* depth_out, float* motion_out, int32_t w, int32_t h);
