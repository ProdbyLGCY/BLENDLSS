/*
 * blendlss5_nvngx.dll - runs NVIDIA DLSS 5 Neural Rendering for Blender.
 *
 * It talks to nvngx_dlssnr.dll directly (its NVSDK_NGX_D3D12_* exports), the
 * same "pure" path NeuralScreen and the Full-Screen DLSS 5 Wrapper use:
 *
 *   Init_Ext -> CreateFeature(18) per (stream, pass) -> EvaluateFeature
 *
 * No NGX SDK, no Streamline and no NVIDIA headers are needed. The only
 * NVIDIA-shaped thing here is NVSDK_NGX_Parameter, the name/value block the
 * runtime reads its settings from. It is a C++ interface compiled by MSVC, so
 * we build its vtable by hand in MSVC's slot order (overloads grouped and
 * reversed; verified against nvsdk_ngx_parameters_lib.obj from the SDK):
 *
 *   0x00 Set(void*)   0x08 Set(ID3D12Resource*) 0x10 Set(ID3D11Resource*)
 *   0x18 Set(int)     0x20 Set(unsigned)        0x28 Set(double)
 *   0x30 Set(float)   0x38 Set(unsigned long long)
 *   0x40 Get(void**)  0x48 Get(ID3D12**)        0x50 Get(ID3D11**)
 *   0x58 Get(int*)    0x60 Get(unsigned*)       0x68 Get(double*)
 *   0x70 Get(float*)  0x78 Get(ull*)            0x80 Reset()
 *
 * The runtime checks that each call comes back into a module whose path
 * contains "nvngx.dll", which is why this file is built as
 * blendlss5_nvngx.dll and why the calls below are never tail calls.
 */
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include "../include/dlss5_bridge.h"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <thread>
#include <vector>

template <class T> static void safe_release(T*& p) { if (p) { p->Release(); p = nullptr; } }

