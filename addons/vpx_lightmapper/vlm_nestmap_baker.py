#    Copyright (C) 2022  Vincent Bousquet
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>

import bpy
import time
import datetime
import hashlib
import os
import glob
from . import vlm_nest
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency

logger = vlm_utils.logger


def _nesting_checkpoint_signature(context, objects):
    """Return a stable signature for the current bake/nesting input.
    It deliberately includes mesh topology and UV coordinates so a checkpoint
    from a different mesh state cannot be reused.
    """
    h = hashlib.sha256()
    for obj in sorted(objects, key=lambda o: o.name):
        h.update(obj.name.encode("utf-8", "replace"))
        mesh = obj.data
        h.update(mesh.name.encode("utf-8", "replace"))
        h.update(str((len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops))).encode())
        for v in mesh.vertices:
            h.update(repr((round(v.co.x, 7), round(v.co.y, 7), round(v.co.z, 7))).encode())
        uv = mesh.uv_layers.get('UVMap')
        if uv:
            h.update(str(len(uv.data)).encode())
            for d in uv.data:
                h.update(repr((round(d.uv.x, 7), round(d.uv.y, 7))).encode())
        h.update(str((obj.vlmSettings.is_lightmap, round(obj.vlmSettings.bake_hdr_range, 6))).encode())
    return h.hexdigest()


def clear_nesting_checkpoint(context, reset_assignments=True):
    scene = context.scene
    scene['_vlm_nesting_checkpoint_active'] = False
    scene['_vlm_nesting_checkpoint_next_index'] = 0
    scene.pop('_vlm_nesting_checkpoint_time', None)
    scene.pop('_vlm_nesting_checkpoint_signature', None)
    scene.pop('_vlm_nesting_checkpoint_completed', None)
    if reset_assignments:
        result_col = vlm_collections.get_collection(scene.collection, 'VLM.Result', create=False)
        if result_col:
            for obj in result_col.all_objects:
                try:
                    obj.vlmSettings.bake_nestmap = -1
                except Exception:
                    pass
    return True


