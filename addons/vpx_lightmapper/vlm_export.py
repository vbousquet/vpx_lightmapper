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
import os
from . import vlm_utils
from . import vlm_collections

# TODO
# - Try exporting bakes as HDR (or provide an evaluation of the benefit it would give)
# - Try generating a default VBS script for eady integration
# - Try computing bakemap histogram, select the right format depending on the intensity span (EXR / brightness adjusted PNG or WEBP)


def export_packmap(bake_instance, name, is_light, tex_width, tex_height, opt_save_webp, opt_padding, opt_force_render, target_path):
    if opt_force_render or not os.path.exists(target_path):
        pack_image = bpy.data.images.new(f"PackMap.{name}", tex_width, tex_height, alpha=True)
        for mat in bake_instance.data.materials:
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake mode
            if is_light:
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mat.blend_method = 'OPAQUE'
            mat.node_tree.nodes["PackTex"].image = pack_image
            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
        bpy.context.scene.render.bake.margin = opt_padding
        bpy.context.scene.render.bake.use_clear = True
    
        cg = vlm_utils.push_color_grading(True)
        bpy.ops.object.bake(type='EMIT', margin=opt_padding)
        pack_image.save_render(target_path)
        vlm_utils.pop_color_grading(cg)
        
        bpy.data.images.remove(pack_image)
        for mat in bake_instance.data.materials:
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
            if is_light:
                mat.blend_method = 'BLEND'
    if opt_save_webp:
        webp_path = bpy.path.abspath(context.scene.render.filepath).removesuffix('.png') + '.webp'
        if opt_force_render or not os.path.exists(webp_path):
            im = Image.open(target_path)
            im.save(webp_path, 'WEBP')


def export_all(context):
    vlmProps = context.scene.vlmSettings
    result_col = vlm_collections.get_collection('BAKE RESULT')
    exportpath = f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Export/"
    if not os.path.exists(bpy.path.abspath(exportpath)):
        os.mkdir(bpy.path.abspath(exportpath))
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, result_col).exclude = False
    print(f"\nStarting export process for {len(result_col.all_objects)} bakes")
    
    for i, obj in enumerate(result_col.all_objects, start = 1):
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        print(f". {i}/{len(result_col.all_objects)} Exporting packed bake maps for '{obj.name}'")
        export_packmap(obj, obj["vlm.name"], obj["vlm.is_light"] != 0, obj["vlm.tex_width"], obj["vlm.tex_height"], vlmProps.export_webp, vlmProps.padding, False, bpy.path.abspath(f"{exportpath}{obj.name}.png"))

        # see https://docs.blender.org/api/current/bpy.ops.export_scene.html
        print(f". {i}/{len(result_col.all_objects)} Exporting bake mesh for '{obj.name}'")
        bpy.ops.export_scene.obj(
            filepath=os.path.join(bpy.path.abspath(exportpath), f"{obj.name}.obj"),
            use_selection=True, use_materials=False, use_triangles=True,
            global_scale=1000.0 / global_scale, axis_forward='-Y', axis_up='-Z')

    print(f"\nExport finished.")
    return {"FINISHED"}

