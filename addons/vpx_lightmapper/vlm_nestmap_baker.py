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
from . import vlm_nest
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency


def render_nestmaps(op, context):
    ctx_area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if not ctx_area:
        op.report({'ERROR'}, 'This operator must be used with a 3D view active')
        return {'CANCELLED'}
    ctx_area.regions[-1].data.view_perspective = 'CAMERA'

    result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
    if not result_col or len(result_col.all_objects) == 0:
        op.report({'ERROR'}, 'No bake result to process')
        return {'CANCELLED'}
    
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    selected_objects = list(context.selected_objects)
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    render_size = (int(opt_tex_size * context.scene.vlmSettings.render_aspect_ratio), opt_tex_size)

    # reset UV of target objects (2 layers: 1 for default view projected, 1 for nested UV)
    ldr_objects = []
    hdr_objects = []
    for obj in [o for o in result_col.all_objects]:
        uvs = [uv for uv in obj.data.uv_layers]
        while uvs:
            obj.data.uv_layers.remove(uvs.pop())
        obj.data.uv_layers.new(name='UVMap Nested')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')
        override = context.copy()
        override["object"] = override["active_object"] = obj
        override["selected_objects"] = override["selected_editable_objects"] = [obj]
        override["area"] = ctx_area
        override["space_data"] = ctx_area.spaces.active
        override["region"] = ctx_area.regions[-1]
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        obj.data.uv_layers.new(name='UVMap')
        if obj.vlmSettings.bake_hdr_range > 1.0:
            hdr_objects.append(obj)
        else:
            ldr_objects.append(obj)

    # Perform the actual island nesting and nestmap generation
    max_tex_size = min(4096, 2 * opt_tex_size)
    nestmap_offset = 0
    if ldr_objects:
        n_nestmap, splitted_objects = vlm_nest.nest(context, ldr_objects, render_size, max_tex_size, max_tex_size, 'Nestmap', nestmap_offset)
        nestmap_offset = nestmap_offset + n_nestmap
    if hdr_objects:
        n_nestmap, splitted_objects = vlm_nest.nest(context, hdr_objects, render_size, max_tex_size, max_tex_size, 'Nestmap', nestmap_offset)
        nestmap_offset = nestmap_offset + n_nestmap

    # Restore initial state
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    context.scene.vlmSettings.last_bake_step = 'nestmaps'
    print(f'Nestmap generation finished ({nestmap_offset - 1} nestmap generated).')
    return {'FINISHED'}