namespace {

// ---------------------------------------------------------------------------
// NGX basics (values from nvsdk_ngx_defs.h)
// ---------------------------------------------------------------------------
using NgxResult = uint32_t;
constexpr NgxResult kNgxSuccess = 0x1;
constexpr NgxResult kNgxFail = 0xBAD00000;
constexpr uint32_t kNgxVersionApi = 0x15;
constexpr uint32_t kFeatureNeuralRendering = 18;
constexpr unsigned long long kAppId = 0x1000000ULL;
inline bool ngx_failed(NgxResult r) { return (r & 0xFFF00000u) == kNgxFail; }

const char* ngx_name(NgxResult r)
{
    switch (r) {
    case 0x1: return "Success";
    case 0xBAD00000: return "Fail";
    case 0xBAD00001: return "FeatureNotSupported";
    case 0xBAD00002: return "PlatformError (caller module check)";
    case 0xBAD00003: return "FeatureAlreadyExists";
    case 0xBAD00004: return "FeatureNotFound";
    case 0xBAD00005: return "InvalidParameter";
    case 0xBAD00006: return "ScratchBufferTooSmall";
    case 0xBAD00007: return "NotInitialized";
    case 0xBAD00008: return "UnsupportedInputFormat";
    case 0xBAD00009: return "RWFlagMissing";
    case 0xBAD0000A: return "MissingInput";
    case 0xBAD0000B: return "UnableToInitializeFeature";
    case 0xBAD0000C: return "OutOfDate (driver too old)";
    case 0xBAD0000D: return "OutOfGPUMemory";
    case 0xBAD0000E: return "UnsupportedFormat";
    case 0xBAD0000F: return "UnableToWriteToAppDataPath";
    case 0xBAD00010: return "UnsupportedParameter";
    case 0xBAD00011: return "Denied";
    case 0xBAD00012: return "NotImplemented";
    }
    return "unknown";
}

struct NgxHandle;
struct Params;
using PFN_InitExt = NgxResult (*)(unsigned long long, const wchar_t*, ID3D12Device*, uint32_t, const Params*);
using PFN_Create = NgxResult (*)(ID3D12GraphicsCommandList*, uint32_t, Params*, NgxHandle**);
using PFN_Evaluate = NgxResult (*)(ID3D12GraphicsCommandList*, const NgxHandle*, Params*, void*);
using PFN_Release = NgxResult (*)(NgxHandle*);
using PFN_Shutdown1 = NgxResult (*)(ID3D12Device*);

// ---------------------------------------------------------------------------
// NVSDK_NGX_Parameter, hand-built
// ---------------------------------------------------------------------------
enum class VT : uint8_t { U64, F32, F64, U32, I32, Ptr };
struct Value {
    VT type;
    union { unsigned long long u64; float f32; double f64; unsigned u32; int i32; void* ptr; };
};

struct Params {
    void* const* vtbl;
    std::map<std::string, Value> values;
};

double as_double(const Value& v)
{
    switch (v.type) {
    case VT::U64: return (double)v.u64;
    case VT::F32: return v.f32;
    case VT::F64: return v.f64;
    case VT::U32: return v.u32;
    case VT::I32: return v.i32;
    case VT::Ptr: return (double)(uintptr_t)v.ptr;
    }
    return 0;
}
unsigned long long as_u64(const Value& v)
{
    switch (v.type) {
    case VT::U64: return v.u64;
    case VT::U32: return v.u32;
    case VT::I32: return (unsigned long long)(long long)v.i32;
    case VT::Ptr: return (unsigned long long)(uintptr_t)v.ptr;
    default: return (unsigned long long)as_double(v);
    }
}

void put(Params* p, const char* n, Value v) { if (n) p->values[n] = v; }
const Value* find(const Params* p, const char* n)
{
    if (!n) return nullptr;
    auto it = p->values.find(n);
    return it == p->values.end() ? nullptr : &it->second;
}

void P_SetPtr(Params* p, const char* n, void* v) { Value x{}; x.type = VT::Ptr; x.ptr = v; put(p, n, x); }
void P_SetI(Params* p, const char* n, int v) { Value x{}; x.type = VT::I32; x.i32 = v; put(p, n, x); }
void P_SetUI(Params* p, const char* n, unsigned v) { Value x{}; x.type = VT::U32; x.u32 = v; put(p, n, x); }
void P_SetD(Params* p, const char* n, double v) { Value x{}; x.type = VT::F64; x.f64 = v; put(p, n, x); }
void P_SetF(Params* p, const char* n, float v) { Value x{}; x.type = VT::F32; x.f32 = v; put(p, n, x); }
void P_SetULL(Params* p, const char* n, unsigned long long v) { Value x{}; x.type = VT::U64; x.u64 = v; put(p, n, x); }

NgxResult P_GetPtr(const Params* p, const char* n, void** o)
{
    const Value* v = find(p, n);
    if (!v || !o) return kNgxFail;
    *o = v->type == VT::Ptr ? v->ptr : (void*)(uintptr_t)as_u64(*v);
    return kNgxSuccess;
}
NgxResult P_GetI(const Params* p, const char* n, int* o)
{ const Value* v = find(p, n); if (!v || !o) return kNgxFail; *o = v->type == VT::I32 ? v->i32 : (int)as_u64(*v); return kNgxSuccess; }
NgxResult P_GetUI(const Params* p, const char* n, unsigned* o)
{ const Value* v = find(p, n); if (!v || !o) return kNgxFail; *o = (v->type == VT::F32 || v->type == VT::F64) ? (unsigned)as_double(*v) : (unsigned)as_u64(*v); return kNgxSuccess; }
NgxResult P_GetD(const Params* p, const char* n, double* o)
{ const Value* v = find(p, n); if (!v || !o) return kNgxFail; *o = as_double(*v); return kNgxSuccess; }
NgxResult P_GetF(const Params* p, const char* n, float* o)
{ const Value* v = find(p, n); if (!v || !o) return kNgxFail; *o = (float)as_double(*v); return kNgxSuccess; }
NgxResult P_GetULL(const Params* p, const char* n, unsigned long long* o)
{ const Value* v = find(p, n); if (!v || !o) return kNgxFail; *o = (v->type == VT::F32 || v->type == VT::F64) ? (unsigned long long)as_double(*v) : as_u64(*v); return kNgxSuccess; }
void P_Reset(Params* p) { p->values.clear(); }

void* const kParamsVtbl[17] = {
    (void*)&P_SetPtr,  (void*)&P_SetPtr,  (void*)&P_SetPtr,  (void*)&P_SetI,
    (void*)&P_SetUI,   (void*)&P_SetD,    (void*)&P_SetF,    (void*)&P_SetULL,
    (void*)&P_GetPtr,  (void*)&P_GetPtr,  (void*)&P_GetPtr,  (void*)&P_GetI,
    (void*)&P_GetUI,   (void*)&P_GetD,    (void*)&P_GetF,    (void*)&P_GetULL,
    (void*)&P_Reset,
};

Params g_params{kParamsVtbl, {}};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
std::string g_error, g_adapter;
std::wstring g_logPath;

HMODULE g_nr = nullptr;
PFN_InitExt g_initExt = nullptr;
PFN_Create g_create = nullptr;
PFN_Evaluate g_evaluate = nullptr;
PFN_Release g_release = nullptr;
PFN_Shutdown1 g_shutdown1 = nullptr;
bool g_ngxInit = false;
volatile NgxResult g_lastNgx = 0;   // written after every NGX call so none is a tail call

ID3D12Device* g_dev = nullptr;
ID3D12CommandQueue* g_queue = nullptr;
ID3D12CommandAllocator* g_alloc = nullptr;
ID3D12GraphicsCommandList* g_cmd = nullptr;
ID3D12Fence* g_fence = nullptr;
HANDLE g_fenceEvent = nullptr;
UINT64 g_fenceValue = 0;

struct Tex {
    ID3D12Resource* res = nullptr;
    DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN;
    UINT bpp = 0;
    D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_COMMON;
};
struct Targets {
    int w = 0, h = 0;
    Tex color, depth, mvec, out[2];
    ID3D12Resource* upload = nullptr;
    ID3D12Resource* readback = nullptr;
    UINT64 uploadSize = 0, readbackSize = 0;
    bool depthConst = false, mvecConst = false;   // constant guides already on the GPU
};
Targets g_t;

struct Feature { NgxHandle* handle = nullptr; int w = 0, h = 0; };
std::map<int, Feature> g_features;   // key = stream * 16 + pass

void log(const char* fmt, ...)
{
    if (g_logPath.empty()) return;
    FILE* f = _wfopen(g_logPath.c_str(), L"a");
    if (!f) return;
    SYSTEMTIME st; GetLocalTime(&st);
    std::fprintf(f, "%02d:%02d:%02d.%03d ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_list ap; va_start(ap, fmt); std::vfprintf(f, fmt, ap); va_end(ap);
    std::fputc('\n', f);
    std::fclose(f);
}

bool fail(const std::string& m) { g_error = m; log("ERROR %s", m.c_str()); return false; }
std::string fmt_hr(const char* what, HRESULT hr)
{ char b[160]; std::snprintf(b, sizeof b, "%s failed (HRESULT 0x%08lX)", what, (unsigned long)hr); return b; }
std::string fmt_ngx(const char* what, NgxResult r)
{ char b[200]; std::snprintf(b, sizeof b, "%s failed: 0x%08X %s", what, r, ngx_name(r)); return b; }

// ---------------------------------------------------------------------------
// D3D12 helpers
// ---------------------------------------------------------------------------
bool wait_gpu()
{
    const UINT64 v = ++g_fenceValue;
    HRESULT hr = g_queue->Signal(g_fence, v);
    if (FAILED(hr)) return fail(fmt_hr("Signal", hr));
    if (g_fence->GetCompletedValue() < v) {
        g_fence->SetEventOnCompletion(v, g_fenceEvent);
        if (WaitForSingleObject(g_fenceEvent, 30000) != WAIT_OBJECT_0) return fail("GPU timed out (30 s)");
    }
    return true;
}

bool begin()
{
    HRESULT hr = g_alloc->Reset();
    if (FAILED(hr)) return fail(fmt_hr("CommandAllocator::Reset", hr));
    hr = g_cmd->Reset(g_alloc, nullptr);
    if (FAILED(hr)) return fail(fmt_hr("CommandList::Reset", hr));
    return true;
}

bool submit()
{
    HRESULT hr = g_cmd->Close();
    if (FAILED(hr)) return fail(fmt_hr("CommandList::Close", hr));
    ID3D12CommandList* lists[] = {g_cmd};
    g_queue->ExecuteCommandLists(1, lists);
    if (!wait_gpu()) return false;
    hr = g_dev->GetDeviceRemovedReason();
    if (FAILED(hr)) return fail(fmt_hr("GPU device removed", hr));
    return true;
}

ID3D12Resource* make_buffer(UINT64 size, D3D12_HEAP_TYPE heap, D3D12_RESOURCE_STATES state)
{
    D3D12_HEAP_PROPERTIES hp{}; hp.Type = heap;
    D3D12_RESOURCE_DESC d{};
    d.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    d.Width = size; d.Height = 1; d.DepthOrArraySize = 1; d.MipLevels = 1;
    d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource* r = nullptr;
    HRESULT hr = g_dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, state, nullptr, IID_PPV_ARGS(&r));
    if (FAILED(hr)) { fail(fmt_hr("CreateCommittedResource(buffer)", hr)); return nullptr; }
    return r;
}

bool make_tex(Tex& t, int w, int h, DXGI_FORMAT fmt, UINT bpp, bool uav)
{
    D3D12_HEAP_PROPERTIES hp{}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC d{};
    d.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    d.Width = (UINT64)w; d.Height = (UINT)h; d.DepthOrArraySize = 1; d.MipLevels = 1;
    d.Format = fmt; d.SampleDesc.Count = 1; d.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    d.Flags = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    t.fmt = fmt; t.bpp = bpp; t.state = D3D12_RESOURCE_STATE_COMMON;
    HRESULT hr = g_dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &d, t.state, nullptr, IID_PPV_ARGS(&t.res));
    if (FAILED(hr)) return fail(fmt_hr("CreateCommittedResource(texture)", hr));
    return true;
}

