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
import math
import mathutils
import bmesh
import os
import time
import gpu
import datetime
import numpy as np
from math import radians
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from . import vlm_collections
from . import vlm_uvpacker
from PIL import Image # External dependency

global_scale = vlm_utils.global_scale

# TODO
# - Implement 'Movable' bake mode (each object is baked to a separate mesh, keeping its origin)
#   . Object must be UV unwrapped, and must have either a VLM.BakeTex node in its material (first slot) or has an imported VPX image (bake will be the same size as the image)
#   . Light map are computed on the UV unwrapped model, filtered based on a custom threshold
#   . Implementation progress:
#     x At group step, just hide them
#     . At render step, set them to hide/indirect for others, then adapt material (create VLM.BakeTex if missing, same size as VPX Texture) perform a bake for each movable/light scenarios, also compute light threshold of the bake and allow to access it from the UI (for easy adjustment of the lighmap bake threshold)
#     . At mesh step, copy the movables to bake result and generate a light mesh for each on the lightmap above threshold
#     . At packmap step, copy bakes to export (with exr to png/webp conversion)
#     . At export step, include them in the VPX and produce sync code
# - Apply layback lattice transform when performing UV projection
# - When baking lights, we bake them to white (for later coloring) then apply overlay, therefore overlay are colored...


