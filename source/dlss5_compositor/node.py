"""The DLSS 5 compositor node.

A CompositorNodeCustomGroup: a real node in Add > Filter with its own
properties drawn on it. Inside it is a pass-through group - Blender's
compositor can't run Python per pixel - so the node marks *where* DLSS 5
applies and *how*, and the add-on runs DLSS 5 on the final image:

  * live in the 3D viewport when the viewport's Compositor option is on,
  * on the Render Result after F12,
  * on every written frame of an animation render.

Its optional Depth / Motion inputs are copied to disk during renders by a
small, collapsed File Output node the add-on manages ("DLSS5 guides").
"""

import os
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty

IDNAME = "DLSS5_CompositorNode"
GROUP_NAME = ".DLSS 5 (internal)"
GROUP_VERSION = 3
CAPTURE_TAG = "dlss5_capture_for"
GUIDE_SLOTS = ("Depth", "Motion")
RESULT_IMAGE = "DLSS 5 Result"     # scene-linear DLSS 5 output the group feeds back into the compositor
FLAG_NODE = "DLSS5 Use Result"
FRAME_NODE = "DLSS5 Result Frame"
STYLE_ITEMS = (
    ("0", "Standard", "DLSS 5 default look"),
    ("1", "Natural", "Softer, closer to the original"),
    ("2", "Cinematic", "Stronger contrast and grading"),
)

IS_50 = bpy.app.version >= (5, 0, 0)
# 5.0+: DLSS 5 lands in the real composite (Render Result, Viewer, saved files).
IN_COMPOSITE = IS_50 and hasattr(bpy.types, "CompositorNodeImageInfo")
CAPTURE_SLOTS = (("Image",) if IN_COMPOSITE else ()) + GUIDE_SLOTS


def get_scene_tree(scene, create=False):
    if hasattr(scene, "compositing_node_group"):  # 5.0+
        tree = scene.compositing_node_group
        if tree is None and create:
            tree = bpy.data.node_groups.new("Compositor Nodes", "CompositorNodeTree")
            scene.compositing_node_group = tree
        return tree
    if create and not scene.use_nodes:
        scene.use_nodes = True
    return scene.node_tree


def is_dlss_node(n):
    return n.bl_idname == IDNAME


def iter_dlss_nodes(tree):
    if tree is None:
        return
    for n in tree.nodes:
        if n.bl_idname == IDNAME:
            yield n


_OUTPUT_TYPES = {"CompositorNodeComposite", "NodeGroupOutput", "CompositorNodeViewer"}


def in_path(node):
    """True if the node's output reaches the compositor's output."""
    seen, todo = set(), [node]
    while todo:
        n = todo.pop()
        for out in n.outputs:
            for link in out.links:
                if not link.is_valid or link.is_muted:
                    continue
                to = link.to_node
                if to.bl_idname in _OUTPUT_TYPES:
                    return True
                if to.as_pointer() not in seen:
                    seen.add(to.as_pointer())
                    todo.append(to)
    return False


def find_node(scene):
    """The DLSS 5 node in the scene's compositor (first one in the output path, else the first one)."""
    nodes = [n for n in iter_dlss_nodes(get_scene_tree(scene)) if not n.mute]
    for n in nodes:
        if in_path(n):
            return n
    return nodes[0] if nodes else None


def active_node(scene):
    """The node that should be applied right now: in the path, not muted, and switched on."""
    n = find_node(scene)
    if n is None or not n.enabled or not in_path(n):
        return None
    return n


def read_settings(node):
    return {
        "intensity": float(node.intensity),
        "structure": float(node.local_structure),
        "tone": float(node.local_tone),
        "style": int(node.style),
        "passes": int(node.model_passes),
        "skin": float(node.skin_structure) if node.use_skin else -1.0,
        "auto_mask": bool(node.auto_mask),
        "preset": 1,
    }


# --------------------------------------------------------------------------
# Internal pass-through group (shared by every DLSS 5 node)
# --------------------------------------------------------------------------

def _socket(ng, name, in_out, stype, **kw):
    s = ng.interface.new_socket(name, in_out=in_out, socket_type=stype)
    for k, v in kw.items():
        try:
            setattr(s, k, v)
        except (AttributeError, TypeError, ValueError):
            pass
    return s


