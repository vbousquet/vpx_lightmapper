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
from . import vlm_utils
from . import vlm_nest


def export_obj(op, context):
    ctx_area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if not ctx_area:
        op.report({'ERROR'}, 'This operator must be used with a 3D view active')
        return {'CANCELLED'}
    ctx_area.regions[-1].data.view_perspective = 'CAMERA'
    
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    selected_objects = list(context.selected_objects)

    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    render_size = (int(opt_tex_size * context.scene.vlmSettings.render_aspect_ratio), opt_tex_size)

    # Duplicate and reset UV of target objects
    to_nest = []
    for obj in [o for o in selected_objects if o.vlmSettings.bake_name != '']:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.duplicate()
        dup = context.view_layer.objects.active
        uvs = [uv for uv in dup.data.uv_layers]
        while uvs:
            dup.data.uv_layers.remove(uvs.pop())
        uv_layer = dup.data.uv_layers.new(name='UVMap')
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')
        override = context.copy()
        override["object"] = override["active_object"] = dup
        override["selected_objects"] = override["selected_editable_objects"] = [dup]
        override["area"] = ctx_area
        override["space_data"] = ctx_area.spaces.active
        override["region"] = ctx_area.regions[-1]
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        to_nest.append(dup)

    # Perform the actual island nesting and packmap generation
    vlm_nest.nest(context, to_nest, render_size, 4096, 4096)

    # Export Wavefront objects
    for dup in to_nest:
        # Remove initial split materials
        dup.active_material_index = 0
        for i in range(len(dup.material_slots)):
            bpy.ops.object.material_slot_remove({'object': dup})
        # Export object
        scale = 0.01 / vlm_utils.get_global_scale(context) # VPX has a default scale of 100, and Blender limit global_scale to 1000 (would need 1852 for inches), so 0.01 makes things ok for everyone
        bpy.ops.export_scene.obj(filepath=bpy.path.abspath(f'{bakepath}{obj.name}.obj'), use_selection=True, use_edges=False, use_materials=False, use_triangles=True, global_scale=scale, axis_forward='-Y', axis_up='-Z')
        # Delete created object
        #bpy.data.objects.remove(dup)

    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    return {'FINISHED'}
