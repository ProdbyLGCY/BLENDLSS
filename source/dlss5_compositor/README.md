# BlenDLSS5 – DLSS 5 Compositor Node

## Install

Drag `BlenDLSS5.zip` into the Blender window and click Install. That's it —
the DLSS 5 runtime (`nvngx_dlssnr.dll`) is included and installs with it.

Needs Windows, an NVIDIA GeForce RTX 50-series GPU and a current driver.
Blender 4.2 or newer (tested on 5.0 and 5.1).

## Use

1. **Render Properties → DLSS 5 → Add DLSS 5 to Compositor.** (Or Compositor →
   Add → Filter → DLSS 5.) The node goes between Render Layers and the output,
   and the viewport's Compositor option is switched on for you.
2. The controls are on the node **and** in Render Properties → DLSS 5:
   Intensity, Local Structure, Local Tone, Style, Model Passes, plus the
   **DLSS 5 On/Off** button for A/B comparisons. The sliders cover the
   normal range; type a number to go past it (passes up to 16).
3. **Viewport button:** the **DLSS 5 ON / OFF** button at the right end of the
   3D viewport header (next to the shading modes) turns DLSS 5 on and off; its
   icon turns green when it's on. If needed it adds the DLSS 5 node and sets
   the viewport's Compositor to *Always*.
   The **DLSS 5** sidebar tab (N) in the 3D viewport and the compositor, and
   Render Properties, hold the controls: sliders, Style, Quality, Model Passes,
   This View (the viewport's Compositor option) and Performance Readout. The
   collapsible **Advanced** section holds Depth & Motion (on by default: the Z
   and Vector passes are plugged in for renders and the viewport uses its depth
   and camera motion), Compatibility Mode (off by default), Auto Mask, Skin
   Detail and the DLSS 5 DLL picker.
4. **Live viewport:** Viewport Shading → Compositor → *Always* (or *Camera*).
   Works in Solid, Material Preview, EEVEE and Cycles. DLSS 5 runs on the
   image the viewport has just drawn (no second render), and the grid,
   outlines, gizmos and camera frame are laid back on top untouched.
   Quality: Native / Balanced (75%, default) / Performance (50%).
   **Compatibility Mode** (Render Properties → DLSS 5 → Live Viewport) runs
   DLSS 5 on the viewport exactly as displayed, overlays included – the
   simplest path, use it if the default mode ever misbehaves.
   The live view updates while the mouse is anywhere over Blender; with the
   pointer on another app/monitor it keeps the last DLSS 5 frame while
   nothing changes and shows the plain viewport otherwise.
5. **F12:** DLSS 5 goes into the real composite (Blender 5.0+): the Render
   Result, the Viewer node and the image you save all contain it, and any
   nodes after the DLSS 5 node are applied on top of it. It appears a moment
   after the render finishes. Changing a setting updates the render live; the
   On/Off button switches the composite between DLSS 5 and the original.
   (Blender 4.x shows it as a separate "DLSS 5 Render" image instead.)
6. **Animation (Ctrl+F12):** every saved frame gets DLSS 5 applied in place.
   Render to an image sequence (PNG, JPEG, EXR…) – video files can't be
   post-processed.

DLSS 5 only runs while the node is connected to the output and not muted.

## Using a different DLSS 5 DLL

The node and Preferences → Add-ons → BlenDLSS5 both have a **DLSS 5 DLL**
field. Pick any `nvngx_dlssnr.dll` there; leave it empty to use the bundled
one. The refresh button reloads it.

## How it works

`bin/blendlss5_nvngx.dll` creates its own D3D12 device, loads
`nvngx_dlssnr.dll` and calls it directly (NGX feature 18), the same way
NeuralScreen and the Full-Screen DLSS 5 Wrapper do. No NGX SDK or Streamline
install is needed. Source is in `native/`. A log is written to
`%LOCALAPPDATA%\BlenDLSS5\blendlss5.log`.

Without a working DLSS 5 runtime the node falls back to a CPU approximation
for renders (clearly labelled "CPU preview (not DLSS)"); the live viewport
only runs with real DLSS 5.
