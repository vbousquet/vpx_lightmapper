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
from . import vlm_collections
from . import vlm_uvpacker
from PIL import Image # External dependency


def export_obj(op, context):
    ctx_area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if not ctx_area:
        op.report({'ERROR'}, 'This operator must be used with a 3D view active')
        return {'CANCELLED'}
    ctx_area.regions[-1].data.view_perspective = 'CAMERA'
    
    vlmProps = context.scene.vlmSettings
    opt_padding = vlmProps.padding
    opt_tex_size = int(vlmProps.tex_size)
    opt_pack_margin = 0.05 # ratio that we admit to loose in resolution to optimize grouped texture size
    max_level = max(0, opt_tex_size.bit_length() - 1)
    scale = 0.01 / vlm_utils.get_global_scale(context) # VPX has a defautl scale of 100, and Blender limit global_scale to 1000 (would need 1852 for inches)
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    selected_objects = list(context.selected_objects)
    for obj in [o for o in selected_objects if o.vlmSettings.bake_packmap >= 0]:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        if obj.vlmSettings.bake_type == 'playfield_fv':
            # FIXME implement for playfield
            pass
        else:
            # Duplicate
            bpy.ops.object.duplicate()
            dup = context.view_layer.objects.active
            # UV project and perform UV packing
            if obj.vlmSettings.bake_type == 'playfield':
                # FIXME implement for playfield
                pass
            else:
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
                opt_n = 0
                for n in range(max_level, 0, -1):
                    if (1.0 - opt_pack_margin) * obj.vlmSettings.bake_tex_factor <= 1.0 / (1 << n):
                        opt_n = n
                        break
                h_n = int(opt_n / 2)
                w_n = opt_n - h_n
                tex_width = int(opt_tex_size / (1 << w_n))
                tex_height = int(opt_tex_size / (1 << h_n))
                if vlmProps.uv_packer == 'blender':
                    bpy.ops.uv.pack_islands(margin=opt_padding / opt_tex_size)
                elif vlmProps.uv_packer == 'uvpacker':
                    vlm_uvpacker.uvpacker_pack([dup], opt_padding, tex_width, tex_height)
                bpy.ops.object.mode_set(mode='OBJECT')
            # Render pack texture with the per object UV coordinates
            path_png = bpy.path.abspath(f'{bakepath}{obj.vlmSettings.bake_name}.png')
            path_webp = bpy.path.abspath(f'{bakepath}{obj.vlmSettings.bake_name}.webp')
            pack_image = bpy.data.images.new('ExportMap', tex_width, tex_height, alpha=True)
            context.scene.render.bake.margin = opt_padding
            context.scene.render.bake.use_selected_to_active = False
            context.scene.render.bake.use_clear = True
            brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale) if obj.vlmSettings.bake_type == 'lightmap' else 1.0
            unloads = []
            for i, mat in enumerate(dup.data.materials):
                path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                loaded, render = vlm_utils.get_image_or_black(path)
                if loaded == 'loaded': unloads.append(render)
                mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
                mat.node_tree.nodes["BakeTex"].image = render
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0 if obj.vlmSettings.bake_type == 'lightmap' else 0.0 # Lightmap ?
                mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake
                mat.node_tree.nodes["PackMap"].inputs[4].default_value = brightness # HDR scale
                mat.node_tree.nodes["PackMap"].inputs[5].default_value = 0.0 # Enabled
                mat.node_tree.nodes["PackTex"].image = pack_image
                mat.blend_method = 'OPAQUE'
            bpy.ops.object.bake(type='COMBINED', pass_filter={'EMIT', 'DIRECT'}, margin=opt_padding)
            for render in unloads:
                bpy.data.images.remove(render)
            for mat in dup.data.materials:
                mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview
                mat.blend_method = 'BLEND' if obj.vlmSettings.bake_type == 'lightmap' else 'OPAQUE'
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
            Image.open(path_png).save(path_webp, 'WEBP')
            # Export
            bpy.ops.export_scene.obj(filepath=bpy.path.abspath(f'{bakepath}{obj.vlmSettings.bake_name}.obj'), use_selection=True, use_edges=False, use_materials=False, use_triangles=True, global_scale=scale, axis_forward='-Y', axis_up='-Z')
            # Delete temp object
            bpy.data.objects.remove(dup)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in selected_objects:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    return {"FINISHED"}