inline UINT pitch_of(int w, UINT bpp)
{
    const UINT a = D3D12_TEXTURE_DATA_PITCH_ALIGNMENT;
    return ((UINT)w * bpp + a - 1) / a * a;
}
inline UINT64 align_up(UINT64 v, UINT64 a) { return (v + a - 1) / a * a; }

void release_targets()
{
    for (Tex* t : {&g_t.color, &g_t.depth, &g_t.mvec, &g_t.out[0], &g_t.out[1]}) safe_release(t->res);
    safe_release(g_t.upload);
    safe_release(g_t.readback);
    g_t = Targets{};
}

bool ensure_targets(int w, int h)
{
    if (g_t.w == w && g_t.h == h && g_t.color.res) return true;
    release_targets();
    g_t.w = w; g_t.h = h;
    if (!make_tex(g_t.color, w, h, DXGI_FORMAT_R8G8B8A8_UNORM, 4, false)) return false;
    if (!make_tex(g_t.depth, w, h, DXGI_FORMAT_R32_FLOAT, 4, false)) return false;
    if (!make_tex(g_t.mvec, w, h, DXGI_FORMAT_R16G16_FLOAT, 4, false)) return false;
    if (!make_tex(g_t.out[0], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, 4, true)) return false;
    if (!make_tex(g_t.out[1], w, h, DXGI_FORMAT_R8G8B8A8_UNORM, 4, true)) return false;
    const UINT64 A = D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT;
    g_t.uploadSize = 3 * (align_up((UINT64)pitch_of(w, 4) * h, A) + A);
    g_t.readbackSize = (UINT64)pitch_of(w, 4) * h;
    g_t.upload = make_buffer(g_t.uploadSize, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    g_t.readback = make_buffer(g_t.readbackSize, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    log("targets %dx%d", w, h);
    return g_t.upload && g_t.readback;
}

void barrier(Tex& t, D3D12_RESOURCE_STATES to)
{
    if (t.state == to) return;
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = t.res;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = t.state;
    b.Transition.StateAfter = to;
    g_cmd->ResourceBarrier(1, &b);
    t.state = to;
}

void uav_barrier(Tex& t)
{
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    b.UAV.pResource = t.res;
    g_cmd->ResourceBarrier(1, &b);
}

template <class Fill>
UINT64 stage(Tex& t, uint8_t* mapped, UINT64 offset, Fill fill)
{
    const UINT pitch = pitch_of(g_t.w, t.bpp);
    offset = align_up(offset, D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT);
    {   // rows are independent: fill them on several threads
        const int H = g_t.h;
        const int nt = H >= 256 ? (int)std::clamp(std::thread::hardware_concurrency(), 1u, 8u) : 1;
        if (nt == 1) {
            for (int y = 0; y < H; ++y) fill(mapped + offset + (UINT64)y * pitch, y);
        } else {
            std::vector<std::thread> ts;
            const int step = (H + nt - 1) / nt;
            for (int t = 0; t < nt; ++t) {
                const int y0 = t * step, y1 = std::min(H, y0 + step);
                if (y0 < y1)
                    ts.emplace_back([&, y0, y1] { for (int y = y0; y < y1; ++y) fill(mapped + offset + (UINT64)y * pitch, y); });
            }
            for (auto& th : ts) th.join();
        }
    }
    barrier(t, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = t.res;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = g_t.upload;
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    src.PlacedFootprint.Offset = offset;
    src.PlacedFootprint.Footprint.Format = t.fmt;
    src.PlacedFootprint.Footprint.Width = (UINT)g_t.w;
    src.PlacedFootprint.Footprint.Height = (UINT)g_t.h;
    src.PlacedFootprint.Footprint.Depth = 1;
    src.PlacedFootprint.Footprint.RowPitch = pitch;
    g_cmd->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    return offset + (UINT64)pitch * g_t.h;
}

uint16_t to_half(float f)
{
    uint32_t x; std::memcpy(&x, &f, 4);
    const uint32_t sign = (x >> 16) & 0x8000u;
    int32_t e = (int32_t)((x >> 23) & 0xFF) - 127 + 15;
    uint32_t m = x & 0x7FFFFFu;
    if (((x >> 23) & 0xFF) == 0xFF) return (uint16_t)(sign | 0x7C00u | (m ? 0x200u : 0));
    if (e >= 31) return (uint16_t)(sign | 0x7C00u);
    if (e <= 0) {
        if (e < -10) return (uint16_t)sign;
        m |= 0x800000u;
        const uint32_t shift = (uint32_t)(14 - e);
        uint32_t hm = m >> shift;
        if ((m >> (shift - 1)) & 1u) ++hm;
        return (uint16_t)(sign | hm);
    }
    uint32_t h = sign | ((uint32_t)e << 10) | (m >> 13);
    if (m & 0x1000u) ++h;
    return (uint16_t)h;
}

// ---------------------------------------------------------------------------
// NGX calls (kept out of line; each stores its result so it is never a tail call)
// ---------------------------------------------------------------------------
__attribute__((noinline)) NgxResult call_init(const wchar_t* dataDir)
{
    NgxResult r = g_initExt(kAppId, dataDir, g_dev, kNgxVersionApi, &g_params);
    g_lastNgx = r;
    return r;
}
__attribute__((noinline)) NgxResult call_create(NgxHandle** out)
{
    NgxResult r = g_create(g_cmd, kFeatureNeuralRendering, &g_params, out);
    g_lastNgx = r;
    return r;
}
__attribute__((noinline)) NgxResult call_evaluate(NgxHandle* h)
{
    NgxResult r = g_evaluate(g_cmd, h, &g_params, nullptr);
    g_lastNgx = r;
    return r;
}
__attribute__((noinline)) NgxResult call_release(NgxHandle* h)
{
    NgxResult r = g_release(h);
    g_lastNgx = r;
    return r;
}
__attribute__((noinline)) NgxResult call_shutdown()
{
    NgxResult r = g_shutdown1 ? g_shutdown1(g_dev) : kNgxSuccess;
    g_lastNgx = r;
    return r;
}

void set_tuning(const DLSS5B_Settings& s)
{
    P_SetUI(&g_params, "DLSSNR.Hint.Render.Preset", (unsigned)std::max(0, s.preset));
    // No clamping: this is a testing tool, values outside 0..1 go straight to the model.
    auto fin = [](float v, float d) { return std::isfinite(v) ? v : d; };
    P_SetF(&g_params, "DLSSNR.Intensity", fin(s.intensity, 1.0f));
    P_SetUI(&g_params, "DLSSNR.Style", (unsigned)std::clamp(s.style, 0, 2));
    P_SetF(&g_params, "DLSSNR.LocalStructureStrength", fin(s.local_structure, 1.0f));
    P_SetF(&g_params, "DLSSNR.LocalToneStrength", fin(s.local_tone, 0.5f));
    P_SetF(&g_params, "DLSSNR.SkinStructureStrength", s.skin_structure);
    P_SetUI(&g_params, "DLSSNR.UseAutoMask", s.auto_mask ? 1u : 0u);
    P_SetUI(&g_params, "DLSSNR.UICorrection", 0u);
}

void release_feature(Feature& f)
{
    if (f.handle) {
        NgxResult r = call_release(f.handle);
        log("release feature -> 0x%08X", r);
        f.handle = nullptr;
    }
}

bool ensure_feature(int stream, int pass, int w, int h, const DLSS5B_Settings& s)
{
    Feature& f = g_features[stream * 64 + pass];
    if (f.handle && f.w == w && f.h == h) return true;
    if (f.handle) { wait_gpu(); release_feature(f); }

    if (!begin()) return false;
    P_Reset(&g_params);
    P_SetUI(&g_params, "CreationNodeMask", 1u);
    P_SetUI(&g_params, "VisibilityNodeMask", 1u);
    P_SetUI(&g_params, "DLSSNR.Enabled", 1u);
    P_SetUI(&g_params, "DLSSNR.Width", (unsigned)w);
    P_SetUI(&g_params, "DLSSNR.Height", (unsigned)h);
    P_SetF(&g_params, "DLSSNR.ScalingRatio", 1.0f);
    P_SetUI(&g_params, "DLSS.Feature.Create.Flags", 0u);
    set_tuning(s);
    NgxHandle* handle = nullptr;
    NgxResult r = call_create(&handle);
    if (!submit()) return false;
    log("CreateFeature(18) stream=%d pass=%d %dx%d -> 0x%08X %s", stream, pass, w, h, r, ngx_name(r));
    if (ngx_failed(r) || !handle) return fail(fmt_ngx("CreateFeature(DLSS 5)", r));
    f.handle = handle; f.w = w; f.h = h;
    return true;
}

} // namespace

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------
DLSS5B_API int32_t dlss5b_abi_version(void) { return DLSS5B_ABI_VERSION; }
DLSS5B_API const char* dlss5b_last_error(void) { return g_error.c_str(); }
DLSS5B_API const char* dlss5b_adapter_name(void) { return g_adapter.c_str(); }

DLSS5B_API int32_t dlss5b_init(const wchar_t* dlssnr_path, const wchar_t* data_dir)
{
    if (g_ngxInit) return 0;
    g_error.clear();
    std::wstring dataDir = data_dir ? data_dir : L"";
    if (!dataDir.empty()) {
        CreateDirectoryW(dataDir.c_str(), nullptr);
        g_logPath = dataDir + L"\\blendlss5.log";
    }
    log("---- init (abi %d)", DLSS5B_ABI_VERSION);

    // 1. The DLSS 5 runtime.
    if (!g_nr) {
        g_nr = LoadLibraryExW(dlssnr_path, nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!g_nr) {
            char b[64]; std::snprintf(b, sizeof b, " (Win32 error %lu)", GetLastError());
            fail(std::string("Could not load nvngx_dlssnr.dll") + b);
            return 1;
        }
        g_initExt = reinterpret_cast<PFN_InitExt>(reinterpret_cast<void*>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_Init_Ext")));
        g_create = reinterpret_cast<PFN_Create>(reinterpret_cast<void*>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_CreateFeature")));
        g_evaluate = reinterpret_cast<PFN_Evaluate>(reinterpret_cast<void*>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_EvaluateFeature")));
        g_release = reinterpret_cast<PFN_Release>(reinterpret_cast<void*>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_ReleaseFeature")));
        g_shutdown1 = reinterpret_cast<PFN_Shutdown1>(reinterpret_cast<void*>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_Shutdown1")));
        if (!g_initExt || !g_create || !g_evaluate || !g_release) {
            fail("This DLL is not an NVIDIA DLSS 5 runtime (missing NVSDK_NGX_D3D12_* exports)");
            return 2;
        }
    }

    // 2. A D3D12 device on the NVIDIA GPU.
    if (!g_dev) {
        IDXGIFactory6* factory = nullptr;
        HRESULT hr = CreateDXGIFactory2(0, IID_PPV_ARGS(&factory));
        if (FAILED(hr)) { fail(fmt_hr("CreateDXGIFactory2", hr)); return 3; }
        IDXGIAdapter1* chosen = nullptr;
        for (UINT i = 0;; ++i) {
            IDXGIAdapter1* a = nullptr;
            if (factory->EnumAdapterByGpuPreference(i, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&a)) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_ADAPTER_DESC1 d{}; a->GetDesc1(&d);
            if (d.VendorId == 0x10DE && !(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
                char name[128];
                WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, name, sizeof name, nullptr, nullptr);
                g_adapter = name; chosen = a; break;
            }
            safe_release(a);
        }
        safe_release(factory);
        if (!chosen) { fail("No NVIDIA GPU found (DLSS 5 needs a GeForce RTX card)"); return 4; }
        hr = D3D12CreateDevice(chosen, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&g_dev));
        safe_release(chosen);
        if (FAILED(hr)) { fail(fmt_hr("D3D12CreateDevice", hr)); return 5; }
        log("device: %s", g_adapter.c_str());

        D3D12_COMMAND_QUEUE_DESC qd{}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        if (FAILED(hr = g_dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&g_queue)))) { fail(fmt_hr("CreateCommandQueue", hr)); return 6; }
        if (FAILED(hr = g_dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&g_alloc)))) { fail(fmt_hr("CreateCommandAllocator", hr)); return 6; }
        if (FAILED(hr = g_dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, g_alloc, nullptr, IID_PPV_ARGS(&g_cmd)))) { fail(fmt_hr("CreateCommandList", hr)); return 6; }
        g_cmd->Close();
        if (FAILED(hr = g_dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&g_fence)))) { fail(fmt_hr("CreateFence", hr)); return 6; }
        g_fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }

    // 3. NGX init straight on the DLSS 5 runtime.
    // The model draws a debug overlay (version / weights) into the image when
    // the NGX indicator is on; renders must stay clean, so force it off.
    SetEnvironmentVariableW(L"__NGX_SHOW_INDICATOR", L"0");
    P_Reset(&g_params);
    P_SetUI(&g_params, "ShowDlssIndicator", 0u);
    P_SetUI(&g_params, "NGX.ShowIndicator", 0u);
    NgxResult r = call_init(dataDir.c_str());
    log("Init_Ext -> 0x%08X %s", r, ngx_name(r));
    if (ngx_failed(r)) { fail(fmt_ngx("DLSS 5 Init", r)); return 7; }
    g_ngxInit = true;
    return 0;
}

