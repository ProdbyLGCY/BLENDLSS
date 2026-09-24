# BlenDLSS

AI WARNING

(THIS PROJECT IS VIBE CODED TO HELL) 

I dont no nothin about nobody when it comes to coding, python, etc. I like 3d modeling and gaming, when dlss5 leaked, I wanted to try it in blender and couldnt find a reliable way to use it other then nuralscreen and running it over the whole screen. 

Claude (Opus 5.5 if anyone cares) was responsible for the lifting of making this work. 

I take ZERO credit other then writing the prompts and debugging (aka typing what wasnt working back into claude and repeat) the addon until I felt it was good enough to upload for others to use without needing a claude subscription themselves. I expect ZERO praise or love for knowing how to prompt. Although I hope to set an example to others on how to be transparent about AI usage. 

Anyways, I hope that this addon is able to help. 

Thats all. 

Cheers! 




FYI The Read me is 99% AI also, I just read through it and changed what didnt make sense to keep. 

**NVIDIA DLSS 5 in Blender – live in the viewport and in your final renders.**

BlenDLSS5 is a Blender add-on that adds a **DLSS 5 compositor node**. Put it between your Render Layers and the output, and DLSS 5 runs on your image:

- in the viewport as you work, in Solid, Material Preview, EEVEE and Cycles;
- on F12 renders, straight into the final composite;
- on every frame of an animation render.

There's a one-click **DLSS 5 ON / OFF** button in the viewport header, so comparing before and after takes a single click.
---

## What you need

- Windows 10 or 11
- An NVIDIA GeForce RTX GPU that supports DLSS 5 (built and tested on an RTX 5070 Ti) and a current driver
- Blender 4.2 or newer (tested on 5.0 and 5.1). Putting DLSS 5 into the final composite needs Blender 5.0 or newer.
- The DLSS 5 runtime, `nvngx_dlssnr.dll`. One zip below already includes it.

## Downloads

| `BlenDLSS5.zip` | The add-on **with** `nvngx_dlssnr.dll` bundled. Drag it into Blender and it just works.

| `BlenDLSS5_noDLL.zip` | The add-on **without** the DLL. Point it at your own .dll. I added this version in the case that in the future there's more DLL leaks and there's no point in grabbing the old version.

| `nvngx_dlssnr.dll` | The raw DLSS 5 runtime on its own, if you'd rather use the small zip. (Current as of 9/24/26)

## Install

1. Open Blender and drag **`BlenDLSS5.zip`** into the window, or use *Edit → Preferences → Get Extensions → Install from Disk*.
2. That's it. With the full zip, DLSS 5 is ready immediately.

Using the no-DLL zip? After installing, go to *Edit → Preferences → Add-ons → BlenDLSS5* (or the **Advanced** section of the DLSS 5 panel) and pick your `nvngx_dlssnr.dll` in the **DLSS 5 DLL** field.

## Quick start

1. In the 3D viewport header, click **DLSS 5 OFF** (top right, next to the shading buttons).
2. It turns green (**DLSS 5 ON**) and sets everything up for you:
   - adds the DLSS 5 node to the compositor;
   - plugs in Depth and Motion;
   - switches the viewport compositor on.
3. Tweak the look in the **DLSS 5** tab of the sidebar (press **N**).

---

## The controls

You'll find the same panel in the sidebar (**N**) of both the 3D viewport and the compositor, and in *Render Properties → DLSS 5*.

- **DLSS 5 ON / OFF**: the big switch. It works for the viewport and renders alike, which makes A/B comparisons easy.
- **Intensity**: how strong the overall effect is.
- **Local Structure**: fine detail such as contact shadows, micro-occlusion and texture.
- **Local Tone**: large-scale lighting and colour response.
- **Style**: *Standard*, *Natural* (softer, closer to the original) or *Cinematic* (more contrast and grading).
- **Quality**: the resolution DLSS 5 runs at in the viewport.
  - *Native* (100%)
  - *Balanced* (75%)
  - *Performance* (50%)
