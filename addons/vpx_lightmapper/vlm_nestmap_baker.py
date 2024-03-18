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
from . import vlm_nest
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency

logger = vlm_utils.logger


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
    to_nest_ldr = []
    to_nest_hdr = []
    to_nest_ldr_nm = []
    to_nest_hdr_nm = []
    for obj in to_nest:
        uvmap = next((uv for uv in obj.data.uv_layers if uv.name == 'UVMap'), None)
        if uvmap is None:
            op.report({'ERROR'}, f'Object {obj.name} is missing the required unwrapped UV map named \'UVMap\'.')
            return {'CANCELLED'}
        obj.data.uv_layers.active = uvmap
        if not obj.data.uv_layers.get('UVMap Nested'):
            obj.data.uv_layers.new(name='UVMap Nested')
        obj.data.uv_layers.active = uvmap
        has_normalmap = next((mat for mat in obj.data.materials if mat.get('VLM.HasNormalMap') == True and mat['VLM.IsLightmap'] == False), None)  is not None
        # VPX only supports opaque HDR therefore we pack all non lightmaps as LDR (luckily base bake is usually LDR, and we don't really need this for lightmaps which are RGB only)
        if not obj.vlmSettings.is_lightmap or obj.vlmSettings.bake_hdr_range <= 1.0:
            if obj.vlmSettings.bake_hdr_range > 1.0:
                logger.error('ERROR: Object {obj.name} is packed to an LDR nestmap while it has an HDR range of {obj.vlmSettings.bake_hdr_range}. Render will be wrongly clamped. You need to reduce bake lighting strength to avoid this.')
            if has_normalmap:
                to_nest_ldr_nm.append(obj)
            else:
                to_nest_ldr.append(obj)
        else:
            if has_normalmap:
                to_nest_hdr_nm.append(obj)
            else:
                to_nest_hdr.append(obj)

    # Perform the actual island nesting and nestmap generation
    n_nestmaps = 0
    max_tex_size = min(8192, int(context.scene.vlmSettings.tex_size))
    if len(to_nest_ldr) > 0:
        logger.info('\nNesting all LDR parts')
        n_ldr_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_ldr, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', n_nestmaps)
        n_nestmaps += n_ldr_nestmaps
    if len(to_nest_hdr) > 0:
        logger.info('\nNesting all HDR parts')
        n_hdr_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_hdr, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', n_nestmaps)
        n_nestmaps += n_hdr_nestmaps
    if len(to_nest_ldr_nm) > 0:
        logger.info('\nNesting all LDR parts with normal maps')
        n_ldr_nm_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_ldr_nm, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', n_nestmaps)
        n_nestmaps += n_ldr_nm_nestmaps
    if len(to_nest_hdr_nm) > 0:
        logger.info('\nNesting all HDR parts with normal maps')
        n_hdr_nm_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_hdr_nm, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', n_nestmaps)
        n_nestmaps += n_hdr_nm_nestmaps

    # Restore initial state
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    logger.info(f'\nNestmap generation finished ({n_nestmaps} nestmaps generated for {len(to_nest)} objects) in {str(datetime.timedelta(seconds=time.time() - start_time))}.')
    return {'FINISHED'}
