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
import mathutils
import time
from . import vlm_utils
from . import vlm_camera
from . import vlm_collections
from PIL import Image # External dependency

def projected_bounds_area(mvp_matrix, obj):
    max_x = max_y = -10000000
    min_x = min_y = 10000000
    if obj.type == 'MESH':
        corners = [mvp_matrix @ obj.matrix_world @ mathutils.Vector((v.co[0], v.co[1], v.co[2], 1)) for v in obj.data.vertices]
    else:
        corners = [mvp_matrix @ obj.matrix_world @ mathutils.Vector((corner[0], corner[1], corner[2], 1)) for corner in obj.bound_box]
    proj_x = [o for o in map(lambda a: a.x / a.w, corners)]
    proj_y = [o for o in map(lambda a: a.y / a.w, corners)]
    min_x = min(min_x, min(proj_x))
    min_y = min(min_y, min(proj_y))
    max_x = max(max_x, max(proj_x))
    max_y = max(max_y, max(proj_y))
    return (max_x - min_x) * (max_y - min_y)


def compute_render_groups(op, context):
    """Evaluate the set of bake groups (groups of objects that do not overlap when rendered 
    from the camera point of view) and store the result in the 'group' property of objects.
    It will also compute 2 group masks:
    - 'Mask - Group xx.png' a full resolution of the group mask, including border fade (used during nesting for border padding)
    - 'Mask - Group xx (Padded LD).png' a low resolution of the group mask, including a 1 pixel padding (used to discard renders according to light AOI)
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before computing groups')
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

    # Force a camera update
    vlm_camera.camera_inclination_update(op, context)

    # at least 2 pixels padding around each objects to avoid overlaps, and help clean island spliting => depends on actual render size
    render_size = vlm_utils.get_render_size(context)
    opt_mask_pad = math.ceil(opt_mask_size * 2 / render_size[1])
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
    scene.world = None
    scene.use_nodes = False

    modelview_matrix = camera_object.matrix_world.inverted()
    projection_matrix = camera_object.calc_matrix_camera(context.evaluated_depsgraph_get(),
        x = scene.render.resolution_x,
        y = scene.render.resolution_y,
        scale_x = scene.render.pixel_aspect_x,
        scale_y = scene.render.pixel_aspect_y)
    mvp_matrix = projection_matrix @ modelview_matrix

    object_masks = []
    bakepath = vlm_utils.get_bakepath(context, type='MASKS')
    vlm_utils.mkpath(bakepath)
    for obj in bake_col.all_objects:
        obj.vlmSettings.render_group = -1
    all_objects = list([o for o in bake_col.all_objects if not o.vlmSettings.indirect_only])
    object_surfaces = [projected_bounds_area(mvp_matrix, o) for o in all_objects]
    all_objects = sorted(zip(object_surfaces, all_objects), key=lambda pair: pair[0], reverse=True)
    for i, (area, obj) in enumerate(all_objects, start=1):
        if obj.vlmSettings.indirect_only:
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it is only indirectly influencing the scene")
            continue
        if obj.vlmSettings.render_group != -1: # Render group already defined
            continue
        if obj.vlmSettings.bake_to:
            scene.render.filepath = f"{bakepath}{vlm_utils.clean_filename(obj.vlmSettings.bake_to.name)}.png"
            obj_group = [o for _, o in all_objects if o.vlmSettings.bake_to == obj.vlmSettings.bake_to]
            obj_group.append(obj.vlmSettings.bake_to)
            print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for bake target '{obj.vlmSettings.bake_to.name}' ({[o.name for o in obj_group]} with a total projected area of {area})")
        else:
            scene.render.filepath = f"{bakepath}{vlm_utils.clean_filename(obj.name)}.png"
            obj_group = [obj]
            print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for '{obj.name}' (projected area of {area})")
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
        if obj.vlmSettings.use_bake:
            im = Image.frombytes('L', (scene.render.resolution_x, scene.render.resolution_y), bytes(alpha), 'raw')
            im.save(bpy.path.abspath(f'{bakepath}Mask - Bake - {obj.name} (Padded LD).png'))
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it use traditional baking instead of projective baking")
            continue
        n_groups = len(object_masks)
        g = n_groups
        for group_index in range(n_groups):
            ga = object_masks[group_index]
            if next((b for b in zip(alpha, ga) if b[0] > 0.0 and b[1] > 0.0), None) is None:
                object_masks[group_index] = [max(b[0],b[1]) for b in zip(alpha, ga)]
                g = group_index
                break
        if g == n_groups:
            object_masks.append(alpha)
        for o in obj_group:
            o.vlmSettings.render_group = g
    
    # Save group masks for later use
    for i, group in enumerate(object_masks):
        im = Image.frombytes('L', (scene.render.resolution_x, scene.render.resolution_y), bytes(group), 'raw')
        im.save(bpy.path.abspath(f'{bakepath}Mask - Group {i} (Padded LD).png'))


    print(f"\n{len(object_masks)} render groups defined in {vlm_utils.format_time(time.time() - start_time)}.")
    bpy.data.scenes.remove(scene)

    # render hi-res mask for later nestmap rendering
    render_group_masks(op, context)

    context.scene.vlmSettings.last_bake_step = 'groups'
    return {'FINISHED'}
    

def render_group_masks(op, context):
    """Render render groups masks
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering')
        return {'CANCELLED'}

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    n_render_groups = vlm_utils.get_n_render_groups(context)
    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)

    # Create temp render scene, using the user render settings setup
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    scene.render.engine = 'CYCLES'
    for prop in context.scene.render.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type', ''}:
            setattr(scene.render, prop.identifier, getattr(context.scene.render, prop.identifier))
    for prop in context.scene.cycles.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type', 'denoiser', ''}:
            setattr(scene.cycles, prop.identifier, getattr(context.scene.cycles, prop.identifier))
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    render_size = vlm_utils.get_render_size(context)
    scene.render.resolution_x = render_size[0]
    scene.render.resolution_y = render_size[1]
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    scene.view_layers[0].use_pass_combined = True
    scene.view_layers[0].use_pass_z = False
    scene.cycles.max_bounces = 1
    scene.cycles.use_denoising = False

    mask_mat = bpy.data.materials.new(name='VPX.Mask')
    mask_mat.use_nodes = True
    nodes = mask_mat.node_tree.nodes
    nodes.clear()
    links = mask_mat.node_tree.links
    emit = nodes.new('ShaderNodeEmission')
    node_output = nodes.new(type='ShaderNodeOutputMaterial')   
    node_output.location.x = 400
    links.new(emit.outputs[0], node_output.inputs[0])
    scene.view_layers[0].material_override = mask_mat

    print(f'\nEvaluating {n_render_groups} render group masks')
    bakepath = vlm_utils.get_bakepath(context, type='MASKS')
    for group_index in range(n_render_groups):
        linked_objects = []
        for obj in bake_col.all_objects:
            if obj.vlmSettings.render_group == group_index and not obj.vlmSettings.indirect_only:
                # if obj.vlmSettings.bake_mask and obj.vlmSettings.bake_mask not in linked_objects:
                    # scene.collection.objects.link(obj.vlmSettings.bake_mask)
                    # linked_objects.append(obj.vlmSettings.bake_mask)
                if obj.vlmSettings.bake_to:
                    obj = obj.vlmSettings.bake_to
                if obj not in linked_objects:
                    scene.collection.objects.link(obj)
                    linked_objects.append(obj)
        print(f'\n. Rendering group #{group_index+1}/{n_render_groups} ({len(linked_objects)} objects)')
        
        scene.render.filepath = f'{bakepath}Mask - Group {group_index}.png'
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGBA'
        scene.render.image_settings.color_depth = '8'
        bpy.ops.render.render(write_still=True, scene=scene.name)

        for obj in linked_objects:
            scene.collection.objects.unlink(obj)

    # Hires mask are not used for baked objects
    # for obj in bake_col.all_objects:
        # if not obj.vlmSettings.use_bake:
            # continue
        # scene.collection.objects.link(obj)
        # print(f'\n. Rendering mask for {obj}')
        # scene.render.filepath = f'{bakepath}Mask - Bake - {obj.name}.png'
        # scene.render.image_settings.file_format = 'PNG'
        # scene.render.image_settings.color_mode = 'RGBA'
        # scene.render.image_settings.color_depth = '8'
        # bpy.ops.render.render(write_still=True, scene=scene.name)
        # scene.collection.objects.unlink(obj)

    bpy.data.materials.remove(mask_mat)
    bpy.data.scenes.remove(scene)
    length = time.time() - start_time
    print(f'\nRendering group masks finished in a total time of {vlm_utils.format_time(length)}')
    return {'FINISHED'}