- **Model Passes**: runs DLSS 5 over its own output several times, for a stronger effect.
- **This View**: the viewport's compositor mode.
  - *Always* shows DLSS 5 in every view.
  - *Camera* only shows it when you look through the camera.
- **Performance Readout**: shows frame rate and timings in the corner of the viewport. Off by default.

The sliders cover the normal range, but you can type any number to push past it. Model Passes goes up to 16. This is a testing tool, so go wild.

**Advanced** (collapsible):

- **Depth & Motion** (on by default)
  - DLSS 5 gets the scene's depth and motion, which gives steadier results, especially while you orbit.
  - For renders, the Z and Vector passes are switched on and plugged in automatically.
  - In the viewport, it uses the viewport's depth plus the camera's movement.
- **Compatibility Mode** (off by default): runs DLSS 5 on the viewport exactly as displayed, grid and gizmos included. It's the simplest path; use it if the normal mode ever misbehaves.
- **Auto Mask / Skin Detail**: extra DLSS 5 model options.
- **DLSS 5 DLL**: use a different `.dll`. Leave it empty to use the bundled one.


---

## How it works (for the curious)

### Viewport

DLSS 5 runs on the image Blender's viewport has just drawn, so there's no second render and it works the same in every shading mode, including Cycles.

The grid, outlines, gizmos and camera frame are peeled off first and laid back on top afterwards, so your UI stays crisp and untouched. With Depth & Motion on, DLSS 5 also gets the viewport depth and camera motion vectors, so it can keep its history while you move around.

### Renders

The compositor can't run code per pixel, so the add-on handles renders in four steps:

1. It captures the DLSS 5 node's input while the render is compositing.
2. It runs DLSS 5 on that captured image, converted with your exact colour management (view transform, look, exposure).
3. It feeds the result back into the node.
4. The **Render Result, the Viewer node and the image you save all contain DLSS 5**, and any nodes after the DLSS 5 node still apply on top.

Changing a setting after a render updates it live.

### Animations

Each saved frame gets DLSS 5 applied in place, with temporal history carried from frame to frame. Render to an image sequence (PNG, EXR, …); video files can't be post-processed.

### Under the hood

- A small bridge, `blendlss5_nvngx.dll`, creates its own Direct3D 12 device and talks to `nvngx_dlssnr.dll` directly, using the same "pure" approach as the Full-Screen DLSS 5 Wrapper and NeuralScreen.
- No NGX SDK, Streamline or other installs are needed.
- The bridge source is in `native/` inside the add-on.

## Tips & known limitations

- **Performance:** the live viewport costs roughly 20–30 ms per update on an RTX 5070 Ti at 1080p–1440p. Use *Quality → Balanced* or *Performance* for smoother orbiting.
- **Pointer outside Blender:** Blender only lets add-ons redraw the window while the mouse is over it. If the pointer is on another app or monitor, the viewport keeps the last DLSS 5 frame until you come back.
- **Motion vectors:** the viewport's motion vectors cover camera movement only; moving objects rely on DLSS 5 alone. Final renders use Blender's full Vector pass.
- **Cycles Vector pass:** Cycles leaves the Vector pass empty while Motion Blur is on.
- **Blender 4.x:** the add-on shows the render result as a separate "DLSS 5 Render" image instead of putting it into the composite.
- **Log file:** a log is written to `%LOCALAPPDATA%\BlenDLSS5\blendlss5.log` if something goes wrong.

## Credits

Made by **LGCY Creative Studios**.

Thanks to these projects, which showed how to drive DLSS 5 directly:

- [ThioJoe / Full-Screen-DLSS5-Wrapper](https://github.com/ThioJoe/Full-Screen-DLSS5-Wrapper)
- [perseval-BLR / NeuralScreen](https://github.com/perseval-BLR/NeuralScreen)
- [rakanki911 / DLSS5-Swapper](https://github.com/rakanki911/DLSS5-Swapper)


This is an independent project and is not affiliated with or endorsed by NVIDIA. NVIDIA, GeForce, RTX and DLSS are trademarks of NVIDIA Corporation. `nvngx_dlssnr.dll` is NVIDIA's software and is covered by NVIDIA's own license terms.