def remove_backfacing(context, obj, eye_position, limit):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    context.view_layer.objects.active = obj
    dot_limit = math.cos(radians(limit + 90))
    bpy.ops.object.mode_set(mode = 'EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for face in bm.faces:
        normal = face.normal
        if normal.length_squared < 0.5:
            face.select_set(False)
        else:
            incoming = eye_position - face.calc_center_bounds()
            dot_value = normal.dot(incoming)
            if dot_value >= dot_limit:
                face.select_set(False)
            else:
                face.select_set(True)
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.mesh.delete(type='FACE')
    bpy.ops.object.mode_set(mode = 'OBJECT') 


def compute_uvmap_density(mesh, uv_layer):
    area_sum = 0
    for poly in mesh.polygons:
        surface = []
        for loop_index in poly.loop_indices:
            uv_base = uv_layer.data[loop_index].uv
            surface.append(mathutils.Vector((uv_base[0], uv_base[1], 0)))
        if len(surface) == 3:
            area = mathutils.geometry.area_tri(surface[0], surface[1], surface[2])
        elif len(surface) == 4:
            area = mathutils.geometry.area_tri(surface[0], surface[1], surface[2]) + mathutils.geometry.area_tri(surface[0], surface[2], surface[3])
        else:
            bm = bmesh.new()
            verts = []
            for v in surface:
                verts.append(bm.verts.new(v))
            bm.verts.ensure_lookup_table()
            face = bm.faces.new(verts)
            bm.faces.ensure_lookup_table()
            area = face.calc_area()
            bm.free()
        if not math.isnan(area) and not math.isinf(area):
            area_sum += area
    return area_sum


def get_lightings(context):
    """Return the list of lighting situations to be rendered as list of tuples
        (name, None/light collection, single light, custom data)
    """
    world = ["Environment", None, [], None]
    light_scenarios = {"Environment": world}
    lights_col = vlm_collections.get_collection('LIGHTS', create=False)
    if lights_col is not None:
        for light_col in lights_col.children:
            lights = light_col.objects
            if light_col.hide_render == False and len(lights) > 0:
                if light_col.vlmSettings.light_mode == 'group':
                    name = vlm_utils.strip_vlm(light_col.name)
                    light_scenarios[name] = [name, light_col, None, None]
                elif light_col.vlmSettings.light_mode == 'split':
                    for light in lights:
                        name = f"{vlm_utils.strip_vlm(light_col.name)}-{light.name}"
                        light_scenarios[name] = [name, light_col, light, None]
                elif light_col.vlmSettings.light_mode == 'world':
                    world[2].extend(lights)
    return light_scenarios


def get_n_lightings(context):
    return len(get_lightings(context))
    
    
def get_n_render_groups(context):
    n = 0
    root_bake_col = vlm_collections.get_collection('BAKE', create=False)
    if root_bake_col is not None:
        for obj in root_bake_col.all_objects:
            n = max(n, obj.vlmSettings.render_group + 1)
    return n


def compute_render_groups(op, context):
    """Evaluate the set of bake groups (groups of objects that do not overlap when rendered 
    from the camera point of view) and store the result in the object properties.
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before computing groups')
        return {'CANCELLED'}

    start_time = time.time()
    print(f"\nEvaluating render groups")
    opt_mask_size = 1024 # Height used for the object masks
    opt_mask_threshold = 0.0 # Alpha threshold used when building object groups
    opt_force_render = False # Force rendering even if cache is available
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    tmp_col = vlm_collections.get_collection('BAKETMP')
    root_bake_col = vlm_collections.get_collection('BAKE')
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

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

    object_groups = []
    bakepath = vlm_utils.get_bakepath(context, type='MASKS')
    vlm_utils.mkpath(bakepath)
    all_objects = [obj for obj in root_bake_col.all_objects]
    for i, obj in enumerate(all_objects, start=1):
        if vlm_utils.is_object_in_movable(obj):
            obj.vlmSettings.render_group = -1
            print(f". Skipping   object mask #{i:>3}/{len(all_objects)} for '{obj.name}' since it is movable")
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
        alpha = im.tobytes("raw", "A")
        obj.vlmSettings.render_group = len(object_groups)
        for group_index, group in enumerate(object_groups):
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

    context.scene.eevee.taa_render_samples = 64
    context.scene.render.engine = 'CYCLES'
    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.pop_state(col_state)
    print(f"\n{len(object_groups)} render groups defined in {vlm_utils.format_time(time.time() - start_time)}.")
    context.scene.vlmSettings.last_bake_step = 'groups'
    return {'FINISHED'}

def render_all_groups(op, context):
    """Render all render groups for all lighting situations
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering')
        return
    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    opt_force_render = False # Force rendering even if cache is available
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    render_border_state = (context.scene.render.use_border, context.scene.render.use_crop_to_border,
                context.scene.render.border_min_x,context.scene.render.border_max_x,
                context.scene.render.border_min_y,context.scene.render.border_max_y)
    context.scene.render.use_border = False
    context.scene.render.use_crop_to_border = False
    context.scene.render.resolution_y = opt_tex_size
    context.scene.render.resolution_x = int(opt_tex_size * render_aspect_ratio)
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    #context.scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression whiis too big, 
    # another way to compact lightmaps is to remove the alpha channel using the composer (moderate win and makes the code more complex)
    context.scene.render.image_settings.exr_codec = 'DWAA' # Lossy compression (5x to 10x smaller on lightmaps)
    context.scene.render.image_settings.color_depth = '16'
    context.scene.view_layers["ViewLayer"].use_pass_z = True
    context.scene.render.film_transparent = True
    context.scene.cycles.film_transparent_glass = True
    context.scene.use_nodes = False
    cg = vlm_utils.push_color_grading(True)
    n_render_performed = 0

    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    tmp_col = vlm_collections.get_collection('BAKETMP')
    indirect_col = vlm_collections.get_collection('INDIRECT')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    lights_col = vlm_collections.get_collection('LIGHTS')
    world_col = vlm_collections.get_collection('WORLD')
    root_bake_col = vlm_collections.get_collection('BAKE')
    overlay_col = vlm_collections.get_collection('OVERLAY')
    vlm_collections.find_layer_collection(rlc, vlm_collections.get_collection('HIDDEN')).exclude = True
    vlm_collections.find_layer_collection(rlc, vlm_collections.get_collection('TRASH')).exclude = True
    vlm_collections.find_layer_collection(rlc, result_col).exclude = True
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, indirect_col).exclude = False
    vlm_collections.find_layer_collection(rlc, indirect_col).indirect_only = True
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, root_bake_col).exclude = False
    vlm_collections.find_layer_collection(rlc, overlay_col).indirect_only = True
    for bake_col in root_bake_col.children:
        vlm_collections.find_layer_collection(rlc, bake_col).exclude = False
        vlm_collections.find_layer_collection(rlc, bake_col).indirect_only = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    n_render_groups = get_n_render_groups(context)
    light_scenarios = get_lightings(context)
    n_lighting_situations = len(light_scenarios)

    bake_info_group = bpy.data.node_groups.get('VLM.BakeInfo')

    # Restore state after setting up a light scenario for rendering
    def restore_light_setup(initial_state):
        if initial_state[0] == 0: # World
            vlm_collections.restore_all_col_links(initial_state[1])
        elif initial_state[0] == 1: # Group lightmap, pre-colored
            vlm_collections.restore_all_col_links(initial_state[1])
        elif initial_state[0] == 2: # Split lightmap, pre-colored
            vlm_collections.restore_col_links(initial_state[1])
        elif initial_state[0] == 3: # Group lightmap, white
            for obj, color in zip(initial_state[1], initial_state[2]): obj.data.color = color
            vlm_collections.restore_all_col_links(initial_state[3])
        elif initial_state[0] == 4: # Split lightmap, white
            initial_state[1].data.color = initial_state[2]
            vlm_collections.restore_col_links(initial_state[3])
        if bake_info_group:
            bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 0.0
            bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 0.0
        
    camera = bpy.data.objects['Bake Camera']
    camera_rotation = mathutils.Quaternion(camera.rotation_quaternion)
    modelview_matrix = camera.matrix_world.inverted()
    projection_matrix = camera.calc_matrix_camera(
        context.view_layer.depsgraph,
        x = context.scene.render.resolution_x,
        y = context.scene.render.resolution_y,
        scale_x = context.scene.render.pixel_aspect_x,
        scale_y = context.scene.render.pixel_aspect_y,
    )
    
    def project_point(p, w, h):
        p1 = projection_matrix @ modelview_matrix @ Vector((p.x, p.y, p.z, 1)) # projected coordinates
        return Vector(((w - 1) * 0.5 * (1 + p1.x / p1.w), (h - 1) * 0.5 * (1 - p1.y / p1.w))) # pixel coordinates
    
    # Check if light is influencing with regard to the provided group mask using an ellipsoid influence bound
    def get_light_influence(light, group_mask):
        if not group_mask:
            w = context.scene.render.resolution_x
            h = context.scene.render.resolution_y
            mask = None
        else:
            w, h, mask = group_mask
        if light.type != 'LIGHT':
            return (0, 1, 0, 1)
        influence_radius = light.data.shadow_soft_size + math.log10(light.data.energy) * 250 * global_scale # empirically observed
        light_center = project_point(Vector(light.location), w, h)
        light_xr = (project_point(Vector(light.location) + Vector((influence_radius, 0, 0)), w, h) - light_center).x # projected radius on x axis
        light_yr = (project_point(Vector(light.location) + camera_rotation @ Vector((0, influence_radius, 0)), w, h) - light_center).length # projected radius on y axis
        min_x = max(  0, int(light_center.x - light_xr))
        max_x = min(w-1, int(light_center.x + light_xr))
        min_y = max(  0, int(light_center.y - light_yr))
        max_y = min(h-1, int(light_center.y + light_yr))
        if not mask: # No mask, return the area of influence of the light
            return (max(0, (light_center.x - light_xr) / w), min(1, (light_center.x + light_xr) / w),
                max(  0, (light_center.y - light_yr) / h), min(1, (light_center.y + light_yr) / h))
        alpha_y = light_yr / light_xr
        max_r2 = light_xr * light_xr
        for y in range(min_y, max_y + 1):
            py = (y - light_center.y) * alpha_y
            py2 = py * py
            for x in range(min_x, max_x + 1):
                px = x - light_center.x
                if px*px+py2 < max_r2 and mask[x + y * w] > 0: # inside the influence elipsoid, with an influenced object
                    return (max(0, (light_center.x - light_xr) / w), min(1, (light_center.x + light_xr) / w),
                        max(  0, (light_center.y - light_yr) / h), min(1, (light_center.y + light_yr) / h))
        return None
    
    def check_min_render_size():
        w = context.scene.render.border_max_x - context.scene.render.border_min_x
        if int(w * context.scene.render.resolution_x) < 1:
            return False
        h = context.scene.render.border_max_y - context.scene.render.border_min_y
        if int(h * context.scene.render.resolution_y) < 1:
            return False
        return True
    
    # Apply a ligth scenario for rendering, returning the previous state and a lambda to apply it
    def setup_light_scenario(context, scenario, group_mask):
        if scenario[1] is None: # Base render (world lighting from Blender's World and World light groups)
            context.scene.render.use_border = False
            context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
            context.scene.render.image_settings.color_mode = 'RGBA'
            initial_state = (0, vlm_collections.move_all_to_col(scenario[2], tmp_col))
            if bake_info_group:
                bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 1.0
                bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 0.0
            return initial_state, lambda initial_state : restore_light_setup(initial_state)
        else: # Lightmap render (no world lighting)
            context.scene.render.use_border = True
            context.scene.world = bpy.data.worlds["VPX.Env.Black"]
            context.scene.render.image_settings.color_mode = 'RGB'
            if bake_info_group:
                bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 0.0
                bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 1.0
            if scenario[2] is None: # Group of lights
                influence = None
                for light in scenario[1].objects:
                    light_influence = get_light_influence(light, group_mask)
                    if light_influence:
                        if influence:
                            min_x, max_x, min_y, max_y = influence
                            min_x2, max_x2, min_y2, max_y2 = light_influence
                            influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_x, max_x2))
                        else:
                            influence = light_influence
                if not influence:
                    return None, None
                min_x, max_x, min_y, max_y = influence
                context.scene.render.border_min_x = min_x
                context.scene.render.border_max_x = max_x
                context.scene.render.border_min_y = 1-max_y
                context.scene.render.border_max_y = 1-min_y
                if not check_min_render_size():
                    print(f". light scenario '{scenario[0]}' has no render region, skipping (influence area: {influence})")
                    return None, None
                if vlm_utils.is_same_light_color(scenario[1].objects, 0.1):
                    prev_colors = [o.data.color for o in scenario[1].objects if o.type=='LIGHT']
                    for o in scenario[1].objects: o.data.color = (1.0, 1.0, 1.0)
                    initial_state = (3, scenario[1].objects, prev_colors, vlm_collections.move_all_to_col(scenario[1].all_objects, tmp_col))
                else:
                    print(f". light scenario '{scenario[0]}' contains lights with different colors or colored emitters. Lightmap will baked with these colors instead of full white.")
                    initial_state = (1, vlm_collections.move_all_to_col(scenario[1].all_objects, tmp_col))
            else: # Single light
                influence = get_light_influence(scenario[2], group_mask)
                if not influence:
                    return None, None
                min_x, max_x, min_y, max_y = influence
                context.scene.render.border_min_x = min_x
                context.scene.render.border_max_x = max_x
                context.scene.render.border_min_y = 1-max_y
                context.scene.render.border_max_y = 1-min_y
                if not check_min_render_size():
                    print(f". light scenario '{scenario[0]}' has no render region, skipping (influence area: {influence})")
                    return None, None
                if scenario[2].type == 'LIGHT':
                    prev_color = scenario[2].data.color
                    scenario[2].data.color = (1.0, 1.0, 1.0)
                    initial_state = (4, scenario[2], prev_color, vlm_collections.move_to_col(scenario[2], tmp_col))
                else:
                    print(f". light scenario '{scenario[0]}' is a colored emitters. Lightmap will baked with these colors instead of full white.")
                    initial_state = (2, vlm_collections.move_to_col(scenario[2], tmp_col))
            return initial_state, lambda initial_state : restore_light_setup(initial_state)

    # Render overlay collection and save it, activate composer accordingly to overlay it on object group renders (z masked)
    overlays = [obj for obj in overlay_col.all_objects]
    if overlays:
        print(f"\nPreparing overlays for {n_lighting_situations} lighting situations")
        initial_collections = vlm_collections.move_all_to_col(overlays, tmp_col)
        context.scene.render.image_settings.use_zbuffer = True
        vlm_collections.find_layer_collection(rlc, overlay_col).indirect_only = False
        for i, (name, scenario) in enumerate(light_scenarios.items(), start=1):
            context.scene.render.filepath = f"{bakepath}{scenario[0]} - Overlays.exr"
            if opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath)):
                print(f". Rendering overlay ({len(overlays)} objects) for '{scenario[0]}' ({i}/{n_lighting_situations})")
                state, restore_func = setup_light_scenario(context, scenario, None)
                n_render_performed = n_render_performed + 1
                context.scene.render.image_settings.color_mode = 'RGBA' # Force alpha channel, even for lightmaps
                bpy.ops.render.render(write_still=True)
                restore_func(state)
        vlm_collections.restore_all_col_links(initial_collections)
        vlm_collections.find_layer_collection(rlc, overlay_col).indirect_only = True
        context.scene.render.image_settings.use_zbuffer = False
        
        # Prepare compositor to apply overlay for the upcoming renders
        context.scene.use_nodes = True
        nodes = context.scene.node_tree.nodes
        nodes.clear() # I did not find a way to switch the active composer output, so we clear it each time
        links = context.scene.node_tree.links
        # Create default overlay composer
        rl = nodes.new("CompositorNodeRLayers")
        rl.location.x = -400
        rl.location.y = 100

        il = nodes.new("CompositorNodeImage")
        # We need to load an image with a z layer to enable the outputs of the image node
        overlay = bpy.data.images.load(context.scene.render.filepath, check_existing=False)
        il.image = overlay
        il.name = 'OverlayImage'
        il.location.x = -400
        il.location.y = -400
        
        malpha = nodes.new("CompositorNodeMath")
        malpha.operation = 'MULTIPLY'
        malpha.location.x = 0
        malpha.location.y = -200
        links.new(rl.outputs[1], malpha.inputs[0])
        links.new(il.outputs[1], malpha.inputs[1])
        
        setalpha = nodes.new("CompositorNodeSetAlpha")
        setalpha.location.x = 300
        setalpha.location.y = -400
        links.new(il.outputs[0], setalpha.inputs[0])
        links.new(malpha.outputs[0], setalpha.inputs[1])
        
        zc = nodes.new("CompositorNodeZcombine")
        zc.use_alpha = True
        zc.location.x = 600
        links.new(rl.outputs[0], zc.inputs[0])
        links.new(rl.outputs[2], zc.inputs[1])
        links.new(setalpha.outputs[0], zc.inputs[2])
        links.new(il.outputs[2], zc.inputs[3])

        out = nodes.new("CompositorNodeComposite")
        out.name = 'OverlayComposite'
        out.location.x = 900
        links.new(zc.outputs[0], out.inputs[0])
        
        bpy.data.images.remove(overlay) 
    
    # Load the group masks to filter out the obviously non influenced scenarios
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')
    group_masks = []
    for i in range(n_render_groups):
        im = Image.open(bpy.path.abspath(f"{mask_path}Group {i}.png"))
        group_masks.append((im.size[0], im.size[1], im.tobytes("raw", "L")))

    print(f"\nRendering {n_render_groups} render groups for {n_lighting_situations} lighting situations")
    context.scene.use_nodes = False
    n_skipped = n_existing = 0
    n_total_render = n_render_groups * n_lighting_situations + n_render_performed
    for group_index, group_mask in enumerate(group_masks):
        objects = [obj for obj in root_bake_col.all_objects if obj.vlmSettings.render_group == group_index]
        n_objects = len(objects)
        initial_collections = vlm_collections.move_all_to_col(objects, tmp_col)
        for i, (name, scenario) in enumerate(light_scenarios.items(), start=1):
            context.scene.render.filepath = f"{bakepath}{scenario[0]} - Group {group_index}.exr"
            if opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath)):
                state, restore_func = setup_light_scenario(context, scenario, group_mask)
                if state:
                    print(f". {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%} Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations})")
                    n_render_performed = n_render_performed + 1
                    if overlays:
                        context.scene.use_nodes = True
                        overlay = bpy.data.images.load(f"{bakepath}{scenario[0]} - Overlays.exr", check_existing=False)
                        bpy.data.scenes["Scene"].node_tree.nodes["OverlayImage"].image = overlay
                    bpy.ops.render.render(write_still=True)
                    if overlays:
                        bpy.data.images.remove(overlay)
                        context.scene.use_nodes = False
                    restore_func(state)
                else:
                    print(f". {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%} Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations}) - Skipped (no influence)")
                    n_skipped += 1
            else:
                n_existing += 1
        vlm_collections.restore_all_col_links(initial_collections)

    context.scene.use_nodes = True
    context.scene.node_tree.nodes.clear()
    context.scene.use_nodes = False

    context.scene.render.use_border = render_border_state[0]
    context.scene.render.use_crop_to_border = render_border_state[1]
    context.scene.render.border_min_x = render_border_state[2]
    context.scene.render.border_max_x = render_border_state[3]
    context.scene.render.border_min_y = render_border_state[4]
    context.scene.render.border_max_y = render_border_state[5]

    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    vlm_utils.pop_color_grading(cg)
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.pop_state(col_state)
    length = time.time() - start_time
    print(f"\nRendering finished in a total time of {vlm_utils.format_time(length)}")
    if n_existing > 0: print(f". {n_existing:>3} renders were skipped since they were already existing")
    if n_skipped > 0: print(f". {n_skipped:>3} renders were skipped since objects were outside of lights influence")
    if n_render_performed > 0: print(f". {n_render_performed:>3} renders were computed ({vlm_utils.format_time(length/n_render_performed)} per render)")

    context.scene.vlmSettings.last_bake_step = 'renders'
    return {'FINISHED'}
    

