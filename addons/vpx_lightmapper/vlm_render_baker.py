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


def project_point(proj, p):
    p1 = proj @ Vector((p.x, p.y, p.z, 1)) # projected coordinates (range [-1, 1]x[-1, 1])
    return Vector(((1 + p1.x / p1.w) / 2, (1 - p1.y / p1.w) / 2)) # pixel coordinates (range [0, 1]x[0, 1])


def get_light_influence_radius(light):
    """Evaluate the radius of influence of the given object (light or emissive mesh)
    If evaluation fails, return (None, None) otherwise, returns (center, radius)
    Computed based on mesures on real renders, per radius, for 1/10/100/1000 energy
    """
    if not light.vlmSettings.enable_aoi:
        return (None, None)
    if light.type == 'LIGHT':
        light_aois = {
            0.01: [1.069, 2.019, 4.314, 10.00],
            0.05: [1.093, 2.139, 4.434, 10.00],
            0.10: [1.214, 2.223, 4.656, 10.00],
        }
        emission_strength = 1
        if light.data.use_nodes:
            for n in [n for n in light.data.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission']:
                if n.inputs['Strength'].is_linked:
                    return (None, None) # Strength is not a constant (Unsupported)
                emission_strength += n.inputs['Strength'].default_value
        if light.data.type == 'POINT' or light.data.type == 'SPOT':
            radius = light.data.shadow_soft_size
            emission = emission_strength * light.data.energy
            p = math.log10(emission)
            if p < 3:
                i = math.floor(p)
                a = p - i
                if radius <= 0.01:
                    r = 0.01
                elif radius <= 0.05:
                    r = 0.05
                else:
                    r = 0.10
                aoi_radius = (1-a)*light_aois[r][i] + a*light_aois[r][i+1]
                #print(f'L {light.name:>20} {radius} {r} {i} {a} => {aoi_radius}')
                return (light.matrix_world @ mathutils.Vector((0,0,0)), aoi_radius)
    elif light.type == 'MESH' or light.type == 'CURVE':
        mesh_aois = {
            0.01: [0.240, 0.252, 0.409, 0.625],
            0.05: [0.613, 1.202, 2.223, 4.530],
            0.10: [0.961, 1.790, 3.436, 6.140],
        }
        emission = radius = 0
        for mat in light.data.materials:
            for n in [n for n in mat.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission']:
                if n.inputs['Strength'].is_linked: 
                    return (None, None) # Strength is not a constant (Unsupported)
                emission += n.inputs['Strength'].default_value
        if emission > 0:
            p = math.log10(emission)
            if p < 3:
                i = math.floor(p)
                a = p - i
                center = mathutils.Vector((0, 0, 0))
                for corner in light.bound_box:
                    center = center + light.matrix_world @ mathutils.Vector(corner)
                center = center * (1.0 / len(light.bound_box))
                for corner in light.bound_box:
                    l = (light.matrix_world @ mathutils.Vector(corner) - center).length
                    radius = max(radius, l)
                if radius <= 0.01:
                    r = 0.01
                elif radius <= 0.05:
                    r = 0.05
                else:
                    r = 0.10
                aoi_radius = (1-a)*mesh_aois[r][i] + a*mesh_aois[r][i+1]
                #print(f'M {light.name:>20} {radius} {r} {i} {a} => {aoi_radius}')
                return (center, aoi_radius)
    return (None, None)


def get_light_influence(scene, depsgraph, camera, light, group_mask):
    """Compute area of influence of the given light
    If a group mask is provided, the AOI is filtered against it
    The implementation use a 2D ellipsoid influence bound computed by projecting a 3D sphere bound
    """
    if not group_mask:
        w = scene.render.resolution_x
        h = scene.render.resolution_y
        mask = None
    else:
        w, h, mask = group_mask
        
    center, radius = get_light_influence_radius(light)
    if center is None:
        return (0, 1, 0, 1)
    
    camera_rotation = mathutils.Quaternion(camera.rotation_quaternion)
    modelview_matrix = camera.matrix_world.inverted()
    projection_matrix = camera.calc_matrix_camera(
        depsgraph,
        x = scene.render.resolution_x,
        y = scene.render.resolution_y,
        scale_x = scene.render.pixel_aspect_x,
        scale_y = scene.render.pixel_aspect_y,
    )
    proj = projection_matrix @ modelview_matrix

    light_center = project_point(proj, center)
    light_xr = (project_point(proj, center + Vector((radius, 0, 0))) - light_center).x # projected radius on x axis
    light_yr = (project_point(proj, center + camera_rotation @ Vector((0, radius, 0))) - light_center).length # projected radius on y axis

    if light_xr <= 0 or light_yr <= 0:
        return None
    min_x = max(  0, int((light_center.x - light_xr) * (w-1)))
    max_x = min(w-1, int((light_center.x + light_xr) * (w-1)))
    min_y = max(  0, int((light_center.y - light_yr) * (h-1)))
    max_y = min(h-1, int((light_center.y + light_yr) * (h-1)))
    aoi = (max(0, light_center.x-light_xr), min(1, light_center.x+light_xr), max(0, light_center.y-light_yr), min(1, light_center.y+light_yr))
    # print(f'{light.name} {light.matrix_world} {light.location} {center} => {radius} / {light_center} {light_xr} {light_yr}')
    
    if not mask: # No mask, just return the bounds of the area of influence of the light
        return aoi
    light_center.x *= w - 1
    light_center.y *= h - 1
    alpha_y = light_yr / light_xr
    max_r2 = light_xr * (w-1) * light_xr * (w-1)
    for y in range(min_y, max_y + 1):
        py = (y - light_center.y) * alpha_y
        py2 = py * py
        for x in range(min_x, max_x + 1):
            px = x - light_center.x
            if px*px+py2 < max_r2 and mask[x + y * w] > 0: # inside the influence elipsoid, with an influenced object
                return aoi
    return None


def check_min_render_size(scene):
    w = scene.render.border_max_x - scene.render.border_min_x
    if int(w * scene.render.resolution_x) < 1:
        return False
    h = scene.render.border_max_y - scene.render.border_min_y
    if int(h * scene.render.resolution_y) < 1:
        return False
    return True


def setup_light_scenario(scene, depsgraph, camera, scenario, group_mask, render_col, bake_info_group):
    """Apply a light scenario for rendering, returning the previous state and a lambda to restore it
    """
    name, is_lightmap, light_col, lights, _ = scenario
    prev_world = scene.world
    if is_lightmap:
        scene.render.use_border = True
        scene.world = light_col.vlmSettings.world
        scene.render.image_settings.color_mode = 'RGB'
        if bake_info_group:
            bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 0.0
            bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 1.0
        if light_col.vlmSettings.world:
            scene.render.border_min_x = 0
            scene.render.border_max_x = 1
            scene.render.border_min_y = 0
            scene.render.border_max_y = 1
        else:
            influence = None
            for light in lights:
                light_influence = get_light_influence(scene, depsgraph, camera, light, group_mask)
                if light_influence:
                    if influence:
                        min_x, max_x, min_y, max_y = influence
                        min_x2, max_x2, min_y2, max_y2 = light_influence
                        influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                    else:
                        influence = light_influence
            if not influence:
                return None, None
            min_x, max_x, min_y, max_y = influence
            scene.render.border_min_x = min_x
            scene.render.border_max_x = max_x
            scene.render.border_min_y = 1 - max_y
            scene.render.border_max_y = 1 - min_y
            print(f". light scenario '{name}' influence area computed to: {influence}")
            if not check_min_render_size(scene):
                print(f". light scenario '{name}' has no render region, skipping (influence area: {influence})")
                return None, None
        if vlm_utils.is_rgb_led(lights):
            colored_lights = [o for o in lights if o.type=='LIGHT']
            prev_colors = [o.data.color for o in colored_lights]
            for o in colored_lights: o.data.color = (1.0, 1.0, 1.0)
            initial_state = (2, lights, colored_lights, prev_colors)
        else:
            initial_state = (1, lights)
    else:
        scene.render.use_border = False
        scene.world = light_col.vlmSettings.world
        scene.render.image_settings.color_mode = 'RGBA'
        if bake_info_group:
            bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 1.0
            bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 0.0
        initial_state = (0, lights)
    for light in lights:
        render_col.objects.link(light)
    return initial_state, lambda initial_state : restore_light_setup(initial_state, render_col, lights, scene, prev_world, bake_info_group)


def restore_light_setup(initial_state, render_col, lights, scene, prev_world, bake_info_group):
    """Restore state after setting up a light scenario for rendering
    """
    scene.world = prev_world
    for light in lights:
        render_col.objects.unlink(light)
    if bake_info_group:
        bake_info_group.nodes['IsBakeMap'].outputs["Value"].default_value = 0.0
        bake_info_group.nodes['IsLightMap'].outputs["Value"].default_value = 0.0
    if initial_state[0] == 2: # RGB led, restore colors
        for obj, color in zip(initial_state[2], initial_state[3]): obj.data.color = color

    
def render_all_groups(op, context):
    """Render all render groups for all lighting situations
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering')
        return {'CANCELLED'}

    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
    if not light_col:
        op.report({'ERROR'}, "No 'VLM.Lights' collection to process")
        return {'CANCELLED'}

    camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    if context.scene.use_nodes and 'VLM.Overlay' not in bpy.data.node_groups:
        context.scene.use_nodes = True
        vlm_utils.load_library()
        op.report({'ERROR'}, 'You must use the provided VLM.Overlay node group in your compositor setup')
        return {'CANCELLED'}

    if context.scene.use_nodes and not next((node for node in context.scene.node_tree.nodes if node.bl_idname == 'CompositorNodeGroup' and node.node_tree == bpy.data.node_groups['VLM.Overlay']), None):
        op.report({'ERROR'}, 'You must use the provided VLM.Overlay node group in your compostor setup')
        return {'CANCELLED'}

    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    opt_force_render = False # Force rendering even if cache is available
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio

    # Create temp render scene, using the user render settings and compositor setup if any
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    for prop in bpy.context.scene.render.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type'}:
            setattr(scene.render, prop.identifier, getattr(context.scene.render, prop.identifier))
    for prop in bpy.context.scene.cycles.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type'}:
            setattr(scene.cycles, prop.identifier, getattr(context.scene.cycles, prop.identifier))
    scene.render.engine = 'CYCLES'
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.resolution_y = opt_tex_size
    scene.render.resolution_x = int(opt_tex_size * render_aspect_ratio)
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression which is big
    #scene.render.image_settings.exr_codec = 'DWAA' # Lossy compression (4x to 10x smaller on lightmaps)
    scene.render.image_settings.color_depth = '16'
    scene.render.film_transparent = True
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    scene.view_layers[0].use_pass_z = False
    scene.use_nodes = True
    if context.scene.use_nodes:
        # FIXME
        scene.node_tree.nodes = context.scene.node_tree.nodes
        scene.node_tree.links = context.scene.node_tree.links
    else:
        nodes = scene.node_tree.nodes
        links = scene.node_tree.links
        nodes.clear()
        links.clear()
        rl = nodes.new("CompositorNodeRLayers")
        rl.scene = scene
        rl.location.x = -200
        group = nodes.new('CompositorNodeGroup')
        group.width = 300
        group.node_tree = bpy.data.node_groups['VLM.Overlay']
        out = nodes.new("CompositorNodeComposite")
        out.location.x = 200
        links.new(rl.outputs[0], group.inputs[0])
        links.new(rl.outputs[1], group.inputs[1])
        links.new(group.outputs[0], out.inputs[0])
    nodes = bpy.data.node_groups['VLM.Overlay'].nodes

    # Setup the scene with all the bake objects with indirect render influence
    indirect_col = bpy.data.collections.new('Indirect')
    render_col = bpy.data.collections.new('Render')
    scene.collection.children.link(indirect_col)
    scene.collection.children.link(render_col)
    vlm_collections.find_layer_collection(scene.view_layers[0].layer_collection, indirect_col).indirect_only = True
    for obj in bake_col.all_objects:
        indirect_col.objects.link(obj)
    
    n_render_groups = vlm_utils.get_n_render_groups(context)
    light_scenarios = vlm_utils.get_lightings(context)
    n_lighting_situations = len(light_scenarios)
    n_render_performed = n_skipped = n_existing = 0
    n_total_render = n_render_groups * n_lighting_situations
    bake_info_group = bpy.data.node_groups.get('VLM.BakeInfo')


    # FIXME this needs a full review and is unlikely to work (bake is not tied to the rgiht scene), 'Solid' scenario does not mean anything anymore
    for col in [col for col in bake_col.children if col.vlmSettings.bake_mode == 'playfield_fv']:
        print(f'\nBaking solid playfield with fixed view shading {col.name}')
        playfield_left, playfield_top, playfield_width, playfield_height = scene.vlmSettings.playfield_size
        playfield_right = playfield_width - playfield_left
        playfield_bottom = playfield_height - playfield_top
        pf_h = opt_tex_size
        pf_w = int(pf_h * playfield_width / playfield_height)
        pf_bake = bpy.data.images.new('Playfield.Bake.Image', pf_w, pf_h, alpha=True, float_buffer=True)
        pf_mat = bpy.data.materials.new('Playfield.Bake.Mat')
        pf_mat.use_nodes = True
        pf_mat.node_tree.nodes.clear()
        node_bake = pf_mat.node_tree.nodes.new(type='ShaderNodeTexImage')
        node_bake.image = pf_bake
        pf_mat.node_tree.nodes.active = node_bake
        pf_verts = [(playfield_left, -playfield_bottom, 0.0), (playfield_right, -playfield_bottom, 0.0), (playfield_left, -playfield_top, 0.0), (playfield_right, -playfield_top, 0.0)]
        pf_mesh = bpy.data.meshes.new("Playfield.Bake.Target")
        pf_mesh.from_pydata(pf_verts, [], [(0, 1, 3, 2)])
        pf_mesh.uv_layers.new()
        pf_mesh.materials.append(pf_mat)
        pf_obj = bpy.data.objects.new("Playfield.Bake.Temp", pf_mesh)
        render_col.objects.link(pf_obj)
        scene.render.bake.use_clear = True
        scene.render.bake.use_selected_to_active = True
        ax = scene.render.pixel_aspect_x
        scene.render.pixel_aspect_x = 1
        scene.render.bake.cage_extrusion = 10
        print(f'\nBaking {col.name} to playfield texture under environment lighting')
        n_total_render += 1
        vlm_collections.find_layer_collection(rlc, col).indirect_only = False
        bpy.ops.object.select_all(action='DESELECT')
        for obj in col.all_objects:
            obj.select_set(True)
        scene.view_layers[0].objects.active = pf_obj
        pf_obj.select_set(True)
        scenario = light_scenarios['Solid']
        path_exr = bpy.path.abspath(f'{bakepath}{scenario[0]} - {col.name}.exr')
        if opt_force_render or not os.path.exists(path_exr):
            state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, None, tmp_col, bake_info_group)
            if state:
                bpy.ops.object.bake(type='COMBINED', margin=0)
                pf_bake.save_render(path_exr)
                restore_func(state)
                n_render_performed += 1
            else:
                n_skipped += 1
        else:
            n_existing += 1
        vlm_collections.find_layer_collection(rlc, col).indirect_only = True
        scene.render.pixel_aspect_x = ax
        render_col.objects.unlink(pf_obj)
        bpy.data.images.remove(pf_bake)
        bpy.data.meshes.remove(pf_mesh)
        
    # Load the group masks to filter out the obviously non influenced scenarios
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')
    group_masks = []
    for i in range(n_render_groups):
        im = Image.open(bpy.path.abspath(f"{mask_path}Group {i}.png"))
        group_masks.append((im.size[0], im.size[1], im.tobytes("raw", "L")))

    print(f'\nRendering {n_render_groups} render groups for {n_lighting_situations} lighting situations')
    for group_index, group_mask in enumerate(group_masks):
        objects = [obj for obj in bake_col.all_objects if obj.vlmSettings.render_group == group_index]
        n_objects = len(objects)
        for obj in objects:
            indirect_col.objects.unlink(obj)
            render_col.objects.link(obj)
        loaded = mask = None
        for i, (name, scenario) in enumerate(light_scenarios.items(), start=1):
            render_path = f'{bakepath}{name} - Group {group_index}.exr'
            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)):
                state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, group_mask, render_col, bake_info_group)
                if state:
                    nodes['VLM.IsLightmap'].outputs[0].default_value = 1 if scenario[1] else 0
                    nodes['VLM.UseOverlay'].outputs[0].default_value = 0
                    nodes['VLM.AlphaMask'].image = None
                    print(f". {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%} Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations})")
                    scene.render.filepath = render_path
                    bpy.ops.render.render(write_still=True, scene=scene.name)
                    restore_func(state)
                    n_render_performed += 1
                else:
                    print(f". {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%} Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations}) - Skipped (no influence)")
                    n_skipped += 1
            else:
                n_existing += 1
        for obj in objects:
            render_col.objects.unlink(obj)
            indirect_col.objects.link(obj)
        if mask is not None and loaded == 'loaded':
            bpy.data.images.remove(mask)

    nodes['VLM.UseOverlay'].outputs[0].default_value = 0
    nodes['VLM.IsLightmap'].outputs[0].default_value = 0
    nodes['VLM.AlphaMask'].image = None
    bpy.data.scenes.remove(scene)

    length = time.time() - start_time
    print(f"\nRendering finished in a total time of {vlm_utils.format_time(length)}")
    if n_existing > 0: print(f". {n_existing:>3} renders were skipped since they were already existing")
    if n_skipped > 0: print(f". {n_skipped:>3} renders were skipped since objects were outside of lights influence")
    if n_render_performed > 0: print(f". {n_render_performed:>3} renders were computed ({vlm_utils.format_time(length/n_render_performed)} per render)")

    context.scene.vlmSettings.last_bake_step = 'renders'
    return {'FINISHED'}