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


def render_nestmaps(op, context):
    camera = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

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
    for obj in to_nest:
        uvmap = next((uv for uv in obj.data.uv_layers if uv.name == 'UVMap'), None)
        if uvmap is None:
            op.report({'ERROR'}, f'Object {obj.name} is missing the required unwrapped UV map named \'UVMap\'.')
            return {'CANCELLED'}
        obj.data.uv_layers.active = uvmap
        obj.data.uv_layers.new(name='UVMap Nested')
        obj.data.uv_layers.active = uvmap
        if obj.vlmSettings.bake_type == 'active' or obj.vlmSettings.bake_hdr_range <= 1.0:
            to_nest_ldr.append(obj)
        else: # VPX only supports opaque HDR
            to_nest_hdr.append(obj)

    # Perform the actual island nesting and nestmap generation
    max_tex_size = min(8192, int(context.scene.vlmSettings.tex_size))

    if True:
        print('\nNesting all LDR parts')
        n_ldr_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_ldr, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', 0)
        print('\nNesting all HDR parts')
        n_hdr_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest_hdr, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', n_ldr_nestmaps)
        n_nestmaps = n_ldr_nestmaps + n_hdr_nestmaps
    else:
        n_nestmaps, splitted_objects = vlm_nest.nest(context, to_nest, 'UVMap Nested', max_tex_size, max_tex_size, 'Nestmap', 0)

    # Restore initial state
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    context.scene.vlmSettings.last_bake_step = 'nestmaps'
    print(f'\nNestmap generation finished ({n_nestmaps} nestmaps generated for {len(to_nest)} objects) in {str(datetime.timedelta(seconds=time.time() - start_time))}.')
    return {'FINISHED'}
