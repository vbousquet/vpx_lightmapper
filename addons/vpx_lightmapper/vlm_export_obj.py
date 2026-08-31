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
    camera = context.scene.camera
    if not camera:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    selected_objects = list(context.selected_objects)

    render_size = vlm_utils.get_render_size(context)
    proj_ar = vlm_utils.get_render_proj_ar(context)

    scale = 1.0 / vlm_utils.get_global_scale(context)
    # OBJ stores the complete world transform in the mesh below. To make the
    # resulting OBJ numerically match VLM's VPX mesh coordinates (which are
    # written as vertex / global_scale), the OBJ exporter must use the inverse
    # of VLM's global scale. In VPX units this is 100, so the OBJ can be
    # imported into VPX with Scale = 1 instead of Scale = 100.

    # Export non-bake objects
    #
    # IMPORTANT: The VPX batch exporter writes mesh vertices in object-local
    # coordinates and stores the object's transform separately (VPOS/VSIZ/RTV*).
    # OBJ has no equivalent VPX transform record, so the object's complete
    # world transform must be baked into the exported mesh.  The old code passed
    # the object directly to Blender's OBJ exporter, which made the single OBJ
    # export depend on the OBJ exporter's transform handling and caused position
    # and size mismatches when the OBJ was imported into VPX.
    for obj in [o for o in selected_objects if o.vlmSettings.bake_lighting == '']:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        # Work on a temporary copy so the user's original object is untouched.
        bpy.ops.object.duplicate()
        dup = context.view_layer.objects.active
        dup.name = f'ExpOBJ.{obj.name}'

        # Match the transform treatment used by the batch/bake exporter:
        # bake matrix_world into the mesh and leave the exported object at the
        # origin with unit/identity object transform. This makes OBJ coordinates
        # unambiguous and keeps position, rotation and scale consistent.
        dup.data.transform(dup.matrix_world)
        dup.matrix_world.identity()

        bpy.ops.wm.obj_export(
            filepath=bpy.path.abspath(f'{bakepath}{obj.name}.obj'),
            export_selected_objects=True,
            global_scale=scale,
            forward_axis='NEGATIVE_Y',
            up_axis='NEGATIVE_Z',
            export_materials=False,
            export_triangulated_mesh=True
        )

        # Remove only the temporary export copy.
        bpy.data.objects.remove(dup, do_unlink=True)


    # Duplicate and reset UV of target bake objects (which require a nestmap)
    to_nest = []
    for obj in [o for o in selected_objects if o.vlmSettings.bake_lighting != '']:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.duplicate()
        dup = context.view_layer.objects.active
        dup.name = f'ExpOBJ.{obj.name}'
        uvs = [uv for uv in dup.data.uv_layers]
        while uvs:
            dup.data.uv_layers.remove(uvs.pop())
        dup.data.uv_layers.new(name='UVMap Nested')
        vlm_utils.project_uv(camera, dup, proj_ar)
        dup.data.uv_layers.new(name='UVMap')
        to_nest.append(dup)

    if to_nest:
        # Perform the actual island nesting and packmap generation
        export_name = 'ExportObj'
        if len([o for o in selected_objects if o.vlmSettings.bake_lighting != '']) == 1:
            export_name = next((o for o in selected_objects if o.vlmSettings.bake_lighting != '')).name
        max_tex_size = min(8192, int(context.scene.vlmSettings.tex_size))
        if max(render_size) > max_tex_size:
            op.report({'ERROR'}, 'Texture size must be greater than render height')
            return {'CANCELLED'}

        n_nestmap, splitted_objects = vlm_nest.nest(context, to_nest, 'UVMap', 'UVMap Nested', max_tex_size, max_tex_size, export_name, 0)
        to_nest.extend(splitted_objects)

        # Export Wavefront objects
        for dup in to_nest:
            # Match the batch exporter transform convention. The batch path
            # applies each bake object's complete world transform to its mesh
            # before exporting and then resets the object transform. Without
            # this, the single OBJ path can export different position/scale data.
            dup.data.transform(dup.matrix_world)
            dup.matrix_world.identity()

            # Remove initial split materials
            dup.active_material_index = 0
            for i in range(len(dup.material_slots)):
                bpy.ops.object.material_slot_remove({'object': dup})
            # Export object
            bpy.ops.object.select_all(action='DESELECT')
            dup.select_set(True)
            context.view_layer.objects.active = dup
            bpy.ops.export_scene.obj(filepath=bpy.path.abspath(f'{bakepath}{dup.name}.obj'), use_selection=True, use_edges=False, use_materials=False, use_triangles=True, global_scale=scale, axis_forward='-Y', axis_up='-Z')
            #bpy.ops.wm.(filepath=bpy.path.abspath(f'{bakepath}{dup.name}.obj'), use_selection=True, use_edges=False, use_materials=False, use_triangles=True, global_scale=scale, axis_forward='-Y', axis_up='-Z')
            # Delete created object
            #bpy.data.objects.remove(dup)

    # Restore initial state
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj

    print(f'Export finished')
    return {'FINISHED'}