def shared_group():
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is not None and ng.get("dlss5_version") == GROUP_VERSION:
        return ng
    wanted = [("Image", "INPUT", "NodeSocketColor"), ("Depth", "INPUT", "NodeSocketFloat"),
              ("Motion", "INPUT", "NodeSocketVector"), ("Image", "OUTPUT", "NodeSocketColor")]
    if ng is None:
        ng = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    else:
        ng.nodes.clear()
    have = [(i.name, i.in_out, getattr(i, "socket_type", "")) for i in ng.interface.items_tree
            if getattr(i, "item_type", "SOCKET") == "SOCKET"]
    if sorted(have) != sorted(wanted):
        # Only when the sockets really differ: recreating them drops the links
        # of every DLSS 5 node in the file.
        ng.interface.clear()
        _socket(ng, "Image", "INPUT", "NodeSocketColor", description="The image DLSS 5 enhances")
        _socket(ng, "Depth", "INPUT", "NodeSocketFloat", hide_value=True,
                description="Optional: Render Layers > Depth, helps DLSS 5 understand the scene")
        _socket(ng, "Motion", "INPUT", "NodeSocketVector", hide_value=True,
                description="Optional: Render Layers > Vector, keeps animations stable")
        _socket(ng, "Image", "OUTPUT", "NodeSocketColor")
    ng["dlss5_version"] = GROUP_VERSION
    gin = ng.nodes.new("NodeGroupInput")
    gin.location = (-800, 0)
    gout = ng.nodes.new("NodeGroupOutput")
    gout.location = (400, 0)
    if not IN_COMPOSITE:
        ng.links.new(gin.outputs["Image"], gout.inputs["Image"])
        return ng
    # The add-on writes the DLSS 5 result (scene-linear) into an image after a
    # render; the group swaps it in when it belongs to this render: the flag is
    # on, the frame matches and the sizes match (so the viewport compositor,
    # which runs at viewport size, always passes the input through).
    N, L = ng.nodes, ng.links
    img = N.new("CompositorNodeImage")
    img.name = img.label = "DLSS5 Result"
    img.image = result_image()
    img.location = (-600, -250)
    info_in = N.new("CompositorNodeImageInfo")
    info_in.location = (-600, 250)
    info_res = N.new("CompositorNodeImageInfo")
    info_res.location = (-400, -250)
    dist = N.new("ShaderNodeVectorMath")
    dist.operation = "DISTANCE"
    dist.location = (-200, 250)
    same = N.new("ShaderNodeMath")
    same.operation = "LESS_THAN"
    same.inputs[1].default_value = 0.5
    same.location = (0, 250)
    flag = N.new("ShaderNodeValue")
    flag.name = flag.label = FLAG_NODE
    flag.outputs[0].default_value = 0.0
    flag.location = (-400, 450)
    frame = N.new("ShaderNodeValue")
    frame.name = frame.label = FRAME_NODE
    frame.outputs[0].default_value = -1.0
    frame.location = (-400, 600)
    now = N.new("CompositorNodeSceneTime")
    now.location = (-600, 600)
    same_frame = N.new("ShaderNodeMath")
    same_frame.operation = "COMPARE"
    same_frame.inputs[2].default_value = 0.5
    same_frame.location = (-200, 600)
    m1 = N.new("ShaderNodeMath")
    m1.operation = "MULTIPLY"
    m1.location = (0, 500)
    m2 = N.new("ShaderNodeMath")
    m2.operation = "MULTIPLY"
    m2.location = (200, 400)
    sw = N.new("CompositorNodeSwitch")
    sw.location = (200, 0)
    L.new(gin.outputs["Image"], info_in.inputs[0])
    L.new(img.outputs["Image"], info_res.inputs[0])
    L.new(info_in.outputs["Dimensions"], dist.inputs[0])
    L.new(info_res.outputs["Dimensions"], dist.inputs[1])
    L.new(dist.outputs["Value"], same.inputs[0])
    L.new(now.outputs["Frame"], same_frame.inputs[0])
    L.new(frame.outputs[0], same_frame.inputs[1])
    L.new(flag.outputs[0], m1.inputs[0])
    L.new(same_frame.outputs[0], m1.inputs[1])
    L.new(m1.outputs[0], m2.inputs[0])
    L.new(same.outputs[0], m2.inputs[1])
    L.new(m2.outputs[0], sw.inputs["Switch"])
    L.new(gin.outputs["Image"], sw.inputs["Off"])
    L.new(img.outputs["Image"], sw.inputs["On"])
    L.new(sw.outputs[0], gout.inputs["Image"])
    return ng


