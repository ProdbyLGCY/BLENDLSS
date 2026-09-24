import os

import bpy
import bpy.utils.previews

from . import engine, node as dnode, prefs, render


def _is_compositor(context):
    sd = context.space_data
    return sd is not None and sd.type == "NODE_EDITOR" and sd.tree_type == "CompositorNodeTree"


class DLSS5_OT_add_node(bpy.types.Operator):
    bl_idname = "dlss5.add_node"
    bl_label = "DLSS 5"
    bl_description = "Add the NVIDIA DLSS 5 node"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _is_compositor(context)

    def invoke(self, context, event):
        space = context.space_data
        tree = space.edit_tree
        if tree is None:
            tree = dnode.get_scene_tree(context.scene, create=True)
            try:
                space.node_tree = tree
            except Exception:
                pass
        for n in tree.nodes:
            n.select = False
        node = tree.nodes.new(dnode.IDNAME)
        node.location = space.cursor_location
        node.select = True
        tree.nodes.active = node

        # Drop it between Render Layers and the output if that's the obvious spot.
        rl = next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)
        if rl is not None:
            img_out = rl.outputs.get("Image")
            targets = [l for l in tree.links if l.from_socket == img_out
                       and l.to_node.bl_idname in ("CompositorNodeComposite", "NodeGroupOutput")]
            if img_out is not None:
                tree.links.new(img_out, node.inputs["Image"])
                for l in targets:
                    to_sock = l.to_socket
                    tree.links.remove(l)
                    tree.links.new(node.outputs["Image"], to_sock)
            for src, dst in (("Depth", "Depth"), ("Vector", "Motion")):
                o = rl.outputs.get(src)
                if o is not None and o.enabled:
                    tree.links.new(o, node.inputs[dst])
        if tree == dnode.get_scene_tree(context.scene):
            dnode.sync_capture(context.scene, tree, node)
        bpy.ops.node.translate_attach_remove_on_cancel("INVOKE_DEFAULT")
        return {"FINISHED"}


class DLSS5_OT_setup(bpy.types.Operator):
    bl_idname = "dlss5.setup"
    bl_label = "Add DLSS 5 to Compositor"
    bl_description = "Turn on the compositor and insert a DLSS 5 node between Render Layers and the output"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        tree = dnode.get_scene_tree(scene, create=True)
        rl = next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)
        if rl is None:
            rl = tree.nodes.new("CompositorNodeRLayers")
            rl.location = (-400, 0)
        out = next((n for n in tree.nodes if n.bl_idname in ("CompositorNodeComposite", "NodeGroupOutput")), None)
        if out is None:
            if dnode.IS_50:
                if not any(i.in_out == "OUTPUT" for i in tree.interface.items_tree):
                    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
                out = tree.nodes.new("NodeGroupOutput")
            else:
                out = tree.nodes.new("CompositorNodeComposite")
            out.location = (400, 0)
        node = tree.nodes.new(dnode.IDNAME)
        node.location = ((rl.location.x + out.location.x) / 2, rl.location.y)
        src = out.inputs[0].links[0].from_socket if out.inputs[0].is_linked else rl.outputs["Image"]
        tree.links.new(src, node.inputs["Image"])
        tree.links.new(node.outputs["Image"], out.inputs[0])
        p = prefs.get_prefs()
        if p is None or p.use_guides:
            dnode.connect_guides(scene, node)
        dnode.sync_capture(scene, tree, node)
        # Make the live viewport show it straight away.
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    apply_viewport_compositor(area.spaces.active)
        engine.redraw_all()
        return {"FINISHED"}


def apply_viewport_compositor(space, force=False):
    """Turning DLSS 5 on switches a 3D view's Compositor to Always (a view set
    to Camera by hand is left alone)."""
    if space is None or space.type != "VIEW_3D":
        return
    if space.shading.use_compositor == "DISABLED":
        space.shading.use_compositor = "ALWAYS"


