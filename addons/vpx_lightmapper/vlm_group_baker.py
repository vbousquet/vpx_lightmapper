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
import math
import time
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency


def compute_render_groups(op, context):
    """Evaluate the set of bake groups (groups of objects that do not overlap when rendered 
    from the camera point of view) and store the result in the object properties.
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before computing groups')
        return {'CANCELLED'}
        
    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

    start_time = time.time()
    print(f"\nEvaluating render groups")
    opt_mask_size = 1024 # Height used for the object masks
    opt_mask_threshold = 0.0 # Alpha threshold used when building object groups
    opt_force_render = False # Force rendering even if cache is available
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    tmp_col = vlm_collections.get_collection('BAKETMP')
    root_bake_col = vlm_collections.get_collection('BAKE')
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    render_state = vlm_utils.push_render_settings(True)
    context.scene.render.engine = 'BLENDER_EEVEE'
    context.scene.render.film_transparent = True
    context.scene.eevee.taa_render_samples = 1
    context.scene.render.resolution_y = opt_mask_size
    context.scene.render.resolution_x = int(opt_mask_size * render_aspect_ratio)
    context.scene.render.image_settings.file_format = "PNG"
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '8'
    context.scene.world = bpy.data.worlds["VPX.Env.Black"]
    context.scene.use_nodes = False

    # at least 2 pixels padding around each objects to avoid overlaps, and help clean island spliting
    mask_pad = math.ceil(opt_mask_size * 2 / opt_tex_size)
    print(mask_pad)

    object_groups = []
    bakepath = vlm_utils.get_bakepath(context, type='MASKS')
    vlm_utils.mkpath(bakepath)
    all_objects = [obj for obj in root_bake_col.all_objects]
    for i, obj in enumerate(all_objects, start=1):
        obj.vlmSettings.render_group = -1
        if vlm_utils.is_part_of_bake_category(obj, 'movable'):
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it is a movable object")
            continue
        print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for '{obj.name}'")
        # Render object visibility mask (basic low res render)
        context.scene.render.filepath = f"{bakepath}{obj.name}.png"
        need_render = opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath))
        if not need_render:
            im = Image.open(bpy.path.abspath(context.scene.render.filepath))
            need_render = im.size[0] != context.scene.render.resolution_x or im.size[1] != context.scene.render.resolution_y
        if need_render:
            initial_collection = vlm_collections.move_to_col(obj, tmp_col)
            bpy.ops.render.render(write_still=True)
            vlm_collections.restore_col_links(initial_collection)
            im = Image.open(bpy.path.abspath(context.scene.render.filepath))
        # Evaluate if this object can be grouped with previous renders (no alpha overlaps)
        for p in range(mask_pad):
            im.alpha_composite(im, (0, 1))
            im.alpha_composite(im, (0, -1))
            im.alpha_composite(im, (1, 0))
            im.alpha_composite(im, (-1, 0))
        alpha = im.tobytes("raw", "A")
        n_groups = len(object_groups)
        obj.vlmSettings.render_group = n_groups
        r = range(0, n_groups) if (i % 2) == 0 else range(n_groups-1, -1, -1)
        for group_index in r:
            group = object_groups[group_index]
            ga = group['mask']
            if next((b for b in zip(alpha, ga) if b[0] > opt_mask_threshold and b[1] > opt_mask_threshold), None) is None:
                obj.vlmSettings.render_group = group_index
                group['mask'] = [max(b[0],b[1]) for b in zip(alpha, ga)]
                group['objects'].append(obj)
                break
        if obj.vlmSettings.render_group == len(object_groups):
            object_groups.append({'objects': [obj], 'mask': alpha})
    
    # Save group masks for later use
    for i, group in enumerate(object_groups):
        im = Image.frombytes('L', (context.scene.render.resolution_x, context.scene.render.resolution_y), bytes(group['mask']), 'raw')
        im.save(bpy.path.abspath(f"{bakepath}Group {i}.png"))

    context.scene.render.engine = 'CYCLES'
    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    vlm_utils.pop_render_settings(render_state)

    vlm_collections.delete_collection(tmp_col)
    vlm_collections.pop_state(col_state)

    print(f"\n{len(object_groups)} render groups defined in {vlm_utils.format_time(time.time() - start_time)}.")
    context.scene.vlmSettings.last_bake_step = 'groups'
    return {'FINISHED'}
    