DLSS5B_API int32_t dlss5b_process(const DLSS5B_Frame* f, const DLSS5B_Settings* s, uint8_t* out)
{
    g_error.clear();
    if (!g_ngxInit) { fail("DLSS 5 is not initialised"); return 1; }
    if (!f || !s || !out || !f->color || f->width < 16 || f->height < 16) { fail("bad arguments"); return 2; }
    const int w = f->width, h = f->height;
    // The runtime stamps a version/weights label into the bottom-left of its
    // output when the NGX indicator is switched on system-wide. We run DLSS 5
    // on a slightly taller image (mirrored strip underneath) and only return
    // the top part, so the label lands in the strip we throw away.
    const int pad = f->hide_indicator ? std::max(96, h / 8) : 0;
    const int H = h + pad;
    auto srow = [&](int y) { return y < h ? h - 1 - y : std::min(y - h, h - 1); };  // D3D row -> source row
    const int passes = std::clamp(f->passes, 1, DLSS5B_MAX_PASSES);
    if (!ensure_targets(w, H)) return 3;
    for (int p = 0; p < passes; ++p)
        if (!ensure_feature(f->stream, p, w, H, *s)) return 4;

    if (!begin()) return 5;

    // ---- upload (flip bottom-up -> top-down) ----------------------------
    uint8_t* mapped = nullptr;
    D3D12_RANGE none{0, 0};
    if (FAILED(g_t.upload->Map(0, &none, reinterpret_cast<void**>(&mapped)))) { g_cmd->Close(); fail("Map(upload) failed"); return 6; }
    UINT64 off = 0;
    off = stage(g_t.color, mapped, off, [&](uint8_t* dst, int y) {
        std::memcpy(dst, f->color + (size_t)srow(y) * w * 4, (size_t)w * 4);
    });
    // Guides: when absent they're a constant plane / zero vectors, uploaded once per size.
    if (f->depth || !g_t.depthConst) {
        off = stage(g_t.depth, mapped, off, [&](uint8_t* dst, int y) {
            float* d = reinterpret_cast<float*>(dst);
            if (!f->depth) { std::fill(d, d + w, 0.5f); return; }
            const float* sp = f->depth + (size_t)srow(y) * w;
            for (int x = 0; x < w; ++x) d[x] = std::isfinite(sp[x]) ? std::clamp(sp[x], 0.0f, 1.0f) : 1.0f;
        });
        g_t.depthConst = !f->depth;
    }
    if (f->motion || !g_t.mvecConst) {
        off = stage(g_t.mvec, mapped, off, [&](uint8_t* dst, int y) {
            uint16_t* d = reinterpret_cast<uint16_t*>(dst);
            if (!f->motion) { std::memset(d, 0, (size_t)w * 4); return; }
            const float* sp = f->motion + (size_t)srow(y) * w * 2;
            for (int x = 0; x < w * 2; ++x) d[x] = to_half(std::isfinite(sp[x]) ? sp[x] : 0.0f);
        });
        g_t.mvecConst = !f->motion;
    }
    g_t.upload->Unmap(0, nullptr);

    const D3D12_RESOURCE_STATES kIn = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    barrier(g_t.color, kIn);
    barrier(g_t.depth, kIn);
    barrier(g_t.mvec, kIn);

    // ---- passes -----------------------------------------------------------
    Tex* input = &g_t.color;
    Tex* output = nullptr;
    for (int p = 0; p < passes; ++p) {
        output = &g_t.out[p & 1];
        barrier(*input, kIn);
        barrier(*output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);

        P_Reset(&g_params);
        P_SetPtr(&g_params, "DLSSNR.Color", input->res);
        P_SetPtr(&g_params, "DLSSNR.Output", output->res);
        P_SetPtr(&g_params, "DLSSNR.MVec", g_t.mvec.res);
        P_SetPtr(&g_params, "DLSSNR.Depth", g_t.depth.res);
        P_SetUI(&g_params, "DLSSNR.Enabled", 1u);
        P_SetUI(&g_params, "DLSSNR.Width", (unsigned)w);
        P_SetUI(&g_params, "DLSSNR.Height", (unsigned)H);
        P_SetUI(&g_params, "DLSSNR.DepthInverted", 0u);
        P_SetUI(&g_params, "DLSSNR.Reset", f->reset ? 1u : 0u);
        for (const char* k : {"DLSSNR.ColorSubrect", "DLSSNR.OutputSubrect", "DLSSNR.DepthSubrect", "DLSSNR.MVecSubrect"}) {
            std::string b(k);
            P_SetUI(&g_params, (b + "BaseX").c_str(), 0u);
            P_SetUI(&g_params, (b + "BaseY").c_str(), 0u);
            P_SetUI(&g_params, (b + "Width").c_str(), (unsigned)w);
            P_SetUI(&g_params, (b + "Height").c_str(), (unsigned)H);
        }
        P_SetF(&g_params, "DLSSNR.MVecScaleX", 1.0f);
        P_SetF(&g_params, "DLSSNR.MVecScaleY", 1.0f);
        P_SetF(&g_params, "DLSS.Pre.Exposure", 1.0f);
        P_SetF(&g_params, "DLSS.Exposure.Scale", 1.0f);
        set_tuning(*s);

        Feature& feat = g_features[f->stream * 64 + p];
        NgxResult r = call_evaluate(feat.handle);
        if (ngx_failed(r)) {
            g_cmd->Close();
            wait_gpu();
            fail(fmt_ngx("EvaluateFeature(DLSS 5)", r));
            // A failed evaluate usually means the feature is unusable; rebuild next time.
            release_feature(feat);
            return 7;
        }
        uav_barrier(*output);
        input = output;
    }

    // ---- readback -----------------------------------------------------------
    barrier(*output, D3D12_RESOURCE_STATE_COPY_SOURCE);
    const UINT pitch = pitch_of(w, 4);
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = output->res;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = g_t.readback;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Footprint.Format = output->fmt;
    dst.PlacedFootprint.Footprint.Width = (UINT)w;
    dst.PlacedFootprint.Footprint.Height = (UINT)h;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = pitch;
    D3D12_BOX box{0, 0, 0, (UINT)w, (UINT)h, 1};   // only the visible part, not the padding
    g_cmd->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
    if (!submit()) return 8;

    uint8_t* rb = nullptr;
    D3D12_RANGE full{0, (SIZE_T)g_t.readbackSize};
    if (FAILED(g_t.readback->Map(0, &full, reinterpret_cast<void**>(&rb)))) { fail("Map(readback) failed"); return 9; }
    for (int y = 0; y < h; ++y)   // back to bottom-up; the caller restores alpha if it needs it
        std::memcpy(out + (size_t)(h - 1 - y) * w * 4, rb + (size_t)y * pitch, (size_t)w * 4);
    g_t.readback->Unmap(0, &none);
    return 0;
}