def create_bake_meshes(op, context):
    """Create all bake meshes, building from the render groups and renders cached during the previous steps
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before creating bake meshes')
        return {'CANCELLED'}

    print("\nCreating all bake meshes")
    start_time = time.time()
    camera = bpy.data.objects['Bake Camera']
    vlmProps = context.scene.vlmSettings
    n_render_groups = get_n_render_groups(context)
    cursor_loc = context.scene.cursor.location
    context.scene.cursor.location = camera.location # Used for sorting faces by distance from view point

    # Purge unlinked datas to avoid wrong names
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    # Texture packing
    opt_padding = vlmProps.padding
    opt_tex_size = int(vlmProps.tex_size)
    opt_ar = vlmProps.render_aspect_ratio

    # Bake mesh generation settings
    opt_backface_limit_angle = vlmProps.remove_backface
    opt_optimize_mesh = True
    shell_size = global_scale * 0.1 # amount of extrustion for light map shell
    opt_lightmap_prune_res = 256 # resolution used in the algorithm for unlit face pruning

    # Packmap grouping
    opt_pack_margin = 0.05 # ratio that we admit to loose in resolution to optimize grouped texture size

    # Append core material (used to bake the packamp as well as preview it)
    if "VPX.Core.Mat.PackMap" not in bpy.data.materials:
        librarypath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VPXMeshes.blend")
        if not os.path.isfile(librarypath):
            self.report({'WARNING'},f"{librarypath} does not exist")
            return {'CANCELLED'}
        with bpy.data.libraries.load(librarypath, link=False) as (data_from, data_to):
            data_to.objects = data_from.objects
            data_to.materials = [name for name in data_from.materials if name == "VPX.Core.Mat.PackMap"]
            data_to.node_groups = data_from.node_groups
    
    # Create temp collection and setup for rendering (exclude all baked/indirect objects as indirect and temp col as render target)
    rlc = context.view_layer.layer_collection
    tmp_col = vlm_collections.get_collection('BAKETMP')
    indirect_col = vlm_collections.get_collection('INDIRECT')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    lights_col = vlm_collections.get_collection('LIGHTS')
    root_bake_col = vlm_collections.get_collection('BAKE')
    vlm_collections.find_layer_collection(rlc, vlm_collections.get_collection('HIDDEN')).exclude = True
    vlm_collections.find_layer_collection(rlc, vlm_collections.get_collection('TRASH')).exclude = True
    vlm_collections.find_layer_collection(rlc, result_col).exclude = False
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, indirect_col).exclude = False
    vlm_collections.find_layer_collection(rlc, indirect_col).indirect_only = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False
    vlm_collections.find_layer_collection(rlc, root_bake_col).exclude = False
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    for bake_col in root_bake_col.children:
        vlm_collections.find_layer_collection(rlc, bake_col).exclude = False
        vlm_collections.find_layer_collection(rlc, bake_col).indirect_only = True
        # FIXME hide from render all objects marked as such
        if bake_col.vlmSettings.bake_mode == 'movable':
            pass

    # Prepare the list of lighting situation with a packmap material per render group, and a merge group per light situation
    light_merge_groups = {}
    light_scenarios = get_lightings(context)
    for name, light_scenario in light_scenarios.items():
        light_merge_groups[name] = []
        mats = []
        packmat = bpy.data.materials["VPX.Core.Mat.PackMap"]
        is_lightmap = light_scenario[1] is not None
        for index in range(n_render_groups):
            mat = packmat.copy()
            mat.name = f"VPX.PM.{name}.RG{index}"
            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
            if is_lightmap:
                mat.blend_method = 'BLEND'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.blend_method = 'OPAQUE'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
            mats.append(mat)
        light_scenario[3] = mats

    bake_meshes = [] # one per bake group
    for bake_col in root_bake_col.children:
        bake_group_name = vlm_utils.strip_vlm(bake_col.name)
        baked_objects = [obj for obj in bake_col.objects]
        bake_mode = bake_col.vlmSettings.bake_mode # either 'default' / 'movable' / 'playfield' see vlm_commons
        
        # FIXME implement movable baking
        if bake_mode == 'movable':
            continue
    
        # Join all objects to build baked objects (converting to mesh, and preserving split normals)
        print(f"\nBuilding base bake target model for '{bake_col.name}' bake group")
        to_join = []
        for obj in baked_objects:
            if obj.type != 'MESH':
                print(f". Converting non mesh object {obj.name}")
                bpy.ops.object.select_all(action='DESELECT')
                tmp_col.objects.link(obj)
                obj.select_set(True)
                context.view_layer.objects.active = obj
                bpy.ops.object.duplicate(linked=False)
                bpy.ops.object.convert(target='MESH')
                bpy.ops.object.shade_smooth()
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.1))
                bpy.ops.object.mode_set(mode='OBJECT')
                tmp_col.objects.unlink(obj)
                obj = context.view_layer.objects.active
                vlm_utils.apply_split_normals(obj.data)
            else:
                bpy.ops.object.select_all(action='DESELECT')
                tmp_col.objects.link(obj)
                obj.select_set(True)
                context.view_layer.objects.active = obj
                bpy.ops.object.duplicate(linked=False)
                tmp_col.objects.unlink(obj)
                obj = context.view_layer.objects.active
            [col.objects.unlink(obj) for col in obj.users_collection]
            tmp_col.objects.link(obj)
            obj.data.transform(obj.matrix_basis)
            obj.matrix_basis = mathutils.Matrix()
            obj.data.materials.clear()
            for index in range(n_render_groups):
                obj.data.materials.append(light_scenarios['Environment'][3][index])
            for poly in obj.data.polygons:
                poly.material_index  = obj.vlmSettings.render_group
            if not obj.data.has_custom_normals:
                print(f". Warning '{name}' does not have split normals. Final mesh will be flat shaded.")
            to_join.append(obj)
        
        # Join the meshed into a single bake mesh (or skip if there is no mesh in this bake group)
        if not to_join:
            continue
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = to_join[0]
        for obj in to_join: obj.select_set(True)
        if len(to_join) > 1: bpy.ops.object.join()
        bake_target = context.view_layer.objects.active
        bake_mesh = bake_target.data
        bake_mesh.name = "VLM.Bake Target"
        print(f". Objects merged ({len(bake_mesh.vertices)} vertices, {len(bake_mesh.polygons)} faces)")
        
        # Remove backfacing faces
        if opt_backface_limit_angle < 90.0:
            n_faces = len(bake_target.data.polygons)
            remove_backfacing(context, bake_target, camera.location, opt_backface_limit_angle)
            print(f". {n_faces - len(bake_target.data.polygons)} backfacing faces removed (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")

        # Clean up and simplify mesh (except for playfield mesh)
        n_faces = len(bake_target.data.polygons)
        if bake_mode == 'playfield':
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold = 0.001 * global_scale)
            bpy.ops.mesh.delete_loose()
            bpy.ops.object.mode_set(mode='OBJECT')
        elif opt_optimize_mesh:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold = 0.001 * global_scale)
            bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.1))
            bpy.ops.mesh.delete_loose()
            bpy.ops.object.mode_set(mode='OBJECT')
        print(f". {n_faces - len(bake_target.data.polygons)} faces removed (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")

        # Compute base UV Map projection matching the render
        for uvl in bake_mesh.uv_layers:
            bake_mesh.uv_layers.remove(uvl)
        bake_mesh.uv_layers.new(name="UVMap")
        area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
        area.regions[-1].data.view_perspective = 'CAMERA'
        override = {"area": area, "space_data": area.spaces.active, "region": area.regions[-1]}
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Triangulate (in the end, VPX only deals with triangles, and this simplify the lightmap pruning process)
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(bake_mesh)
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
        bmesh.update_edit_mesh(bake_mesh)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Subdivide long edges to avoid visible projection distortion, and allow better lightmap face pruning (recursive subdivisions)
        opt_cut_threshold = 0.02 if bake_mode == 'playfield' else 0.1 # 0.2 seems sufficient for distortion, lower value is needed for lightmap face pruning especially on playfield for inserts
        while True:
            bpy.ops.object.mode_set(mode='EDIT')
            bme = bmesh.from_edit_mesh(bake_mesh)
            long_edges = []
            longest_edge = 0
            for edge in bme.edges:
                if len(edge.verts[0].link_loops) < 1 or len(edge.verts[1].link_loops) < 1:
                    continue
                ua, va = edge.verts[0].link_loops[0][bme.loops.layers.uv.active].uv
                ub, vb = edge.verts[1].link_loops[0][bme.loops.layers.uv.active].uv
                l = math.sqrt((ub-ua)*(ub-ua)*opt_ar*opt_ar+(vb-va)*(vb-va))
                longest_edge = max(longest_edge, l)
                if l >= opt_cut_threshold: long_edges.append(edge)
            if not long_edges:
                bmesh.update_edit_mesh(bake_mesh)
                bpy.ops.object.mode_set(mode='OBJECT')
                break
            bmesh.ops.subdivide_edges(bme, edges=long_edges, cuts=1, use_grid_fill=True)
            bmesh.ops.triangulate(bme, faces=bme.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
            bmesh.update_edit_mesh(bake_mesh)
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.project_from_view(override)
            bpy.ops.object.mode_set(mode='OBJECT')
            print(f". {len(long_edges):>5} edges subdivided to avoid projection distortion and better lightmap pruning (length threshold: {opt_cut_threshold}, longest edge: {longest_edge:4.2}).")
        
        # Separate big polys (out of there natural island) for better packing
        uv_layer_packed = bake_mesh.uv_layers.new(name="UVMap Packed")
        uv_layer_packed.active = True
        big_poly_index = 0
        for poly in bake_mesh.polygons:
            surface = []
            for loop_index in poly.loop_indices:
                u, v = uv_layer_packed.data[loop_index].uv
                u = 0.5 + 0.5 * (u - 0.5) # Account for aspect ratio change
                uv_layer_packed.data[loop_index].uv = (u, v)
                surface.append(mathutils.Vector((u, v, 0)))
            if len(surface) == 3:
                area = mathutils.geometry.area_tri(surface[0], surface[1], surface[2])
            elif len(surface) == 4:
                area = mathutils.geometry.area_tri(surface[0], surface[1], surface[2]) + mathutils.geometry.area_tri(surface[0], surface[2], surface[3])
            else:
                bm = bmesh.new()
                verts = []
                for v in surface:
                    verts.append(bm.verts.new(v))
                bm.verts.ensure_lookup_table()
                face = bm.faces.new(verts)
                bm.faces.ensure_lookup_table()
                area = face.calc_area()
                bm.free()
            if not math.isnan(area) and not math.isinf(area):
                if area >= 0.025:
                    big_poly_index = big_poly_index + 1
                    for loop_index in poly.loop_indices:
                        uv_layer_packed.data[loop_index].uv = (uv_layer_packed.data[loop_index].uv[0] + 1.1 * big_poly_index, uv_layer_packed.data[loop_index].uv[1])
        print(f". {big_poly_index} big sized poly separated for better UV island packing.")
        
        # Sort front to back faces if opaque, back to front for 'active', i.e. non opaque
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.sort_elements(type='CURSOR_DISTANCE', elements={'VERT', 'FACE'}, reverse=bake_col.vlmSettings.is_active_mat)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        print(f'. Base mesh has {len(bake_mesh.polygons)} faces and {len(bake_mesh.vertices)} vertices')
        bake_meshes.append((bake_col, bake_mesh))

    # Merge bake mesh accross bake groups for the light mesh, then extrude it along normal according to its shell factor, and compute its visibility map
    prunemap_width = int(opt_lightmap_prune_res * opt_ar)
    prunemap_height = opt_lightmap_prune_res
    light_mesh = bpy.data.meshes.new('LightMesh')
    bm = bmesh.new()
    for bake_col, bake_mesh in bake_meshes:
        bm.from_mesh(bake_mesh)
    coords = []
    for v in bm.verts:
        coords.append(v.co + v.normal * shell_size * min(v.calc_shell_factor(), 10.0))
    for v, nv in zip(bm.verts, coords):
        v.co = nv
    bm.to_mesh(light_mesh)
    bm.free()
    for index in range(n_render_groups):
        light_mesh.materials.append(light_scenarios['Environment'][3][index])
    obj = bpy.data.objects.new(f"LightMesh", light_mesh)
    tmp_col.objects.link(obj)
    bpy.ops.object.select_all(action='DESELECT')
    context.view_layer.objects.active = obj
    obj.select_set(True)
    lightmap_vmap = build_visibility_map(light_mesh, n_render_groups, prunemap_width, prunemap_height)
    tmp_col.objects.unlink(obj)

    bake_results = []
    print(f'\nOptimizing lightmap meshes (prune map size={prunemap_width}x{prunemap_height})')
    for i, (name, light_scenario) in enumerate(light_scenarios.items(), start = 1):
        is_lightmap = light_scenario[1] is not None
        if is_lightmap:
            bake_instance = bpy.data.objects.get(f'LM.{name}')
            if bake_instance:
                vlm_collections.unlink(bake_instance)
                bake_instance.data = light_mesh.copy()
            else:
                bake_instance = bpy.data.objects.new(f'LM.{name}', light_mesh.copy())
            for index in range(n_render_groups):
                bake_instance.data.materials[index] = light_scenario[3][index]
            result_col.objects.link(bake_instance)
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bake_instance
            bake_instance.select_set(True)
            hdr_range = prune_lightmap_by_visibility_map(bake_instance.data, vlm_utils.get_bakepath(context, type='RENDERS'), name, n_render_groups, lightmap_vmap, prunemap_width, prunemap_height)
            if not bake_instance.data.polygons:
                result_col.objects.unlink(bake_instance)
                print(f". Mesh {name} has no more faces after optimization")
                continue
            bake_instance.vlmSettings.bake_hdr_scale = hdr_range
            bake_instance.vlmSettings.bake_name = name
            bake_instance.vlmSettings.bake_type = 'lightmap'
            bake_instance.vlmSettings.bake_light = light_scenario[2].name if light_scenario[2] is not None else light_scenario[1].name
            bake_instance.vlmSettings.bake_tex_factor = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap"])
            bake_results.append(bake_instance)
        else:
            for bake_col, bake_mesh in bake_meshes:
                bake_group_name = vlm_utils.strip_vlm(bake_col.name)
                bake_instance = bpy.data.objects.get(f'BM.{bake_group_name}')
                if bake_instance:
                    vlm_collections.unlink(bake_instance)
                    bake_instance.data = light_mesh.copy()
                else:
                    bake_instance = bpy.data.objects.new(f'BM.{bake_group_name}', bake_mesh.copy())
                for index in range(n_render_groups):
                    bake_instance.data.materials[index] = light_scenario[3][index]
                bake_instance.vlmSettings.bake_name = name
                bake_instance.vlmSettings.bake_objects = bake_group_name
                if bake_col.vlmSettings.bake_mode == 'playfield':
                    uv_layer_packed = bake_instance.data.uv_layers["UVMap Packed"]
                    uv_layer_packed.active = True
                    l, t, w, h = context.scene.vlmSettings.playfield_size
                    for loop in bake_instance.data.loops:
                        pt = bake_instance.data.vertices[loop.vertex_index].co
                        uv_layer_packed.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
                    bake_instance.vlmSettings.bake_tex_factor = 1
                    bake_instance.vlmSettings.bake_type = 'playfield'
                    bake_instance.vlmSettings.bake_light = ''
                elif bake_col.vlmSettings.is_active_mat:
                    bake_instance.vlmSettings.bake_type = 'active'
                    bake_instance.vlmSettings.bake_light = ''
                    bake_instance.vlmSettings.bake_tex_factor = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap"])
                elif bake_col.vlmSettings.bake_mode == 'movable':
                    bake_instance.vlmSettings.bake_type = 'default'
                    bake_instance.vlmSettings.bake_light = ''
                    bake_instance.vlmSettings.bake_tex_factor = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap"])
                else:
                    bake_instance.vlmSettings.bake_type = 'static'
                    bake_instance.vlmSettings.bake_light = ''
                    bake_instance.vlmSettings.bake_tex_factor = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap"])
                result_col.objects.link(bake_instance)
                bake_results.append(bake_instance)

    # Final view setup (hide bake groups and lights to preview the result)
    context.scene.cursor.location = cursor_loc
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.find_layer_collection(rlc, result_col).exclude = False
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, root_bake_col).exclude = True

    # Sort from higher texture fill factor to lowest, then fillup packmap buckets
    print(f"\nMerging and packing UV maps")
    bake_results.sort(key=lambda obj: obj.vlmSettings.bake_tex_factor, reverse=True)
    packmaps = []
    lattice = bpy.data.objects.get('Layback')
    for bake in bake_results:
        if lattice: bake.modifiers.new('Layback', 'LATTICE').object = lattice
        bake_density = bake.vlmSettings.bake_tex_factor
        if bake.vlmSettings.bake_type == 'playfield': # Playfield projection, no packing
            bake.vlmSettings.bake_packmap = len(packmaps)
            packmaps.append(([bake], 1, int(opt_tex_size/2), opt_tex_size, True))
            bake.vlmSettings.bake_packmap_width = int(opt_tex_size/2)
            bake.vlmSettings.bake_packmap_height = opt_tex_size
        else:
            for index, (bakes, density, _, _, is_playfield) in enumerate(packmaps):
                if not is_playfield and density + bake_density <= 1:
                    bake.vlmSettings.bake_packmap = index
                    packmaps[index] = (bakes + [bake], density + bake_density, -1, -1, is_playfield)
                    bake_density = 0
                    break
            if bake_density > 0:
                bake.vlmSettings.bake_packmap = len(packmaps)
                packmaps.append(([bake], bake_density, -1, -1, False))
    max_level = max(0, opt_tex_size.bit_length() - 1)
    for index, (bakes, density, w, h, is_playfield) in enumerate(packmaps):
        if not is_playfield:
            opt_n = 0
            for n in range(max_level, 0, -1):
                if (1.0 - opt_pack_margin) * density <= 1.0 / (1 << n):
                    opt_n = n
                    break
            h_n = int(opt_n / 2)
            w_n = opt_n - h_n
            tex_width = int(opt_tex_size / (1 << w_n))
            tex_height = int(opt_tex_size / (1 << h_n))
            packmaps[index] = (bakes, density, tex_width, tex_height, is_playfield)
            for bake in bakes:
                bake.vlmSettings.bake_packmap_width = tex_width
                bake.vlmSettings.bake_packmap_height = tex_height
                # the algorithm produce either square texture, or rectangle with w = h / 2 
                # which needs uv to be adapted to avoid texel density distorsion on the x axis
                if tex_width < tex_height:
                    uv_layer_packed = bake_mesh.uv_layers["UVMap Packed"]
                    uv_layer_packed.active = True
                    for poly in bake_mesh.polygons:
                        for loop_index in poly.loop_indices:
                            u, v = uv_layer_packed.data[loop_index].uv
                            u = 0.5 + 2.0 * (u - 0.5) # Account for aspect ratio change
                            uv_layer_packed.data[loop_index].uv = (u, v)

    print(f'. Bake/light maps merged into {len(packmaps)} packmaps:')
    for index, (bakes, density, w, h, is_playfield) in enumerate(packmaps):
        if is_playfield:
            print(f'.  Packmap #{index}: {w:>4}x{h:>4} playfield render')
        else:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bakes[0]
            for obj in bakes:
                obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')
            if vlmProps.uv_packer == 'blender':
                bpy.ops.uv.pack_islands(margin=opt_padding / opt_tex_size)
            elif vlmProps.uv_packer == 'uvpacker':
                vlm_uvpacker.uvpacker_pack(bakes, opt_padding, w, h)
            bpy.ops.object.mode_set(mode='OBJECT')
            packed_density = 0
            for obj in bakes:
                packed_density += compute_uvmap_density(obj.data, obj.data.uv_layers["UVMap Packed"])
            print(f'.  Packmap #{index}: {density:>6.2%} target density for {len(bakes)} objects => {w:>4}x{h:>4} texture size with {packed_density:>6.2%} effective packed density')

    # Purge unlinked datas
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f"\nbake meshes created in {str(datetime.timedelta(seconds=time.time() - start_time))}")

    context.scene.vlmSettings.last_bake_step = 'meshes'
    
    return {'FINISHED'}


# Debug code: call UV packer to repack and check the result
# For better quality, we need to avoid rotation (except 0/90/180/270) and scaling (too much elements on the same UV map)
def uvpack(context):
    result_col = vlm_collections.get_collection('BAKE RESULT')
    packmap_index = -1
    while True:
        packmap_index += 1
        bakes = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not bakes:
            break
        if bakes[0].vlmSettings.bake_type == 'playfield':
            continue
        w = bakes[0].vlmSettings.bake_packmap_width
        h = bakes[0].vlmSettings.bake_packmap_height
        opt_padding = 2
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = bakes[0]
        for obj in bakes:
            obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')
        # Reset UV
        area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
        area.regions[-1].data.view_perspective = 'CAMERA'
        override = {"area": area, "space_data": area.spaces.active, "region": area.regions[-1]}
        bpy.ops.uv.project_from_view(override)
        # Pack
        vlm_uvpacker.uvpacker_pack(bakes, opt_padding, w, h)
        bpy.ops.object.mode_set(mode='OBJECT')
        packed_density = 0
        for obj in bakes:
            packed_density += compute_uvmap_density(obj.data, obj.data.uv_layers["UVMap Packed"])
        
        # Check that we only moved / rotated 90 it
        mesh = bakes[0].data
        uv_layer = mesh.uv_layers["UVMap"]
        uv_layer_packed = mesh.uv_layers["UVMap Packed"]
        average = 0.0
        for i, poly in enumerate(mesh.polygons):
            min_u = min_v = min_up = min_vp = 0
            max_u = max_v = max_up = max_vp = 1
            for loop_index in poly.loop_indices:
                u, v = uv_layer.data[loop_index].uv
                up, vp = uv_layer_packed.data[loop_index].uv
                min_u = min(min_u, u)
                max_u = max(max_u, u)
                min_v = min(min_v, v)
                max_v = max(max_v, v)
                min_up = min(min_up, up)
                max_up = max(max_up, up)
                min_vp = min(min_vp, vp)
                max_vp = max(max_vp, vp)
            su = (max_up - min_up) / (max_u - min_u)
            sv = (max_vp - min_vp) / (max_v - min_v)
            print(f'Packmap #{packmap_index} density={packed_density:>6.2%} scale factor: {su}, {sv}')
            if i > 10:
                break


def orient2d(ax, ay, bx, by, x, y):
    """Evaluate on which side of a line a-b, a given point stand
    """
    return (bx-ax)*(y-ay) - (by-ay)*(x-ax)


def build_visibility_map(bake_instance_mesh, n_render_groups, width, height):
    """Build a set of rasterized maps where each pixels contains the list of 
    visible faces for the given render group.
    """
    bm = bmesh.new()
    bm.from_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    vmaps = [[] for xy in range(width * height)]
    bm.faces.ensure_lookup_table()
    for i, face in enumerate(bm.faces):
        if len(face.loops) != 3: # This should not happen
            continue
        a = face.loops[0][uv_layer].uv
        b = face.loops[1][uv_layer].uv
        c = face.loops[2][uv_layer].uv
        ax = int(a.x * width)
        ay = int(a.y * height)
        bx = int(b.x * width)
        by = int(b.y * height)
        cx = int(c.x * width)
        cy = int(c.y * height)
        min_x = max(0, min(ax, bx, cx))
        min_y = max(0, min(ay, by, cy))
        max_x = min(width - 1, max(ax, bx, cx))
        max_y = min(height - 1, max(ay, by, cy))
        A01 = ay - by
        B01 = bx - ax
        A12 = by - cy
        B12 = cx - bx
        A20 = cy - ay
        B20 = ax - cx
        w0_row = orient2d(bx, by, cx, cy, min_x, min_y)
        w1_row = orient2d(cx, cy, ax, ay, min_x, min_y)
        w2_row = orient2d(ax, ay, bx, by, min_x, min_y)
        #print(i, min_x, max_x, min_y, max_y, width, height, a, b, c)
        for y in range(min_y, max_y + 1):
            w0 = w0_row
            w1 = w1_row
            w2 = w2_row
            for x in range(min_x, max_x + 1):
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    vmaps[x + y * width].append(face.index)
                w0 += A12
                w1 += A20
                w2 += A01
            w0_row += B12
            w1_row += B20
            w2_row += B01
    bm.free()
    return vmaps

def prune_lightmap_by_visibility_map(bake_instance_mesh, render_path, name, n_render_groups, vmaps, w, h):
    """Prune faces based on their visibility in the precomputed visibility maps
    """
    vertex_shader = '''
        in vec2 position;
        in vec2 uv;
        out vec2 uvInterp;
        void main() {
            uvInterp = uv;
            gl_Position = vec4(position, 0.0, 1.0);
        }
    '''
    bw_fragment_shader = '''
        uniform sampler2D image;
        uniform float deltaU;
        uniform float deltaV;
        uniform int nx;
        uniform int ny;
        in vec2 uvInterp;
        out vec4 FragColor;
        void main() {
            vec4 t = vec4(0.0);
            for (int y=0; y<ny; y++) {
                for (int x=0; x<nx; x++) {
                    t = max(t, clamp(texture(image, uvInterp + vec2(x * deltaU, y * deltaV)).rgba, 0.0, 1.0));
                }
            }
            // vec4 t = clamp(texture(image, uvInterp).rgba, 0.0, 1.0);
            float v = t.a * dot(t.rgb, vec3(0.2989, 0.5870, 0.1140));
            FragColor = vec4(1.0, 1.0, 1.0, v);
        }
    '''
    # Rescale with a max filter, convert to black and white, apply alpha, in a single pass per image on the GPU
    offscreen = gpu.types.GPUOffScreen(w, h)
    hdr_range = 1.0
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)
        bw_shader.bind()
        gpu.state.blend_set('ALPHA')
        for i in range(n_render_groups):
            path_exr = f"{render_path}{name} - Group {i}.exr"
            if os.path.exists(bpy.path.abspath(path_exr)):
                image = bpy.data.images.load(path_exr, check_existing=False)
                im_width, im_height = image.size
                nx = int(im_width / w)
                ny = int(im_height / h)
                bw_shader.uniform_sampler("image", gpu.texture.from_image(image))
                bw_shader.uniform_float("deltaU", 1.0 / im_width)
                bw_shader.uniform_float("deltaV", 1.0 / im_height)
                bw_shader.uniform_int("nx", nx)
                bw_shader.uniform_int("ny", ny)
                batch_for_shader(
                    bw_shader, 'TRI_FAN',
                    {
                        "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                        #"uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
                        "uv": ((0, 0), (1 - nx/im_width, 0), (1 - nx/im_width, 1 - ny/im_height), (0, 1 - ny/im_height)),
                    },
                ).draw(bw_shader)
                image.scale(w, h)
                im_width, im_height = image.size
                pixel_data = np.zeros((im_width * im_height * 4), 'f') # use numpy since hdr_range = max(hdr_range, max(image.pixels)) adds 1 minute per 8k image (hours with all of them)
                image.pixels.foreach_get(pixel_data)
                pixel_data = np.minimum(pixel_data, 1000) # clamp out infinity values and excessively bright points (when a light is directly seen from the camera)
                hdr_range = np.amax(pixel_data, initial=hdr_range)
                bpy.data.images.remove(image)
        bw = gpu.state.active_framebuffer_get().read_color(0, 0, w, h, 4, 0, 'UBYTE')
        bw.dimensions = w * h * 4
    offscreen.free()

    if False: # For debug purpose, save generated influence map
        print(f'. Saving light influence map to {render_path}{name} - Influence Map.png')
        image = bpy.data.images.new("debug", w, h)
        image.pixels = [v / 255 for v in bw]
        image.filepath_raw = f'{render_path}{name} - Influence Map.png'
        image.file_format = 'PNG'
        image.save()
        bpy.data.images.remove(image)

    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    n_faces = len(bm.faces)
    for face in bm.faces:
        face.tag = False
    bm.faces.ensure_lookup_table()
    for xy in range(w * h):
        if bw[4 * xy] > 2:
            for face_index in vmaps[xy]:
                bm.faces[face_index].tag = True
    faces = []
    for face in bm.faces:
        if not face.tag:
            faces.append(face)
    if faces:
        bmesh.ops.delete(bm, geom=faces, context='FACES')
        print(f'. Mesh optimized to {n_faces - len(faces):>5} faces out of {n_faces} for {name:15} (HDR range: {hdr_range:>7.1f})')
    bmesh.update_edit_mesh(bake_instance_mesh)
    bpy.ops.object.mode_set(mode='OBJECT')
    return hdr_range


def render_packmaps_gpu(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation optimized for Blender's GPU module, that only support rotation of 0/90/180/270
    """
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True) # Purge unlinked datas to avoid out of memory error
    vlmProps = context.scene.vlmSettings
    opt_force_render = True # Force rendering even if cache is available
    opt_padding = int(vlmProps.padding)
    opt_tex_factor = float(vlmProps.packmap_tex_factor)
    opt_uv_padding = opt_padding * opt_tex_factor / int(vlmProps.tex_size)
    result_col = vlm_collections.get_collection('BAKE RESULT')
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    packmap_index = -1
    while True:
        packmap_index += 1
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break
        basepath = f"{bakepath}Packmap {packmap_index}"
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        if opt_force_render or not os.path.exists(path_png):
            tex_width = int(objects[0].vlmSettings.bake_packmap_width * opt_tex_factor)
            tex_height = int(objects[0].vlmSettings.bake_packmap_height * opt_tex_factor)
            vertex_shader = '''
                in vec2 pos;
                in vec2 uv;
                out vec2 uvInterp;
                void main() {
                    uvInterp = uv;
                    gl_Position = vec4(2.0 * (pos - vec2(0.5)), 0.0, 1.0);
                }
            '''
            fragment_shader = '''
                uniform sampler2D render;
                in vec2 uvInterp;
                out vec4 FragColor;
                uniform float brightness;
                void main() {
                    vec4 tex = texture(render, uvInterp).rgba;
                    vec3 col = clamp(pow(brightness * tex.rgb, vec3(1.0 / 2.2)), 0.0, 1.0);
                    FragColor = vec4(col, tex.a);
                }
            '''
            shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
            offscreen = gpu.types.GPUOffScreen(tex_width, tex_height)
            with offscreen.bind():
                fb = gpu.state.active_framebuffer_get()
                fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                shader.bind()
                gpu.state.blend_set('NONE') # Simple copy
                for obj in objects:
                    mesh = obj.data
                    n_materials = len(mesh.materials)
                    uv_layer = mesh.uv_layers["UVMap"]
                    uv_layer_packed = mesh.uv_layers["UVMap Packed"]
                    pts = [[] for i in range(n_materials)]
                    uvs = [[] for i in range(n_materials)]
                    for poly in mesh.polygons:
                        if len(poly.loop_indices) != 3:
                            print(f'Bug, {obj} has polygons which are not triangles...')
                            continue
                        poly_uvs = uvs[poly.material_index]
                        poly_pts = pts[poly.material_index]
                        start_pos = len(poly_uvs) - 1
                        for loop_index in poly.loop_indices:
                            poly_uvs.append(uv_layer.data[loop_index].uv)
                            poly_pts.append(uv_layer_packed.data[loop_index].uv)
                        if False: # Add padding
                            normals = []
                            normals_packed = []
                            n_indices = len(poly.loop_indices)
                            for i in range(n_indices):
                                ida = poly.loop_indices[i]
                                idb = poly.loop_indices[(i + 1) % n_indices]
                                normals.append((mathutils.Vector(uv_layer.data[idb].uv) - mathutils.Vector(uv_layer.data[ida].uv)).orthogonal().normalized())
                                normals_packed.append((mathutils.Vector(uv_layer_packed.data[idb].uv) - mathutils.Vector(uv_layer_packed.data[ida].uv)).orthogonal().normalized())
                            for i in range(n_indices):
                                j = (i - 1 + n_indices) % n_indices
                                n = opt_uv_padding * (normals[i] + normals[j])
                                np = opt_uv_padding * (normals_packed[i] + normals_packed[j])
                                u, v = poly_uvs[start_pos + i] 
                                poly_uvs[start_pos + i] = (u + n.x, v + n.y)
                                u, v = poly_pts[start_pos + i] 
                                poly_pts[start_pos + i] = (u + np.x, v + np.y)

                    if obj.vlmSettings.bake_type == 'lightmap':
                        brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale)
                    else:
                        brightness = 1.0
                    print(f'  . {obj.name:>15} => HDR Scale: {obj.vlmSettings.bake_hdr_scale:>7.2f} => Brightness factor: {brightness:>7.2f}')

                    for i,_ in enumerate(mesh.materials):
                        if pts[i]:
                            path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                            unload = vlm_utils.image_by_path(path) is None
                            render = bpy.data.images.load(path, check_existing=True)
                            shader.uniform_sampler("render", gpu.texture.from_image(render))
                            shader.uniform_float("brightness", brightness)
                            batch_for_shader(shader, 'TRIS', {"pos": pts[i], "uv": uvs[i]}).draw(shader)
                            if unload: bpy.data.images.remove(render)
                buffer = offscreen.texture_color.read()
                buffer.dimensions = tex_width * tex_height * 4
            offscreen.free()
            pack_image = bpy.data.images.new(f"PackMap{packmap_index}", tex_width, tex_height, alpha=True)
            pack_image.pixels = [v / 255 for v in buffer]
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)

        if opt_force_render or not os.path.exists(path_webp) or os.path.getmtime(path_webp) < os.path.getmtime(path_png):
            Image.open(path_png).save(path_webp, 'WEBP')
    
 