def render_nestmaps(op, context):
    result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
    if not result_col or len(result_col.all_objects) == 0:
        op.report({'ERROR'}, 'No bake result to process')
        return {'CANCELLED'}

    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    selected_objects = list(context.selected_objects)
    lc = vlm_collections.find_layer_collection(context.view_layer.layer_collection, result_col)
    if lc: lc.exclude = False

    # Prepare UV of target objects with 2 layers: 1 corresponding to the bake, 1 for the nested UV
    to_nest = [o for o in result_col.all_objects]
    # Keep VLM.Visuals / bake meshes strictly separate from lightmaps.
    # VPX handles transparency correctly for LDR/WebP visual maps, while HDR
    # is reserved for actual lightmaps.  Do not allow a visual and an HDR
    # lightmap to share a nestmap.
    to_nest_bm_ldr = []
    to_nest_lm_ldr = []
    to_nest_bm_ldr_nm = []
    to_nest_lm_ldr_nm = []
    to_nest_lm_hdr = []
    to_nest_lm_hdr_nm = []
    bakemap_hdr_range = 0.0
    # HDR Auto/Custom mode.
    # Auto = original GitHub per-bake HDR values.
    # Custom = override only for nesting/render scaling; mesh/UV creation is untouched.
    hdr_auto_mode = bool(getattr(context.scene.vlmSettings, "hdr_auto", True))
    hdr_custom_value = max(0.1, min(100.0, float(getattr(context.scene.vlmSettings, "hdr_custom_range", 1.0))))
    if hdr_auto_mode:
        logger.info("> HDR mode: Auto (original GitHub HDR range calculation)")
    else:
        logger.info(f"> HDR mode: Custom (HDR range={hdr_custom_value:.3f})")

    for obj in to_nest:
        uvmap = next((uv for uv in obj.data.uv_layers if uv.name == 'UVMap'), None)
        if uvmap is None:
            op.report({'ERROR'}, f"Object {obj.name} is missing the required unwrapped UV map named 'UVMap'.")
            return {'CANCELLED'}
        obj.data.uv_layers.active = uvmap
        if not obj.data.uv_layers.get('UVMap Nested'):
            obj.data.uv_layers.new(name='UVMap Nested')
        obj.data.uv_layers.active = uvmap

        has_normalmap = next(
            (mat for mat in obj.data.materials
             if mat.get('VLM.HasNormalMap') == True
             and mat.get('VLM.IsLightmap') == False),
            None
        ) is not None

        if not hdr_auto_mode:
            # Deliberately do this after mesh creation: custom HDR must never affect
            # mesh pruning, mesh size, or UV generation.
            obj.vlmSettings.bake_hdr_range = hdr_custom_value
        if not obj.vlmSettings.is_lightmap:
            # All VLM.Visuals / BM bake meshes are deliberately LDR.
            # This guarantees they are exported into WebP nestmaps and keeps
            # their alpha/transparency path compatible with VPX.
            if obj.vlmSettings.bake_hdr_range > bakemap_hdr_range:
                bakemap_hdr_range = obj.vlmSettings.bake_hdr_range
            if has_normalmap:
                to_nest_bm_ldr_nm.append(obj)
            else:
                to_nest_bm_ldr.append(obj)
        elif obj.vlmSettings.bake_hdr_range > 1.0:
            # Only actual lightmaps are allowed into HDR/EXR nestmaps.
            if has_normalmap:
                to_nest_lm_hdr_nm.append(obj)
            else:
                to_nest_lm_hdr.append(obj)
        else:
            # LDR lightmaps can remain LDR, but are kept separate from the
            # visual/bake meshes so a later change cannot mix BM with HDR.
            if has_normalmap:
                to_nest_lm_ldr_nm.append(obj)
            else:
                to_nest_lm_ldr.append(obj)

    # Keep the existing HDR 1.0 bake-map behaviour from the working version.
    # This affects only non-lightmap bake renders; HDR lightmaps are untouched.
    bakemap_hdr_range = min(bakemap_hdr_range, 1.0)

    # Perform the actual island nesting and nestmap generation.
    # Resume support: the nesting operator writes these scene properties only
    # after a complete nestmap has been rendered and the corresponding mesh
    # changes have been committed.  They survive a Blender restart because the
    # main .blend is saved at each checkpoint.
    checkpoint_active = bool(context.scene.get('_vlm_nesting_checkpoint_active', False))
    checkpoint_next_index = int(context.scene.get('_vlm_nesting_checkpoint_next_index', 0)) if checkpoint_active else 0
    checkpoint_signature = context.scene.get('_vlm_nesting_checkpoint_signature', '') if checkpoint_active else ''
    current_signature = _nesting_checkpoint_signature(context, to_nest)
    completed_raw = context.scene.get('_vlm_nesting_checkpoint_completed', '[]') if checkpoint_active else '[]'
    try:
        completed_nestmaps = sorted(set(int(x) for x in __import__('json').loads(completed_raw)))
    except Exception:
        completed_nestmaps = []

    # A checkpoint is valid only for the exact same nesting input and only if
    # at least one nestmap actually exists. This prevents a fresh mesh build
    # from accidentally resuming an old session.
    existing_nestmap_files = glob.glob(os.path.join(bakepath, 'Nestmap *.exr'))
    if checkpoint_active and (checkpoint_signature != current_signature or not completed_nestmaps or not existing_nestmap_files):
        logger.warning('\nStale/invalid nesting checkpoint detected (mesh/input changed, checkpoint incomplete, or no nestmap files found). Starting a fresh nesting run.')
        clear_nesting_checkpoint(context, reset_assignments=True)
        checkpoint_active = False
        checkpoint_next_index = 0
        completed_nestmaps = []
    elif checkpoint_active:
        logger.info(f'\nNesting checkpoint detected. Resuming from global nestmap index {checkpoint_next_index}. Completed nestmaps: {completed_nestmaps}')
    n_nestmaps = checkpoint_next_index
    if not checkpoint_active:
        context.scene['_vlm_nesting_checkpoint_signature'] = current_signature
        context.scene['_vlm_nesting_checkpoint_completed'] = '[]'
    max_tex_size = min(8192, int(context.scene.vlmSettings.tex_size))

    def nest_group(objects, label):
        nonlocal n_nestmaps
        if not objects:
            return
        logger.info(f'\nNesting {label}')
        if label.startswith('LDR visual'):
            logger.info(f'> Bakemap HDR range: {bakemap_hdr_range} (render lighting will be rescaled accordingly)')
            for obj in objects:
                obj.vlmSettings.bake_hdr_range = bakemap_hdr_range
        # During resume, already completed objects are left in the scene for
        # export but are removed from the packing input.  Unassigned objects
        # (including split duplicates) continue through the normal algorithm.
        if checkpoint_active:
            objects = [obj for obj in objects if obj.vlmSettings.bake_nestmap < 0]
            if not objects:
                logger.info(f'. {label}: all objects already completed; skipping.')
                return
        resume_index = 0
        if checkpoint_active:
            resume_index = max(0, n_nestmaps)
        count, _ = vlm_nest.nest(
            context, objects, 'UVMap', 'UVMap Nested',
            max_tex_size, max_tex_size, 'Nestmap', n_nestmaps, resume_index=0
        )
        if count is None:
            return
        n_nestmaps += count

    # IMPORTANT: each group gets its own nestmap range.  In particular, no
    # BM/Visual object can share an EXR nestmap with an HDR lightmap.
    nest_group(to_nest_bm_ldr, 'LDR visual bake meshes')
    nest_group(to_nest_lm_ldr, 'LDR lightmaps')
    nest_group(to_nest_lm_hdr, 'HDR lightmaps')
    nest_group(to_nest_bm_ldr_nm, 'LDR visual bake meshes with normal maps')
    nest_group(to_nest_lm_ldr_nm, 'LDR lightmaps with normal maps')
    nest_group(to_nest_lm_hdr_nm, 'HDR lightmaps with normal maps')

    # All groups completed successfully. Clear the durable resume marker so a
    # later manual nestmap run starts from scratch instead of unexpectedly
    # reusing an old checkpoint.
    if checkpoint_active:
        clear_nesting_checkpoint(context, reset_assignments=False)
        try:
            if bpy.data.filepath:
                bpy.ops.wm.save_mainfile()
        except Exception as checkpoint_clear_error:
            logger.warning(f'Could not clear nesting checkpoint: {checkpoint_clear_error}')

    # Restore initial state
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    logger.info(f'\nNestmap generation finished ({n_nestmaps} nestmaps generated for {len(to_nest)} objects) in {str(datetime.timedelta(seconds=time.time() - start_time))}.')
    # Diagnostic: any object still at -1 here is genuinely unexpected (empty-UV objects are
    # now assigned nestmap_offset by nest(), so -1 means something went wrong during preparation)
    unassigned = [obj.name for obj in to_nest if obj.vlmSettings.bake_nestmap < 0]
    if unassigned:
        logger.info(f'>> ERROR: {len(unassigned)} object(s) unexpectedly have no nestmap assigned after nesting completed. This will block the export button. Objects: {unassigned}')
    return {'FINISHED'}