def result_image(size=None):
    """The float image the group reads the DLSS 5 result from (resized to `size`)."""
    img = bpy.data.images.get(RESULT_IMAGE)
    if img is not None and not img.is_float:
        bpy.data.images.remove(img)
        img = None
    if img is None:
        w, h = size or (4, 4)
        img = bpy.data.images.new(RESULT_IMAGE, w, h, alpha=True, float_buffer=True)
    elif size is not None and tuple(img.size) != tuple(size):
        img.scale(*size)
    ng = bpy.data.node_groups.get(GROUP_NAME)
    n = ng.nodes.get("DLSS5 Result") if ng is not None else None
    if n is not None and n.image != img:
        n.image = img
    return img


def set_result_state(on, frame=None):
    """Tell the group whether to use the stored result (triggers a re-composite)."""
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is None or not IN_COMPOSITE:
        return
    flag = ng.nodes.get(FLAG_NODE)
    fr = ng.nodes.get(FRAME_NODE)
    if fr is not None and frame is not None and fr.outputs[0].default_value != float(frame):
        fr.outputs[0].default_value = float(frame)
    if flag is not None:
        v = 1.0 if on else 0.0
        if flag.outputs[0].default_value != v:
            flag.outputs[0].default_value = v


def result_state():
    ng = bpy.data.node_groups.get(GROUP_NAME)
    flag = ng.nodes.get(FLAG_NODE) if ng is not None else None
    return bool(flag and flag.outputs[0].default_value > 0.5)


def refresh_composite():
    """Make the compositor run again on the current Render Result."""
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is None:
        return
    fr = ng.nodes.get(FRAME_NODE)
    img = ng.nodes.get("DLSS5 Result")
    if img is not None:
        img.image = img.image          # re-assign: tags the tree
    ng.update_tag()
    for scene in bpy.data.scenes:
        t = get_scene_tree(scene)
        if t is not None:
            t.update_tag()
    if fr is not None:                 # value nudge, the most reliable trigger
        v = fr.outputs[0].default_value
        fr.outputs[0].default_value = v + 1e-3
        fr.outputs[0].default_value = v


# --------------------------------------------------------------------------
# The node
# --------------------------------------------------------------------------

def _changed(self, context):
    from . import engine, render
    engine.redraw_all()
    render.settings_changed(self)


def _toggled(self, context):
    from . import engine, render
    engine.redraw_all()
    render.enabled_changed(self)