DLSS5B_API void dlss5b_release_stream(int32_t stream)
{
    if (!g_dev) return;
    wait_gpu();
    for (auto it = g_features.begin(); it != g_features.end();) {
        if (it->first / 64 == stream) { release_feature(it->second); it = g_features.erase(it); }
        else ++it;
    }
}

DLSS5B_API void dlss5b_shutdown(void)
{
    if (g_queue && g_fence) wait_gpu();
    for (auto& kv : g_features) release_feature(kv.second);
    g_features.clear();
    release_targets();
    if (g_ngxInit) { NgxResult r = call_shutdown(); log("Shutdown1 -> 0x%08X", r); }
    g_ngxInit = false;
    safe_release(g_cmd);
    safe_release(g_alloc);
    safe_release(g_queue);
    safe_release(g_fence);
    if (g_fenceEvent) { CloseHandle(g_fenceEvent); g_fenceEvent = nullptr; }
    safe_release(g_dev);
    // nvngx_dlssnr.dll stays loaded: unloading CUDA-backed runtimes mid-process is not safe.
    log("---- shutdown");
}

// ---------------------------------------------------------------------------
// Viewport helper: take a captured viewport image, optionally remove the
// overlay layer Blender blended over it, and resample it to the DLSS size.
//   final = scene * (1 - a) + overlay * a   =>   scene = (final - overlay * a) / (1 - a)
// Where the overlay is (nearly) opaque the scene can't be recovered; those
// pixels keep the captured colour (the overlay is drawn back on top anyway).
// ---------------------------------------------------------------------------
DLSS5B_API void dlss5b_prepare(const uint8_t* src, int32_t sw, int32_t sh, const uint8_t* overlay,
                               uint8_t* dst, int32_t dw, int32_t dh)
{
    if (!src || !dst || sw < 1 || sh < 1 || dw < 1 || dh < 1) return;
    const bool same = (sw == dw && sh == dh);
    std::vector<uint8_t> tmp;
    const uint8_t* clean = src;
    auto uncomp_rows = [&](uint8_t* out, int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const uint8_t* s = src + (size_t)y * sw * 4;
            const uint8_t* o = overlay + (size_t)y * sw * 4;
            uint8_t* d = out + (size_t)y * sw * 4;
            for (int x = 0; x < sw; ++x, s += 4, o += 4, d += 4) {
                const int a = o[3];
                if (a == 0 || a > 242) { d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = 255; continue; }
                const float ia = 255.0f / (255 - a);
                for (int c = 0; c < 3; ++c) {
                    float v = ((float)s[c] - (float)o[c] * a / 255.0f) * ia;
                    d[c] = (uint8_t)std::clamp(v + 0.5f, 0.0f, 255.0f);
                }
                d[3] = 255;
            }
        }
    };
    auto resample_rows = [&](int y0, int y1) {
        const float fx = (float)sw / dw, fy = (float)sh / dh;
        for (int y = y0; y < y1; ++y) {
            const float syf = std::clamp((y + 0.5f) * fy - 0.5f, 0.0f, (float)(sh - 1));
            const int sy0 = (int)syf, sy1 = std::min(sy0 + 1, sh - 1);
            const float ty = syf - sy0;
            uint8_t* d = dst + (size_t)y * dw * 4;
            for (int x = 0; x < dw; ++x, d += 4) {
                const float sxf = std::clamp((x + 0.5f) * fx - 0.5f, 0.0f, (float)(sw - 1));
                const int sx0 = (int)sxf, sx1 = std::min(sx0 + 1, sw - 1);
                const float tx = sxf - sx0;
                const uint8_t* a = clean + ((size_t)sy0 * sw + sx0) * 4;
                const uint8_t* b = clean + ((size_t)sy0 * sw + sx1) * 4;
                const uint8_t* c = clean + ((size_t)sy1 * sw + sx0) * 4;
                const uint8_t* e = clean + ((size_t)sy1 * sw + sx1) * 4;
                for (int k = 0; k < 3; ++k) {
                    const float top = a[k] + (b[k] - a[k]) * tx;
                    const float bot = c[k] + (e[k] - c[k]) * tx;
                    d[k] = (uint8_t)(top + (bot - top) * ty + 0.5f);
                }
                d[3] = 255;
            }
        }
    };
    const int nt = (int)std::clamp(std::thread::hardware_concurrency(), 1u, 8u);
    auto parallel = [&](int rows, auto fn) {
        std::vector<std::thread> ts;
        const int step = (rows + nt - 1) / nt;
        for (int i = 0; i < nt; ++i) {
            const int y0 = i * step, y1 = std::min(rows, y0 + step);
            if (y0 < y1) ts.emplace_back([=, &fn] { fn(y0, y1); });
        }
        for (auto& t : ts) t.join();
    };
    if (overlay) {
        uint8_t* target = dst;
        if (!same) { tmp.resize((size_t)sw * sh * 4); target = tmp.data(); }
        parallel(sh, [&](int y0, int y1) { uncomp_rows(target, y0, y1); });
        clean = target;
        if (same) return;
    } else if (same) {
        std::memcpy(dst, src, (size_t)sw * sh * 4);
        return;
    }
    parallel(dh, [&](int y0, int y1) { resample_rows(y0, y1); });
}

