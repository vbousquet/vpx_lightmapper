# Handling transparent parts

This guide explains how to handle transparent parts, which happens to be the most tricky part. Therefore, you should have followed the previous guides before reading/attempting this one.

* [VPX Rendering](#vpx-rendering)
* [Alpha blending and depth masking](#alpha-blending-and-depth-masking)
* [VPX Alpha mask](#vpx-alpha-mask)
* [Lightmapped table workflow](#lightmapped-table-workflow)
* [VPX render workflow deeper description](#vpx-render-workflow-deeper-description)

**TODO** Add the tutorial files


## VPX Rendering

VPX performs its rendering following these main steps:
- Before starting the game:
  - [1] Render all static opaque parts to an offscreen image,
  - [2] Render all static opaque parts mirrored by the playfield to another offscreen image.
- Then, during the game, for each frame:
  - [3] Prepare an offscreen image for the playfield reflection by copying the prerendered mirrored static opaque parts (from step [2]) to an offscreen image, and rendering the dynamic parts mirrored on it,
  - [4] Create the visible frame by first copying the prerendered static opaque parts (from step [1]),
  - [5] Then apply the playfield reflection by copying the image created at step [3],
  - [6] Then render the dynamic opaque parts (balls, primitives,...),
  - [7] And finally render the transparent parts (active materials), using alpha blending.

The rendering is performed using depth masking, meaning that a point which is behind an already rendered point will be entirely discarded (even if the previous rendered pixel was partially transparent).

The transparent parts are rendered using [alpha blending](https://en.wikipedia.org/wiki/Alpha_compositing), which means that the color of a point is a blend between the color of the rendered primitive and the color of the already rendered ones.

For steps 1 to 6, the rendering is performed from front to back. For step 7, the rendering is performed from back to front. Sorting of the objects is based on their position, modulated by their depth bias.


## Alpha blending and depth masking

The main consequence of this workflow for transparent parts is that they must be rendered from back to front. If not, 2 artefacts will appear:
- jagged lines around the borders due to depth masking,
- wrong blending between multiple transparent objects or self overlapping ones.

These problems appear because the computed color depends on the computed depth (for masking) and color (for blending) of the parts previously rendered.

![Order of drawing in alpha blending](TR-01%20Alpha%20Blend.svg)

![Despth masking with Alpha Blending](TR-02%20Depth%20Mask.svg)

Additionally, a common problem that you may have encountered with alpha blending is that, when rendered in 3D, the final pixel is built by sampling multiple texel from the texture and averaging them. This leads to average the color of pixel with an alpha value of 0 (fully transparent) with the color of pixels wich are actually meant to be visible (alpha > 0). You can find lots of deeper explanation of this problem on the internet like [this one](https://www.adriancourreges.com/blog/2017/05/09/beware-of-transparent-pixels/). To put it short, in the end, alpha blended textures need to be preprocessed with [alpha bleeding](https://github.com/urraka/alpha-bleeding) tools to avoid artefact on their borders.

## VPX Alpha mask

For jagged lines along the borders, VPX offers to minimize the problem by definig an alpha mask for each image. VPX will clip all points that have an alpha value under this user defined threshold.

This is a fast way of getting an acceptable result without having to solve the core problem of render order. The tradeoff is that the quality will always be lower than in a table were transparent parts are rendered in the correct order (from back to front). Though, with the correct values, high resolution screens and antialiasing make the difference barely noticeable.

**TODO** Add an image describing alpha clipping versus clean alpha blending


## Lightmapped table workflow

The previous elements results in that, to get correct transparent part rendering, we need to split the bakemaps (and lightmaps) in layers, corresponding to the overlaps of tranparent objects, and render them from back to front.

For a simple plastic ramp, this would lead to 2 layers: one for the back of the ramp, and one for the front. Rendering of this setup would be: opaque bakground and ball, then layer 1, then layer 2.

![Layers for a transparent ramp](TR-03%20Layers.svg)

The toolkit allows to easily define layers by creating additional bake collections (each bake collection is baked to a separate mesh), and allowing to define for each bake collection its own depth bias.

![Bake layer config](TR-04%20Bake%20Layers.png)

Blender also needs to be told where each layers must be separated. By default, in the above setup, when rendering the part of the ramp nearest to the player, it will compute the lights coming from behind, and since there are opaque objects behind, it will not consider it as transparent but instead shade it with the color of the refracted light coming from behind.

To tell Blender where we want it to split the layers, we must put a separator mesh with an 'Holdout' material.

![Holdout materials](TR-05%20Blender%20Holdout.svg)

The last step we need to do is to link each of these layer separator to the objects they are made for. The 'Bake mask' feature of the toolkit is meant for this. It is available in the 3D view panel, after selecting the transparent object you have made a layer separator for.

**TODO** add a screenshot of the corresponding config


## VPX render workflow deeper description

For readability, the workflow described at the beginning of this page is not fully complete. It does not cover the specific depth buffer passes, nor the light transmission and ball reflections. Here is a more complete view of the workflow:
- Before starting the game:
  - [1] Render all static opaque parts to an offscreen image,
  - [2] Render all static opaque parts mirrored by the playfield to another offscreen image.
- Then, during the game, for each frame:
  - [3a] Prepare an offscreen image for the playfield reflection by copying the prerendered mirrored static opaque parts(from step [2]) to an offscreen image, and rendering the dynamic parts mirrored,
  - [3b] Prepare an offscreenn image with the contribution of each lights enabled for transmission
  - [4a] Create the visible frame by first copying the prerendered static opaque playfield **without** depth (from step [1]),
  - [4b] Then copy other prerendered static opaque playfield **with** depth  parts (from step [1])
  - [5a] Then apply the playfield reflection by copying the image created at step [3] **without** depth,
  - [5b] Then copy playfield depth,
  - [6] Then render the dynamic opaque parts (balls, primitives,...),
  - [7] And finally render the transparent parts (active materials), using alpha blending.

When rendering transparent parts, the offscreen image of transmitting light created at step [3b] is used in the shading of the part.

When rendering balls, the 8 nearest lights that are enabled for ball reflections are selected and rendered as a reflection on the ball according to their position, color and falloff parameters. Only the distance is taken in account to select the light; the state of the light (on or off) is not taken in account.