class DLSS5CompositorNode(bpy.types.CompositorNodeCustomGroup):
    """NVIDIA DLSS 5 neural rendering"""
    bl_idname = IDNAME
    bl_label = "DLSS 5"
    bl_icon = "SHADERFX"
    bl_width_default = 240

    enabled: BoolProperty(name="DLSS 5", default=True,
                          description="Turn DLSS 5 on/off to compare (A/B) in the viewport and render window",
                          update=_toggled)
    intensity: FloatProperty(name="Intensity", default=1.0, soft_min=0.0, soft_max=1.0, min=-100.0, max=100.0, subtype="FACTOR",
                             description="Overall strength of DLSS 5", update=_changed)
    local_structure: FloatProperty(name="Local Structure", default=1.0, soft_min=0.0, soft_max=1.0, min=-100.0, max=100.0, subtype="FACTOR",
                                   description="Fine detail: contact shadows, micro-occlusion, texture", update=_changed)
    local_tone: FloatProperty(name="Local Tone", default=0.5, soft_min=0.0, soft_max=1.0, min=-100.0, max=100.0, subtype="FACTOR",
                              description="Large-scale lighting and colour response", update=_changed)
    style: EnumProperty(name="Style", items=STYLE_ITEMS, default="1", update=_changed)
    model_passes: IntProperty(name="Model Passes", default=1, min=1, max=16, soft_min=1, soft_max=4,
                              description="How many times DLSS 5 runs over its own output (type up to 16)",
                              update=_changed)
    use_skin: BoolProperty(name="Custom Skin Detail", default=False, update=_changed)
    skin_structure: FloatProperty(name="Skin Detail", default=0.5, soft_min=0.0, soft_max=1.0, min=-100.0, max=100.0, subtype="FACTOR",
                                  update=_changed)
    auto_mask: BoolProperty(name="Auto Mask", default=True,
                            description="Let DLSS 5 find people, skin and sky by itself", update=_changed)

    def init(self, context):
        self.node_tree = shared_group()
        self.width = 240

    def copy(self, node):
        self.node_tree = shared_group()

    def draw_label(self):
        return "DLSS 5"

    def draw_buttons(self, context, layout):
        from . import prefs
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.prop(self, "enabled", text="DLSS 5 On" if self.enabled else "DLSS 5 Off (showing original)",
                 toggle=True, icon="HIDE_OFF" if self.enabled else "HIDE_ON")
        if not in_path(self):
            layout.label(text="Connect the output to use it", icon="ERROR")
        col = layout.column(align=True)
        col.active = self.enabled
        col.prop(self, "intensity", slider=True)
        col.prop(self, "local_structure", slider=True)
        col.prop(self, "local_tone", slider=True)
        layout.prop(self, "style", text="")
        layout.prop(self, "model_passes")
        prefs.draw_dll_box(layout, context)

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)
        box = layout.box()
        box.label(text="Advanced")
        box.prop(self, "auto_mask")
        row = box.row(align=True)
        row.prop(self, "use_skin", text="")
        sub = row.row()
        sub.active = self.use_skin
        sub.prop(self, "skin_structure", slider=True)


# --------------------------------------------------------------------------
# Guide capture (Depth / Motion -> EXR during renders)
# --------------------------------------------------------------------------

def _safe(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def capture_dir(scene, node):
    blend = _safe(os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "untitled")
    return os.path.join(tempfile.gettempdir(), "blender_dlss5", blend, _safe(scene.name), _safe(node.name))


def _set(obj, attr, value):
    if hasattr(obj, attr) and getattr(obj, attr) != value:
        try:
            setattr(obj, attr, value)
        except (AttributeError, TypeError, ValueError):
            pass


def _configure_file_output(fo, directory):
    fmt = fo.format
    _set(fmt, "media_type", "IMAGE")
    _set(fmt, "file_format", "OPEN_EXR")
    _set(fmt, "color_depth", "32")
    _set(fmt, "color_mode", "RGBA")
    _set(fmt, "exr_codec", "ZIP")
    _set(fo, "save_as_render", False)
    if IS_50:
        _set(fo, "directory", directory + os.sep)
        _set(fo, "file_name", "")
        items = fo.file_output_items
        if [i.name for i in items] != list(CAPTURE_SLOTS):
            items.clear()
            for slot in CAPTURE_SLOTS:
                items.new("RGBA", slot)
    else:
        _set(fo, "base_path", directory + os.sep)
        slots = fo.file_slots
        wanted = [s + "_" for s in CAPTURE_SLOTS]
        if [s.path for s in slots] != wanted:
            if hasattr(slots, "clear"):
                slots.clear()
            else:
                while len(fo.inputs):
                    slots.remove(fo.inputs[0])
            for w in wanted:
                slots.new(w)


def _find_capture(tree, node):
    for n in tree.nodes:
        if n.bl_idname == "CompositorNodeOutputFile" and n.get(CAPTURE_TAG) == node.name:
            return n
    return None


def sync_capture(scene, tree, node):
    """Keep the guide capture node wired like the DLSS node. Main thread only."""
    wanted = [s for s in CAPTURE_SLOTS if node.inputs.get(s) is not None and node.inputs[s].is_linked]
    fo = _find_capture(tree, node)
    if not wanted:
        if fo is not None:
            tree.nodes.remove(fo)
        if node.get("dlss5_connected", "") != "":
            node["dlss5_connected"] = ""
        return
    if fo is None:
        fo = tree.nodes.new("CompositorNodeOutputFile")
        fo[CAPTURE_TAG] = node.name
        fo.label = "DLSS 5 capture (auto)"
        fo.name = "DLSS5 Guides " + node.name
        fo.location = (node.location.x, node.location.y - 280)
        fo.hide = True
        fo.width = 140
    _configure_file_output(fo, capture_dir(scene, node))
    for i, slot in enumerate(CAPTURE_SLOTS):
        src = node.inputs[slot]
        dst = fo.inputs[i]
        want = src.links[0].from_socket if src.is_linked else None
        have = dst.links[0].from_socket if dst.is_linked else None
        same = (want is None and have is None) or (want is not None and have is not None and want == have)
        if not same:
            for l in list(dst.links):
                tree.links.remove(l)
            if want is not None:
                tree.links.new(want, dst)
    joined = ",".join(wanted)
    if node.get("dlss5_connected") != joined:
        node["dlss5_connected"] = joined


def _upstream_render_layers(tree, node):
    todo, seen = [node], set()
    while todo:
        n = todo.pop(0)
        for inp in n.inputs:
            if n is node and inp.name != "Image":
                continue
            for link in inp.links:
                src = link.from_node
                if src.bl_idname == "CompositorNodeRLayers":
                    return src
                if src.as_pointer() not in seen:
                    seen.add(src.as_pointer())
                    todo.append(src)
    return next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)