// ---------------------------------------------------------------------------
// Viewport helper: depth + camera motion vectors for the live viewport.
//   depth_src: the viewport depth buffer (sw x sh, GL, 0 = near, rows bottom-up)
//   inv_vp_cur / vp_prev: row-major 4x4 (projection * view) now and at the last
//   DLSS frame. Writes depth (w x h) and motion (w x h x 2, pixel offset to the
//   previous position, +Y down) at the DLSS resolution, rows bottom-up.
// Only camera movement is captured; moving objects get no vectors.
// ---------------------------------------------------------------------------
DLSS5B_API void dlss5b_camera_motion(const float* depth_src, int32_t sw, int32_t sh,
                                     const float* inv_vp_cur, const float* vp_prev,
                                     float* depth_out, float* motion_out, int32_t w, int32_t h)
{
    if (!depth_src || !depth_out || sw < 1 || sh < 1 || w < 1 || h < 1) return;
    const float* A = inv_vp_cur;
    const float* B = vp_prev;
    auto rows = [&](int y0, int y1) {
        for (int y = y0; y < y1; ++y) {
            const float v = (y + 0.5f) / h;
            const int syi = std::min(sh - 1, (int)(v * sh));
            for (int x = 0; x < w; ++x) {
                const float u = (x + 0.5f) / w;
                const int sxi = std::min(sw - 1, (int)(u * sw));
                float d = depth_src[(size_t)syi * sw + sxi];
                if (!std::isfinite(d)) d = 1.0f;
                d = std::clamp(d, 0.0f, 1.0f);
                const size_t i = (size_t)y * w + x;
                depth_out[i] = d;
                if (!motion_out) continue;
                float mx = 0.0f, my = 0.0f;
                if (A && B) {
                    const float n[4] = {2.0f * u - 1.0f, 2.0f * v - 1.0f, 2.0f * d - 1.0f, 1.0f};
                    float p[4];
                    for (int r = 0; r < 4; ++r)
                        p[r] = A[r * 4] * n[0] + A[r * 4 + 1] * n[1] + A[r * 4 + 2] * n[2] + A[r * 4 + 3] * n[3];
                    if (std::fabs(p[3]) > 1e-12f) {
                        for (int r = 0; r < 4; ++r) p[r] /= p[3];
                        float q[4];
                        for (int r = 0; r < 4; ++r)
                            q[r] = B[r * 4] * p[0] + B[r * 4 + 1] * p[1] + B[r * 4 + 2] * p[2] + B[r * 4 + 3] * p[3];
                        if (q[3] > 1e-6f) {
                            const float pu = (q[0] / q[3] + 1.0f) * 0.5f;
                            const float pv = (q[1] / q[3] + 1.0f) * 0.5f;
                            mx = (pu - u) * w;
                            my = -(pv - v) * h;          // GL +Y up -> D3D +Y down
                            if (!std::isfinite(mx) || !std::isfinite(my) || std::fabs(mx) > 4 * w || std::fabs(my) > 4 * h)
                                mx = my = 0.0f;
                        }
                    }
                }
                motion_out[i * 2] = mx;
                motion_out[i * 2 + 1] = my;
            }
        }
    };
    const int nt = (int)std::clamp(std::thread::hardware_concurrency(), 1u, 8u);
    std::vector<std::thread> ts;
    const int step = (h + nt - 1) / nt;
    for (int t = 0; t < nt; ++t) {
        const int y0 = t * step, y1 = std::min(h, y0 + step);
        if (y0 < y1) ts.emplace_back([=, &rows] { rows(y0, y1); });
    }
    for (auto& t : ts) t.join();
}
