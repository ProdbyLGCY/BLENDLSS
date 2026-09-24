import os
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(ADDON_DIR, "bin")
BUNDLED_DLL = os.path.join(BIN_DIR, "nvngx_dlssnr.dll")
BRIDGE_DLL = os.path.join(BIN_DIR, "blendlss5_nvngx.dll")


def get_prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def dll_path():
    """The nvngx_dlssnr.dll to use: the one picked in preferences, else the bundled copy."""
    p = get_prefs()
    if p and p.dll_path:
        return bpy.path.abspath(p.dll_path)
    return BUNDLED_DLL


def data_dir():
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(root, "BlenDLSS5")


def _redraw():
    from . import engine
    engine.redraw_all()


def _reload(self, _context):
    from . import engine
    engine.shutdown()
    engine.redraw_all()


class DLSS5_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    dll_path: StringProperty(
        name="DLSS 5 DLL",
        description="nvngx_dlssnr.dll to load. Leave empty to use the copy that ships with the add-on",
        subtype="FILE_PATH",
        update=_reload,
    )
    backend: EnumProperty(
        name="Backend",
        items=(
            ("AUTO", "Auto", "NVIDIA DLSS 5 when it loads, CPU preview otherwise"),
            ("NVIDIA", "DLSS 5 only", "Only use NVIDIA DLSS 5"),
            ("PREVIEW", "CPU preview", "Built-in approximation (not DLSS), for machines without an RTX GPU"),
        ),
        default="AUTO",
        update=_reload,
    )
    viewport_live: BoolProperty(
        name="Live in the viewport",
        description="Run DLSS 5 on the 3D viewport whenever its Compositor option is on (Solid, Material Preview, EEVEE and Cycles)",
        default=True,
        update=lambda self, context: _redraw(),
    )
    viewport_scale: EnumProperty(
        name="Viewport Quality",
        description="Resolution DLSS 5 runs at in the live viewport (lower = smoother)",
        items=(
            ("1.0", "Native (100%)", "Full viewport resolution"),
            ("0.75", "Balanced (75%)", "Good balance of sharpness and speed"),
            ("0.5", "Performance (50%)", "Smoothest"),
        ),
        default="0.75",
        update=lambda self, context: _redraw(),
    )
    viewport_compat: BoolProperty(
        name="Compatibility Mode",
        description=("Run DLSS 5 on the whole viewport image, overlays and gizmos included. "
                     "Simplest and fastest path - use it if the default mode misbehaves"),
        default=False,
        update=lambda self, context: _redraw(),
    )
    use_guides: BoolProperty(
        name="Depth & Motion",
        description=("Give DLSS 5 the scene's depth and motion: the Z and Vector passes are switched on "
                     "and plugged into the node for renders, and the live viewport uses its depth and "
                     "camera motion. Steadier results, a few ms slower"),
        default=True,
        update=lambda self, context: _redraw(),
    )
    show_stats: BoolProperty(
        name="Performance Readout",
        description="Show frame rate and DLSS 5 timings in the corner of the viewport",
        default=False,
        update=lambda self, context: _redraw(),
    )
    keep_originals: BoolProperty(
        name="Keep un-enhanced frames",
        description="When rendering animations, copy each original frame into a 'no_dlss' folder before it is replaced",
        default=False,
    )
    invert_motion: BoolProperty(
        name="Invert motion vectors",
        description="Flip the direction of Blender's Vector pass before handing it to DLSS 5 (try this if moving objects smear)",
        default=False,
    )

    def draw(self, context):
        draw_dll_box(self.layout, context)
        col = self.layout.column()
        col.prop(self, "viewport_scale")
        col.prop(self, "show_stats")
        col.prop(self, "use_guides")
        col.prop(self, "viewport_compat")
        col.prop(self, "keep_originals")
        col.prop(self, "invert_motion")
        col.prop(self, "backend")


def draw_dll_box(layout, context):
    """The DLL picker + status, shared by preferences, the node and the sidebar."""
    from . import engine
    p = get_prefs()
    box = layout.box()
    if p is not None:
        box.prop(p, "dll_path", text="DLSS 5 DLL")
    path = dll_path()
    exists = os.path.isfile(path)
    row = box.row()
    row.label(text=("Bundled: " if not (p and p.dll_path) else "Using: ") + os.path.basename(path)
              + ("" if exists else "  (missing)"), icon="FILE" if exists else "ERROR")
    st = engine.status()
    row = box.row()
    row.label(text=st["message"][:90], icon="CHECKMARK" if st["nvidia"] else "INFO")
    row.operator("dlss5.reload_runtime", text="", icon="FILE_REFRESH")


def register():
    bpy.utils.register_class(DLSS5_Preferences)


def unregister():
    bpy.utils.unregister_class(DLSS5_Preferences)