def connect_guides(scene, node):
    """Switch on the Z and Vector passes and plug Depth / Motion into the node.
    Returns a short message (or "" when nothing needed doing)."""
    tree = node.id_data
    rl = _upstream_render_layers(tree, node)
    if rl is None:
        return "No Render Layers node to take Depth and Motion from"
    vl = scene.view_layers.get(getattr(rl, "layer", "")) or scene.view_layers[0]
    changed = []
    for attr in ("use_pass_z", "use_pass_vector"):
        if hasattr(vl, attr) and not getattr(vl, attr):
            setattr(vl, attr, True)
            changed.append(attr)
    for src_name, dst_name in (("Depth", "Depth"), ("Vector", "Motion")):
        dst = node.inputs.get(dst_name)
        src = rl.outputs.get(src_name)
        if dst is None or src is None or dst.is_linked:
            continue
        tree.links.new(src, dst)
        changed.append(dst_name)
    sync_capture(scene, tree, node)
    note = ""
    if scene.render.engine == "CYCLES" and getattr(scene.render, "use_motion_blur", False):
        note = " (Cycles leaves the Vector pass empty while Motion Blur is on)"
    return ("Connected Depth & Motion" + note) if changed else note.strip(" ()")


def guides_connected(node):
    return all(node.inputs.get(n) is not None and node.inputs[n].is_linked for n in GUIDE_SLOTS)


def sync_all(scene):
    tree = get_scene_tree(scene)
    if tree is None:
        return
    names = set()
    for node in list(iter_dlss_nodes(tree)):
        names.add(node.name)
        sync_capture(scene, tree, node)
    for n in list(tree.nodes):
        if n.bl_idname == "CompositorNodeOutputFile":
            tag = n.get(CAPTURE_TAG)
            if tag is not None and tag not in names:
                tree.nodes.remove(n)


def guide_files(scene, node, frame):
    d = capture_dir(scene, node)
    connected = [c for c in str(node.get("dlss5_connected", "")).split(",") if c]
    out = {}
    for slot in connected:
        numbered = [f"{slot}_{frame:04d}.exr", f"{slot}{frame:04d}.exr"]
        found = next((os.path.join(d, c) for c in numbered if os.path.exists(os.path.join(d, c))), None)
        plain = os.path.join(d, f"{slot}.exr")
        if os.path.exists(plain) and (found is None or os.path.getmtime(plain) > os.path.getmtime(found)):
            found = plain
        if found:
            out[slot] = found
    return out


def register():
    bpy.utils.register_class(DLSS5CompositorNode)


def unregister():
    bpy.utils.unregister_class(DLSS5CompositorNode)
