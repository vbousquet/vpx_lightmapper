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

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    start_time = time.time()
    print(f"\nEvaluating render groups")
    opt_mask_size = 1024 # Height used for the object masks
    opt_force_render = False # Force rendering even if cache is available
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    # at least 2 pixels padding around each objects to avoid overlaps, and help clean island spliting
    opt_mask_pad = math.ceil(opt_mask_size * 2 / opt_tex_size)
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio

    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.film_transparent = True
    scene.render.resolution_y = opt_mask_size
    scene.render.resolution_x = int(opt_mask_size * render_aspect_ratio)
    scene.render.pixel_aspect_x = context.scene.render.pixel_aspect_x
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '8'
    scene.eevee.taa_render_samples = 1
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    scene.world = bpy.data.worlds["VPX.Env.Black"]
    scene.use_nodes = False

    object_masks = []
    bakepath = vlm_utils.get_bakepath(context, type='MASKS')
    vlm_utils.mkpath(bakepath)
    all_objects = list(bake_col.all_objects)
    i = 0 # FIXME count masks
    while all_objects:
        obj = all_objects.pop()
        obj.vlmSettings.render_group = -1
        if vlm_utils.is_part_of_bake_category(obj, 'movable'):
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it is a movable object")
            continue
        if obj.vlmSettings.indirect_only:
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it is only indirectly influencing the scene")
            continue
        if obj.vlmSettings.bake_to:
            scene.render.filepath = f"{bakepath}{vlm_utils.clean_filename(obj.vlmSettings.bake_to.name)}.png"
            obj_group = [o for o in all_objects if o.vlmSettings.bake_to == obj.vlmSettings.bake_to]
            obj_group.append(obj.vlmSettings.bake_to)
            all_objects = [o for o in all_objects if o not in obj_group]
            print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for bake target '{obj.vlmSettings.bake_to.name}' ({[o.name for o in obj_group]})")
        else:
            scene.render.filepath = f"{bakepath}{vlm_utils.clean_filename(obj.name)}.png"
            obj_group = [obj]
            print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for '{obj.name}'")
        need_render = opt_force_render or not os.path.exists(bpy.path.abspath(scene.render.filepath))
        if not need_render:
            im = Image.open(bpy.path.abspath(scene.render.filepath))
            need_render = im.size[0] != scene.render.resolution_x or im.size[1] != scene.render.resolution_y
        if need_render:
            for o in obj_group: scene.collection.objects.link(o)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            for o in obj_group: scene.collection.objects.unlink(o)
            im = Image.open(bpy.path.abspath(scene.render.filepath))
        # Evaluate if this object can be grouped with previous renders (no overlaps)
        for p in range(opt_mask_pad):
            im.alpha_composite(im, (0, 1))
            im.alpha_composite(im, (0, -1))
            im.alpha_composite(im, (1, 0))
            im.alpha_composite(im, (-1, 0))
        alpha = im.tobytes("raw", "A")
        n_groups = len(object_masks)
        g = n_groups
        r = range(0, n_groups) if (i % 2) == 0 else range(n_groups-1, -1, -1)
        for group_index in r:
            ga = object_masks[group_index]
            if next((b for b in zip(alpha, ga) if b[0] > 0.0 and b[1] > 0.0), None) is None:
                object_masks[group_index] = [max(b[0],b[1]) for b in zip(alpha, ga)]
                g = group_index
                break
        if g == n_groups: object_masks.append(alpha)
        for o in obj_group: o.vlmSettings.render_group = g
    
    # Save group masks for later use
    for i, group in enumerate(object_masks):
        im = Image.frombytes('L', (scene.render.resolution_x, scene.render.resolution_y), bytes(group), 'raw')
        im.save(bpy.path.abspath(f"{bakepath}Group {i}.png"))

    print(f"\n{len(object_masks)} render groups defined in {vlm_utils.format_time(time.time() - start_time)}.")
    bpy.data.scenes.remove(scene)
    context.scene.vlmSettings.last_bake_step = 'groups'
    return {'FINISHED'}
    