def render_packmaps_bake(op, context, sequential_baking):
    """Render all packmaps corresponding for the available current bake results.
    Implementation using Blender Cycle's builtin bake. This works perfectly but is rather slow.
    """
    opt_force_render = False # Force rendering even if cache is available
    opt_padding = context.scene.vlmSettings.padding
    
    # Purge unlinked datas to avoid out of memory error
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    cg = vlm_utils.push_color_grading(True)
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    result_col = vlm_collections.get_collection('BAKE RESULT')
    vlm_collections.find_layer_collection(rlc, result_col).exclude = False
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    packmap_index = 0
    render_state = (context.scene.cycles.samples, context.scene.cycles.use_denoising)
    context.scene.cycles.samples = 1
    context.scene.cycles.use_denoising = False
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break

        basepath = f"{bakepath}Packmap {packmap_index}"
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f'. Rendering packmap #{packmap_index} containing {len(objects)} bake/light map')
        
        if opt_force_render or not os.path.exists(path_png):
            tex_width = objects[0].vlmSettings.bake_packmap_width
            tex_height = objects[0].vlmSettings.bake_packmap_height
            pack_image = bpy.data.images.new(f"PackMap{packmap_index}", tex_width, tex_height, alpha=True)
            context.scene.render.bake.margin = opt_padding
            context.scene.render.bake.use_clear = True
            for obj in objects:
                bpy.ops.object.select_all(action='DESELECT')
                context.view_layer.objects.active = obj
                obj.select_set(True)
                is_light = obj.vlmSettings.bake_type == 'lightmap'
                n_materials = len(obj.data.materials)
                if obj.vlmSettings.bake_type == 'lightmap':
                    brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale)
                else:
                    brightness = 1.0
                print(f'  . {obj.name} => HDR Scale: {obj.vlmSettings.bake_hdr_scale:>7.2f} => Brightness factor: {brightness:>7.2f}')
                if sequential_baking: # Bake each render gorup separately. Slow but nneded by low memory system
                    for i in range(n_materials):
                        path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                        loaded, render = vlm_utils.get_image_or_black(path)
                        for j, mat in enumerate(obj.data.materials):
                            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
                            mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0 if is_light else 0.0 # Lightmap ?
                            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake
                            mat.node_tree.nodes["PackMap"].inputs[4].default_value = brightness # HDR scale
                            if i == j:
                                mat.node_tree.nodes["BakeTex"].image = render
                                mat.node_tree.nodes["PackMap"].inputs[5].default_value = 0.0 # Enabled
                            else:
                                mat.node_tree.nodes["PackMap"].inputs[5].default_value = 1.0 # Disabled
                            mat.node_tree.nodes["PackTex"].image = pack_image
                            mat.blend_method = 'OPAQUE'
                        bpy.ops.object.bake(type='COMBINED', pass_filter={'EMIT', 'DIRECT'}, margin=opt_padding)
                        context.scene.render.bake.use_clear = False
                        if loaded: bpy.data.images.remove(render)
                else: # Bake all render groups in a single pass. Needs to have all the hires texture in memory
                    unloads = []
                    for i, mat in enumerate(obj.data.materials):
                        path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                        loaded, render = vlm_utils.get_image_or_black(path)
                        if loaded == 'loaded': unloads.append(render)
                        mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
                        mat.node_tree.nodes["BakeTex"].image = render
                        mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0 if is_light else 0.0 # Lightmap ?
                        mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake
                        mat.node_tree.nodes["PackMap"].inputs[4].default_value = brightness # HDR scale
                        mat.node_tree.nodes["PackMap"].inputs[5].default_value = 0.0 # Enabled
                        mat.node_tree.nodes["PackTex"].image = pack_image
                        mat.blend_method = 'OPAQUE'
                    bpy.ops.object.bake(type='COMBINED', pass_filter={'EMIT', 'DIRECT'}, margin=opt_padding)
                    for render in unloads:
                        bpy.data.images.remove(render)
                    context.scene.render.bake.use_clear = False
                for mat in obj.data.materials:
                    mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview
                    mat.blend_method = 'BLEND' if is_light else 'OPAQUE'
            pack_image.filepath_raw = bpy.path.abspath(basepath + '.hdr')
            pack_image.file_format = 'HDR'
            pack_image.save()
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
            Image.open(path_png).save(path_webp, 'WEBP')

        packmap_index += 1

    context.scene.cycles.samples = render_state[0]
    context.scene.cycles.use_denoising = render_state[1]
    vlm_collections.pop_state(col_state)
    vlm_utils.pop_color_grading(cg)


