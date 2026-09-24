bl_info = {
    "name": "BlenDLSS5 - DLSS 5 Compositor Node",
    "author": "LGCY Creative Studios",
    "version": (2, 6, 1),
    "blender": (4, 2, 0),
    "location": "Compositor > Add > Filter > DLSS 5",
    "description": "NVIDIA DLSS 5 neural rendering as a compositor node, live in the viewport and on renders",
    "category": "Compositing",
}

from . import prefs, node, ui, render, viewport  # noqa: E402

_modules = (prefs, node, ui, render, viewport)


def register():
    for m in _modules:
        m.register()


def unregister():
    from . import engine
    engine.shutdown()
    for m in reversed(_modules):
        m.unregister()
