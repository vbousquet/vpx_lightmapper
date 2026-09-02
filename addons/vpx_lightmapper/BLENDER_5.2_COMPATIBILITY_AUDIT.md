# VPX Lightmapper 3.0.16 — Blender 4.5 → 5.2 compatibility audit

## Baseline and runtime validation
- Minimum supported Blender version: 4.5 LTS.
- Target: Blender 5.2.1 LTS / Python 3.13.
- A real Blender 5.2.1 test run completed batch rendering (9/9 renders), bake mesh generation, visibility masks, LDR lightmap processing, HDR lightmap processing, LDR/HDR nesting, and 3 nestmap renders/saves.

## Audited API areas
- GPU: removed direct `GPUShader(vertex, fragment)` construction migrated to `GPUShaderCreateInfo`; background GPU initialization is supported through `gpu.init()` in Blender 5.2.
- EEVEE: engine identifier uses `BLENDER_EEVEE` on Blender 5.x and `BLENDER_EEVEE_NEXT` on 4.5.
- Compositor: Blender 5.x uses `Scene.compositing_node_group` and `RenderSettings.use_compositing`; File Output uses `file_output_items`, `directory`, `file_name`, and `ImageFormatSettings`; Denoise uses the Prefilter input.
- File Output: `media_type` is written through `node.format.media_type`; the output item is kept stable so existing links are not destroyed during path reconfiguration.
- Render passes: uses modern pass names such as `Denoising Normal`, `Denoising Albedo`, `IndexOB`; no old `DiffCol` or `IndexMA` identifiers are used. Diffuse-color pass enabling is feature-checked before assignment.
- UV edge selection: Blender 5.x uses BMesh UV loop selection (`select_edge`) instead of the removed `UVLayer.edge_selection` access.
- OBJ export: all supported Blender versions use `bpy.ops.wm.obj_export`; no 5.2-incompatible `bpy.ops.export_scene.obj` path remains.
- Materials: legacy `blend_method` calls are mapped to Blender 5.x `surface_render_method`.
- LayerCollection: `indirect_only` remains configured on the scene's actual ViewLayer hierarchy.
- EEVEE 5.2 rendering changes were reviewed; the main lightmapper render path is Cycles, while EEVEE is used for mask rendering.
- 5.1 Python 3.13 and 5.2 Geometry Nodes API changes were reviewed; the addon does not access Geometry Nodes modifier inputs through the changed API.
- 5.2 compositor File Extension behavior is explicitly controlled per File Output node.

## Intentionally unchanged
- Legacy 4.5 branches for pre-5.0 compositor/GPU APIs remain guarded by `bpy.app.version < (5, 0, 0)`.
- Legacy pre-4.1 split-normal/auto-smooth code remains guarded and is not executed on 4.5/5.2.
- VPX BIFF parsing/writing is independent of Blender's 5.x API changes.

## Runtime caveat
A full export test with a real VPX project should still be performed after installation, because the supplied 5.2 log ended after nestmap generation rather than the final VPX export stage.

## Final static scan
- 16 Python modules / 10,365 Python source lines audited.
- Python AST parsing and `compileall` with `SyntaxWarning` treated as errors: clean.
- No active `bpy.ops.export_scene.obj`, BGL, Image.bindcode, shader.program, GTAO, or old render-pass identifier usage remains.
- Legacy 4.5-only API references remain only inside explicit pre-5.0 branches or pre-4.1 branches.
- Exact temporary collection references are used during render-scene cleanup to avoid deleting a user collection with the same name.
- Temporary Blender 5.x compositor node groups are cleaned when they become unused.