class DLSS5_OT_connect_guides(bpy.types.Operator):
    bl_idname = "dlss5.connect_guides"
    bl_label = "Connect Depth & Motion"
    bl_description = ("Switch on the Z and Vector render passes and plug them into the DLSS 5 node "
                      "(more accurate stills, stable animations)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        node = dnode.find_node(context.scene)
        if node is None:
            self.report({"WARNING"}, "Add the DLSS 5 node first")
            return {"CANCELLED"}
        msg = dnode.connect_guides(context.scene, node)
        if msg:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


class DLSS5_OT_reload_runtime(bpy.types.Operator):
    bl_idname = "dlss5.reload_runtime"
    bl_label = "Reload DLSS 5"
    bl_description = "Reload nvngx_dlssnr.dll"

    def execute(self, context):
        engine.shutdown()
        st = engine.status()
        self.report({"INFO"} if st["nvidia"] else {"WARNING"}, st["message"])
        engine.redraw_all()
        return {"FINISHED"}


class DLSS5_OT_apply_render(bpy.types.Operator):
    bl_idname = "dlss5.apply_render"
    bl_label = "Run DLSS 5 on Render"
    bl_description = "Run DLSS 5 on the current Render Result again with the node's current settings"

    def execute(self, context):
        try:
            out = render.process_still(context.scene)
        except Exception as ex:  # noqa: BLE001
            self.report({"ERROR"}, f"DLSS 5 failed: {ex}")
            return {"CANCELLED"}
        if out is None:
            self.report({"WARNING"}, "Render something first (F12) and add a DLSS 5 node")
            return {"CANCELLED"}
        return {"FINISHED"}


class DLSS5_OT_toggle_render_view(bpy.types.Operator):
    bl_idname = "dlss5.toggle_render_view"
    bl_label = "Compare"
    bl_description = "Switch this image editor between the DLSS 5 render and the original Render Result"

    def execute(self, context):
        node = dnode.find_node(context.scene)
        if node is not None:
            node.enabled = not node.enabled      # swaps every render window + the viewport
            return {"FINISHED"}
        sp = context.space_data
        rr = bpy.data.images.get("Render Result")
        dl = bpy.data.images.get(render.RESULT_IMAGE)
        if sp.image == dl and rr is not None:
            sp.image = rr
        elif dl is not None:
            sp.image = dl
        return {"FINISHED"}


class DLSS5_OT_viewport_toggle(bpy.types.Operator):
    bl_idname = "dlss5.viewport_toggle"
    bl_label = "DLSS 5"
    bl_description = ("Turn DLSS 5 on or off (viewport and renders). "
                      "Adds the DLSS 5 node and switches on the viewport compositor if needed")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        node = dnode.find_node(scene)
        if node is None or not dnode.in_path(node):
            bpy.ops.dlss5.setup()
            node = dnode.find_node(scene)
            if node is None:
                self.report({"WARNING"}, "Couldn't add the DLSS 5 node")
                return {"CANCELLED"}
            turn_on = True
        else:
            turn_on = not (node.enabled and _viewport_on(context))
        if turn_on:
            node.enabled = True
            p = prefs.get_prefs()
            if p is not None and not p.viewport_live:
                p.viewport_live = True
            if (p is None or p.use_guides) and not dnode.guides_connected(node):
                dnode.connect_guides(scene, node)
            sp = context.space_data
            if sp is not None and sp.type == "VIEW_3D":
                apply_viewport_compositor(sp, force=True)
            else:                      # clicked from a sidebar elsewhere: every 3D view
                for win in context.window_manager.windows:
                    for area in win.screen.areas:
                        if area.type == "VIEW_3D":
                            apply_viewport_compositor(area.spaces.active, force=True)
        else:
            node.enabled = False
        engine.redraw_all()
        return {"FINISHED"}


def _viewport_on(context):
    sp = context.space_data
    p = prefs.get_prefs()
    if p is None or not p.viewport_live:
        return False
    if sp is not None and sp.type == "VIEW_3D":
        return sp.shading.use_compositor != "DISABLED"
    return True


def _is_on(context):
    node = dnode.find_node(context.scene)
    return node is not None and node.enabled and dnode.in_path(node) and _viewport_on(context)


def big_toggle(layout, context, scale=1.4):
    """The green DLSS 5 ON / OFF button (header and sidebars)."""
    on = _is_on(context)
    try:
        icon = _icon("dlss_on" if on else "dlss_off")
    except Exception:  # noqa: BLE001
        icon = 0
    kw = {"icon_value": icon} if icon else {"icon": "SHADERFX"}
    row = layout.row()
    row.scale_y = scale
    row.operator("dlss5.viewport_toggle", text="DLSS 5 ON" if on else "DLSS 5 OFF", **kw)


_icons = None


def _icon(name):
    global _icons
    if _icons is None:
        _icons = bpy.utils.previews.new()
        d = os.path.join(os.path.dirname(__file__), "icons")
        for n in ("dlss_on", "dlss_off"):
            _icons.load(n, os.path.join(d, n + ".png"), "IMAGE")
    return _icons[name].icon_id


def _view3d_header(self, context):
    """Green DLSS 5 button at the right end of the 3D viewport header."""
    self.layout.separator(factor=0.5)
    big_toggle(self.layout, context, scale=1.0)


def draw_settings(layout, context, node):
    """The node's controls - shared by the node, the compositor sidebar and Render Properties."""
    node.draw_buttons_ext(context, layout)
    st = node.get("dlss5_status")
    if st:
        layout.label(text=st[:90], icon="INFO")


def _this_view(context):
    """The 3D view the sidebar talks about: this one, or the biggest in the window."""
    sp = context.space_data
    if sp is not None and sp.type == "VIEW_3D":
        return sp
    win = context.window
    if win is None:
        return None
    areas = [a for a in win.screen.areas if a.type == "VIEW_3D"]
    return max(areas, key=lambda a: a.width * a.height).spaces.active if areas else None


def draw_sidebar(layout, context, node=None):
    """Everything DLSS 5, shared by the 3D viewport sidebar, the compositor sidebar
    and Render Properties."""
    scene = context.scene
    node = node or dnode.find_node(scene)
    p = prefs.get_prefs()
    big_toggle(layout, context)
    if node is None:
        layout.label(text="No DLSS 5 node in the compositor yet")
        layout.operator("dlss5.setup", icon="ADD")
        _advanced(layout, context, None, p)
        return
    if not dnode.in_path(node):
        layout.label(text="The DLSS 5 node isn't connected to the output", icon="ERROR")

    col = layout.column(align=True)
    col.active = node.enabled
    col.prop(node, "intensity", slider=True)
    col.prop(node, "local_structure", slider=True)
    col.prop(node, "local_tone", slider=True)
    col = layout.column()
    col.prop(node, "style", text="Style")
    if p is not None:
        col.prop(p, "viewport_scale", text="Quality")
    col.prop(node, "model_passes")
    sp = _this_view(context)
    if sp is not None:
        col.prop(sp.shading, "use_compositor", text="This View")
    if p is not None:
        col.prop(p, "show_stats")
    st = node.get("dlss5_status")
    if st:
        layout.label(text=st[:90], icon="INFO")
    _advanced(layout, context, node, p)


def _advanced(layout, context, node, p):
    """Collapsible Advanced section (closed by default)."""
    if hasattr(layout, "panel"):
        header, body = layout.panel("DLSS5_advanced", default_closed=True)
        header.label(text="Advanced", icon="PREFERENCES")
    else:                                            # Blender before 4.1
        body = layout.box()
        body.label(text="Advanced", icon="PREFERENCES")
    if body is None:
        return
    if p is not None:
        body.prop(p, "use_guides")
        body.prop(p, "viewport_compat")
    if node is not None:
        body.prop(node, "auto_mask")
        row = body.row(align=True)
        row.prop(node, "use_skin", text="")
        sub = row.row()
        sub.active = node.use_skin
        sub.prop(node, "skin_structure", slider=True)
    prefs.draw_dll_box(body, context)


class DLSS5_PT_view3d(bpy.types.Panel):
    bl_label = "DLSS 5"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "DLSS 5"

    def draw(self, context):
        draw_sidebar(self.layout, context)


class DLSS5_PT_render_props(bpy.types.Panel):
    bl_label = "DLSS 5"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw_header(self, context):
        node = dnode.find_node(context.scene)
        if node is not None:
            self.layout.prop(node, "enabled", text="")

    def draw(self, context):
        self.layout.use_property_split = False
        draw_sidebar(self.layout, context)


class DLSS5_PT_node_panel(bpy.types.Panel):
    bl_label = "DLSS 5"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "DLSS 5"

    @classmethod
    def poll(cls, context):
        return _is_compositor(context)

    def draw(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.active if tree else None
        if node is None or not dnode.is_dlss_node(node):
            node = None
        draw_sidebar(self.layout, context, node)


class DLSS5_PT_image_panel(bpy.types.Panel):
    bl_label = "DLSS 5"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "DLSS 5"

    @classmethod
    def poll(cls, context):
        img = context.space_data.image
        return img is not None and (img.type == "RENDER_RESULT" or img.name == render.RESULT_IMAGE)

    def draw(self, context):
        layout = self.layout
        showing = context.space_data.image.name == render.RESULT_IMAGE
        layout.label(text="Showing: " + ("DLSS 5" if showing else "Original"))
        node = dnode.find_node(context.scene)
        if node is not None:
            layout.prop(node, "enabled", text="DLSS 5 On" if node.enabled else "DLSS 5 Off", toggle=True)
            draw_settings(layout, context, node)


def _menu_draw(self, context):
    if _is_compositor(context):
        self.layout.separator()
        self.layout.operator("dlss5.add_node", text="DLSS 5", icon="SHADERFX")


def _header_draw(self, context):
    img = context.space_data.image
    if img is not None and (img.type == "RENDER_RESULT" or img.name == render.RESULT_IMAGE) \
            and bpy.data.images.get(render.RESULT_IMAGE) is not None:
        showing = img.name == render.RESULT_IMAGE
        self.layout.operator("dlss5.toggle_render_view", text="DLSS 5" if showing else "Original",
                             icon="SHADERFX", depress=showing)


CLASSES = (
    DLSS5_OT_setup,
    DLSS5_OT_add_node,
    DLSS5_OT_reload_runtime,
    DLSS5_OT_apply_render,
    DLSS5_OT_toggle_render_view,
    DLSS5_OT_viewport_toggle,
    DLSS5_OT_connect_guides,
    DLSS5_PT_view3d,
    DLSS5_PT_render_props,
    DLSS5_PT_node_panel,
    DLSS5_PT_image_panel,
)
_MENUS = ("NODE_MT_add", "NODE_MT_category_compositor_filter")


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    for name in _MENUS:
        m = getattr(bpy.types, name, None)
        if m is not None:
            m.append(_menu_draw)
    if hasattr(bpy.types, "IMAGE_HT_header"):
        bpy.types.IMAGE_HT_header.append(_header_draw)
    bpy.types.VIEW3D_HT_header.append(_view3d_header)


def unregister():
    global _icons
    bpy.types.VIEW3D_HT_header.remove(_view3d_header)
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None
    if hasattr(bpy.types, "IMAGE_HT_header"):
        bpy.types.IMAGE_HT_header.remove(_header_draw)
    for name in _MENUS:
        m = getattr(bpy.types, name, None)
        if m is not None:
            m.remove(_menu_draw)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
