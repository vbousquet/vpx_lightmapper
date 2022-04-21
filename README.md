# Visual Pinball X Light Mapper

*A [Blender](www.blender.org) add-on to help building pretty baked tables for the [world's favorite pinball simulator](https://github.com/vpinball/vpinball).*

## Disclaimer

This tool is a hobby project, developped independently from the great team behind Visual Pinball X. Please do not bother them with issues regarding this tool.

This tool is just my attempt at building better tables. It is shared in the hope that it may help others to build nice tables. There is no dedicated ressources behind this project. It is in a pre-alpha state, with no support. There are bugs more or less everywhere, so it is to be used with extreme care and at your own risk!

## Table of contents

* [What is it ?](#what-is-it)
* [Features](#features)
* [Installation](#installation)
* [Overview](#overview)
* [Import Tool](#import-tool)
* [Camera Tool](#camera-tool)
* [Bakemap/Lightmap tool](#Bakemap-Lightmap-tool)
* [Quick Start](#quick-start)
* [Usefull Hints](#usefull-hints)

## What is it ?

[Visual Pinball X](https://github.com/vpinball/vpinball) is a game engine dedicated to pinball game simulation. It is a great tool which has allowed a whole community to grow and produce incredible pinball tables ([VPForums](https://www.vpforums.com), [VPUniverse](https://www.vpuniverse.com), [Orbitalpin](https://www.orbitalpin.com), [Pinball Nirvana](https://www.pinballnirvana.com), [VPDB](https://www.vpdb.io), [PinSimDB](https://www.pinsimdb.org),...). For the graphic part, it includes a nice realtime rendering engine which allows great visual results. 

To obtain the best visuals, table creators may choose to precompute parts of the rendering of their table in a more advanced offline renderer like [Blender](www.blender.org), a.k.a. "baking" the rendering. This works especially nicely for pinball games since the game is played from a fixed point of view, allowing to efficently bake view dependent lighting effects.

The visual quality of a pinball simulation is also tighlty linked to the rendering of the many (up to hundred) lights that are used in a pinball. The texture baking approach can be extended regarding precomputing the effect of lighting by using "light maps", that is to say models with precomputed textures storing the effect of lights. These models are a shell around the table objects, with texture storing the lighting. When playing the pinball table, they are renderered by adding the lights contribution to the base model.

This tool aims at helping with the process of creating VPX tables by:
- making importing/updating table a fast, simple and easy task,
- easing camera setup,
- automating the full bakemap/lightmap process, with a good level of optimization.

![Property Panel](docs/Property-panel.png)

The UI is available in the scene property panel, inside the "VPX Light Mapper" pane. Lots of informations and tools are also available in the information (N key) panel of the 3D view and the collection property panel (bake & light groups configuration).

## Features

This toolkit offers the following features:
- Import VPX table including lights & materials, easily update edited table
- Setup camera according to VPX camera, including 'layback' support
- Automatically detect occluded objects that can be exclude from bake meshes
- Automatically setup groups of non overlapping objects
- Batch render bakemap/lightmap, automatically optimizing renders
- Generate mesh for bake/lightmaps and optimize them (subividing for limited prejection distortion, pruning of unneeded faces for lightmaps, limited dissolve, backface removal,...)
- Render nested texture combining all the bakemap/lightmap
- Export directly playable VPX table file with updated geometry, textures and script

## Installation

### Blender Console

This add-on use the Blender console for all its output. You need to enable the console before everything else (Window > Toggle System Console).

### Blender add-on

1. Download add-on (zipped) from the [release section](https://github.com/vbousquet/vpx_lightmapper/releases).
2. In blender, go to : Edit -> Preferences -> Add-ons -> Install.
3. Select the downloaded file.
4. This add-on requires external python dependencies that are likely not available with your default Blender installation. If so, it will show an 'install dependencies' button that you need to click before use. After installing the dependencies, Blender will not immediatly detect them: you will have to restart Blender.

Depending on your security configuration, this additional dependency installation step may need to be performed from a Blender instance started with administrator rights. In this case, the installation process is somewhat more complex since you will need to install the add-on as administrator, then the dependencies, then close Blender and restart it from a normal account, and install the add-on again (the first time it was installed to your admin account, so it is not available from your normal acount, but the dependencies are installed system wide and will be available).

### Visual Pinball X with additive blended primitives

Additionnally, this tool needs the latest, not yet released, build of Visual Pinball X or Visual Pinball for VR. These are available [here for VPX](https://github.com/vpinball/vpinball/actions) and [here for VPVR](https://github.com/vpinball/vpvr/actions). Be aware that these builds are not release version but alpha build. Therefore you are likely to encounter bugs, and you should not use them without backing up your work first.

## Import tool

The import tool is used to create a scene setup (empty or from an existing table), then update it. The initial imports will create the default collections, loads everything and try to set up the right collections. Lights are imported as emitting geometry or point lights depending on the 'bulb' option in VPX. Lights which are likely inserts follow a specific process (generate a slightly reflective cup below playfield, generate a translucency map for the playfield)

When updating, the process is the following:
- All textures are reloaded
- Objects link to groups (Hidden/Bake/...) are left untouched except for deleted objects which are moved to the 'Hidden' collection
- World environment is (re)created if missing. The environment texture is updated in the shader node named 'VPX.Mat.Tex.IBL'
- Objects are identified by their link to VPX object (look in 3D View side panel, there you can see/edit the VPX identifier, eventually associated to a subpart for multipart objects like bumpers). If object is not found, it is created like on initial import but if object is found, it is updated according to its configuration (available in the 3D View side panel):
	- If not disabled, meshes/curves/light informations are replaced by the ones defined in the VPX table,
	- If not disabled, position/rotation/scale are redefined to match the one of the VPX table
	- If the material slot is empty, it is (re)created.
	- If the material slot use a material with matching material name, the core material node group and image texture of the material are updated with VPX material data

Therefore, if you modify an object, you have to disable the corresponding import properties or your changes will be lost on the next update.

To take control of a material, either change the material to a non 'VPX.' named one or simply modify inside the existing material (which will still get updates but only on the aforementioned nodes. Other objects with the same material/image pair will use the same Blender material and therefore will have these changes applied too).

## Camera tool

Since having a matching camera with the player view point is needed for correct perspective projected baking, a dedicated tool allows you to manage it automatically. It will update the camera according to your settings (which are imported from the VPX table).

The tool always fit the camera to the objects to be baked, processing layback according to the selected mode:
- Disable: don't take layback in account (this won't render well in VPX),
- Deform: deform geometry by applying a layback lattice (this will somewhat break the shading but is the most faithfull to VPX rendering),
- Camera: this will adjust camera inclination and rendering properties to correspond to VPX rendering without breaking the shading. This mode is the one recommended for good quality baking.

## Bakemap/Lightmap tool

The workflow can be whatever you want, but the tool was designed around the following one:
1. Create a table in VPX
2. Import the table in Blender (or update if this is done after the first iteration) 
3. Adjust the generated baking configuration (light groups, bake groups, bake modes,...)
4. Improve the materials and adjust the lights in Blender
5. Bake
6. Review the result direclty inside Blender
7. Export an updated table with all the bakes
8. Adapt the table script, using the generated helper
9. Test and adjust in VPX, eventually go back to step 2, using update button

The texture baking is performed according to:
- Light groups, which define each lighting situation to be computed,
- Bake groups, which define groups of object to be rendered, baked together and exported.

This tools performs baking using 'projected UV unwrapping'. This means that the baking is performed from the bake camera point of view, texture coordinates of the models are computed automatically by projecting them from the camera point of view. This method works well in the context of baking a pinball table:
- you don't need to unwrap your models,
- you can use default Blender's shading nodes,
- the produced texture have uniform texel density (which is good for performance and visual quality).
The drawback of this technique is that it is highly point of view dependent and won't work well on objects or ligths that can be moved. Alson this technique needs ad-hoc texture packing algorithm since normal uv-packing only works well if baking is performed after uv packing which is not the case here.

The lightmap baker automates all the baking process according to the following workflow for each bake group:
1. Identify non overlapping groups of objects from the camera point of view
2. For each light situation, render each non overlapping object groups from the camera point of view and store these renders in a cache
3. Generate an optimized mesh for each bake group, suitable for fixed view baking (including preliminary mesh optimization, and automatically subdivision to avoid projection artefacts)
4. For each light group, derive an optimized mesh by removing unlit faces
5. For all of these meshes, compute a texture map from the initial renders
7. Export all this to an updated VPX file

In the collection property panel, each bake groups has a 'bake mode' which determine how it will be processed:
- Group: outputs a single mesh per bake group
- Split: outputs a single mesh per object in the group, do not perform backface culling
Additionally, each group must be marked as 'opaque' or 'non opaque'. This information is needed for VPX to correctly render transparent objects (they need to be rendered after opaque one, from back to front), to allow render optimization (static opaque objects are prerendered, giving a large performance boost), and to correctly process object border shading (transparency of borders of opaque objects is discarded).

In the collection property panel, each light collection has a 'light mode' which determine how it will be processed:
- Solid: content of the collection is used for the base bake (not a lightmap).
- Group: all lights are treated as a group and generate a single lightmap,
- Split: each light of the collection generate a lightmap,
Note that all lights or emissive objects must be placed in one of these collections or they will influence each of the lightmap, creating ugly artefacts.

Exporting consist in generating a new VPX table file with the new packed bake/lightmap images, and the bake/lightmap primitives. The exporter has an option to decide what to do with the initial items of the table that have been baked:
- Default: don't touh them,
- Hide: hide items that have been baked,
- Remove: remove items that have been baked and do not have physics, hide the one that have been baked but are needed for physics,
- Remove all: same as remove, but also remove images that are not needed anymore.

## Quick start

This little guide gives you the step to create your first lightmap baked table:
1. Open VPX and create a new default table (Ctrl+N), then save it.
2. Open Blender, open the Blender console, install the add-on
3. Delete everything from the default Blender scene
4. In the scene panel, press the 'Import' button, and select the saved default VPX table
5. Press 0 for camera point of view
6. In the 3D view, in the right panel (N shotcut), select the VLM pane
7. In the 3D view, select the primitives on the left and right side of the table, then move them to the 'Hidden' collection
8. In the 'Active' collection, select all shadow objects ('FlipperRsh', 'BallShadow1',...) and move them to the 'Hidden' collection
9. Move the remaining primitive of the 'Active' collection to the 'Default' collection
10. Save your scene
11. In the scene panel, select a target resolution (start with very low for an ugly but fast preview), then press 'Batch'
12. Wait (this may be long depending on your computer and the resolution), watching in the console what is happening
13 You can preview/inspect the result directly in blender: select 'Rendered' mode for the 3D View, you now have a pink table! Select it, and press 'Load/Unload Renders' in the VLM panel to view it with the baked textures. Do the same for the different bake/light mesh you want to inspect.
14. Open the new VPX table and play! You will the see the baked objects. Though, lights won't be synchronnized until you update your table script.

## Usefull hints

### Optimizing the bake mesh (and render time)

As much as possible, you should avoid including occluded (not directly visible) geometry from your bakes. To help you in doing so, the add-on offers a small tool that will identify occluded geometry from the camera point of view. You can then choose to mark them as 'Indirect' (it will influence the image but won't be part of the bake mesh). This tool can be accessed from the 3D view, by clicking the button 'Select Occluded'. Note that this can be a lengthy operation. Another side benefit of tagging as indirect the occluded geometry is that this will help limiting the number of render groups, and therefore limit the render time.

Beside this user action, to avoid ending up with very large meshes (in terms of number of faces, as well as overdraw when rendering in VPX), all produced meshes are optimized by:
- pruning duplicate vertices,
- performing a limited dissolve of the mesh (see Blender doc),
- performing backface culling,
- removing unlit faces from lightmaps.

### Inserts

The importer has an option to detect inserts (lights placed on the playfield, with a name which does not start by 'gi') and apply the following process:
- move light slightly below playfield,
- generate a cup mesh, opened on the top side, corresponding to the VPX light shape, with a core reflective material,
- adjust the playfield material to be partly translucent for the inserts, using an automatically generated translucency map.

### Transparent objects (plastic ramps, bumper caps,...) and alpha blending

Visual Pinball X performs alpha blending of transparent objects. This means that the object is rendered on top of objects behind it, blending the color between them according to the alpha channel of the used texture. This can be compared to the shading of a transparent glass with an indice of refraction of 1 (i.e. non refractive glass). Alpha blending needs the opaque objects to be rendered first, then the alpha blended parts, rendering the ones in the back first, then the ones in the front.

Blender can render glass, outputing to the alpha channel the amount of escaping rays, that is to say rays that are transmitted through the glass and reaching the background. This is great since it gives full control on what is rendered to the user. To enable this, in 'Render Properties > Film > Transparent' check 'Transparent Glass' and increase 'Roughness Threshold'. In the case of a pinball table, transparent objects usually have other objects behind them, therefore rays are not escaping but reaching other elements. If we want to view a ball passing behind 2 elements, they have to be rendered as 2 layers: a background opaque one, and an alpha blended one. To let Blender knows about this separation between the 2 layers, we need to place a mesh between them with an holdout shader.

Note that, with this setup, there won't be any shadowing, since there is no shadowing support inside VPX. So a ball passing under a transparent object will not occlude light coming from behind it.

### Managing the cache

Each step of the bake/lightmap process is saved in a cache located along the blend file (that's the reason why you need to save your blend before starting the bake process). 
For the moment, this cache is manually managed. Inside it, you will find 3 folders:
- Object Masks: contains the mask computed for each object during the 'Group' step. You need to manually delete elements (or the whole folder) when objects are moved
- Renders: contains the actual renders. This needs to be manually cleared for render to actually happen (or if you know what you do, clear the only the elements you want to rerender)
- Export: contains the packmaps in Png and WebP format. As for the other folders, this needs to be manually cleared for packmaps to be regenerated