def render_packmaps_eevee(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation using Eevee render. Works fine. No padding support for the time being
    """
    opt_force_render = False # Force rendering even if cache is available
    opt_padding = context.scene.vlmSettings.padding
    
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    tmp_col = vlm_collections.get_collection('BAKETMP')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    render_state = (context.scene.render.pixel_aspect_x, context.scene.render.pixel_aspect_y)
    context.scene.render.engine = 'BLENDER_EEVEE'
    context.scene.render.film_transparent = True
    context.scene.eevee.taa_render_samples = 1
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '16'
    context.scene.render.pixel_aspect_x = 1.0
    context.scene.render.pixel_aspect_y = 1.0
    cg = vlm_utils.push_color_grading(True)

    prev_camera = context.scene.camera
    camera = bpy.data.objects.new('Tmp.Camera', bpy.data.cameras.new(name='Camera'))
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = 1
    tmp_col.objects.link(camera)
    context.scene.camera = camera

    # Setup a ompositor for our needs
    nodes = context.scene.node_tree.nodes
    links = context.scene.node_tree.links
    nodes.clear()
    dy = 0 #1200
    rl = nodes.new("CompositorNodeRLayers")
    rl.location.x = -400
    rl.location.y = dy+ 100
    il = nodes.new("CompositorNodeImage")
    il.name = 'PackmapImage'
    il.location.x = -400
    il.location.y = dy-400
    malpha = nodes.new("CompositorNodeAlphaOver")
    malpha.location.x = 0
    malpha.location.y = dy-200
    links.new(rl.outputs[0], malpha.inputs[1])
    links.new(il.outputs[0], malpha.inputs[2])
    out = nodes.new("CompositorNodeComposite")
    out.name = 'PackmapComposite'
    out.location.x = 400
    out.location.y = dy
    links.new(malpha.outputs[0], out.inputs[0])
   
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    packmap_index = 0
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break

        # Purge unlinked datas to avoid out of memory error
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
        basepath = f"{bakepath}Packmap {packmap_index}"
        path_hdr = bpy.path.abspath(basepath + '.hdr')
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        pass_path = ''
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        
        if not opt_force_render and os.path.exists(path_png):
            packmap_index += 1
            continue
        
        tex_width = objects[0].vlmSettings.bake_packmap_width
        tex_height = objects[0].vlmSettings.bake_packmap_height
        # FIXME half texture output regarding the render size => this should be an option
        context.scene.render.resolution_x = tex_width / 2
        context.scene.render.resolution_y = tex_height / 2
        context.scene.use_nodes = False
        x_scale = tex_width / tex_height
        camera.location = (0.5 * x_scale, 0.5, 1.0)
        for obj_index, obj in enumerate(objects):
            # Create a mesh corresponding to the wanted UV projection
            verts = []
            faces = []
            uvs = []
            materials = []
            used_materials = {}
            uv_layer = obj.data.uv_layers["UVMap"]
            uv_layer_packed = obj.data.uv_layers["UVMap Packed"]
            for poly in obj.data.polygons:
                face = []
                used_materials[poly.material_index] = True
                materials.append(poly.material_index)
                for loop_index in poly.loop_indices:
                    u, v = uv_layer.data[loop_index].uv
                    x, y = uv_layer_packed.data[loop_index].uv
                    face.append(len(verts))
                    verts.append((x * x_scale, y, 0))
                    uvs.append((u, v))
                faces.append(face)
            mesh = bpy.data.meshes.new(f'Tmp.Mesh.{obj_index}')
            mesh.from_pydata(verts, [], faces)
            uv_layer = mesh.uv_layers.new()
            for i in range(len(mesh.loops)):
                uv_layer.data[i].uv = uvs[i]
            mesh.materials.clear()
            unloads = []
            mats = []
            brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale)
            print(f'. {obj.name} => HDR Scale: {obj.vlmSettings.bake_hdr_scale:>7.2f} => Brightness factor: {brightness:>7.2f}')
            for mat_index,_ in enumerate(obj.data.materials):
                path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {mat_index}.exr"
                loaded, render = vlm_utils.get_image_or_black(path)
                if loaded == 'loaded': unloads.append(render)
                mat = bpy.data.materials.new(f'Tmp.Pack.{obj_index}.{mat_index}')
                mat.blend_method = 'BLEND'
                mat.shadow_method = 'NONE'
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                nodes.clear()
                node_tex = nodes.new(type='ShaderNodeTexImage')
                node_tex.image = render
                node_premul = nodes.new("ShaderNodeMixRGB")
                node_emit = nodes.new("ShaderNodeEmission")
                node_emit.inputs[1].default_value = brightness
                node_transp = nodes.new("ShaderNodeHoldout")
                node_mix = nodes.new("ShaderNodeMixShader")
                node_output = nodes.new(type='ShaderNodeOutputMaterial')   
                node_premul.blend_type = 'MULTIPLY'
                node_premul.inputs[0].default_value = 1.0
                links.new(node_tex.outputs[0], node_premul.inputs[1])
                links.new(node_tex.outputs[1], node_premul.inputs[2])
                links.new(node_premul.outputs[0], node_emit.inputs[0])
                links.new(node_tex.outputs[1], node_mix.inputs[0])
                links.new(node_emit.outputs[0], node_mix.inputs[2])
                links.new(node_transp.outputs[0], node_mix.inputs[1])
                links.new(node_mix.outputs[0], node_output.inputs[0])
                mesh.materials.append(mat)
                mats.append(mat)
            pack_obj = bpy.data.objects.new(f'PM.{packmap_index}.{obj_index}', mesh)
            tmp_col.objects.link(pack_obj)
            for poly in mesh.polygons:
                poly.material_index = materials[poly.index]

            # Render it from an ortho projection with Eevee, and combine with previous render with the an AlphaOver in the compositor
            prev_pass_path = pass_path
            pass_path = bpy.path.abspath(f'{bakepath}Packmap {packmap_index} - Pass {obj_index}.exr')
            context.scene.render.filepath = pass_path
            bpy.ops.render.render(write_still=True)

            tmp_col.objects.unlink(pack_obj)
            for render in unloads:
                bpy.data.images.remove(render)
            for mat in mats:
                bpy.data.materials.remove(mat)
                
            # Prepare compositor to combine bake/light maps (we can not render all at once, since this will crash by out of memory on large renders)
            if prev_pass_path != '' and os.path.exists(prev_pass_path):
                bpy.data.images.remove(context.scene.node_tree.nodes['PackmapImage'].image)
                os.remove(prev_pass_path)
            context.scene.use_nodes = True
            context.scene.node_tree.nodes['PackmapImage'].image = bpy.data.images.load(pass_path)

        if pass_path != '' and os.path.exists(pass_path):
            img = bpy.data.images.load(pass_path, check_existing=True)
            img.filepath_raw = path_png
            img.file_format = 'PNG'
            img.save()
            #img.filepath_raw = path_hdr # It's not really usable so just skip it
            #img.file_format = 'HDR'
            #img.save()
            bpy.data.images.remove(img)
            Image.open(path_png).save(path_webp, 'WEBP')
            os.remove(pass_path)
        
        packmap_index += 1
        
    context.scene.render.pixel_aspect_x = render_state[0]
    context.scene.render.pixel_aspect_y = render_state[1]
    context.scene.camera = prev_camera
    tmp_col.objects.unlink(camera)
    context.scene.eevee.taa_render_samples = 64
    context.scene.render.engine = 'CYCLES'
    vlm_collections.pop_state(col_state)
    vlm_utils.pop_color_grading(cg)


def render_packmaps(op, context):
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering packmaps')
        return {'CANCELLED'}
    start_time = time.time()
    print(f'\nRendering packmaps (mode is {context.scene.vlmSettings.bake_packmap_mode})')
    if context.scene.vlmSettings.bake_packmap_mode == 'gpu':
        render_packmaps_gpu(context)
    elif context.scene.vlmSettings.bake_packmap_mode == 'eevee':
        render_packmaps_eevee(context)
    elif context.scene.vlmSettings.bake_packmap_mode == 'cycle_seq':
        render_packmaps_sequential_bake(op, context, True)
    elif context.scene.vlmSettings.bake_packmap_mode == 'cycle':
        render_packmaps_sequential_bake(op, context, False)
    print(f'\nPackmaps rendered in {vlm_utils.format_time(time.time() - start_time)}.')
    context.scene.vlmSettings.last_bake_step = 'packmaps'
    return {'FINISHED'}
