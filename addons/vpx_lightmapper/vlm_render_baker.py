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
import re
import time
import gpu
import datetime
import numpy as np
from math import radians
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from .vlm_compat52 import (
    get_compositor_tree,
    set_compositing_enabled,
    set_eevee_engine,
    configure_file_output_node_52,
    configure_denoise_node_52,
    set_material_surface_render_method,
    set_diffuse_color_pass_enabled,
)
from . import vlm_collections
from PIL import Image # External dependency

logger = vlm_utils.logger


def project_point(proj, p):
    p1 = proj @ Vector((p.x, p.y, p.z, 1)) # projected coordinates (range [-1, 1]x[-1, 1])
    if p1.w<=0:
        return Vector((1,1))
    return Vector(((1 + p1.x / p1.w) / 2, (1 - p1.y / p1.w) / 2)) # pixel coordinates (range [0, 1]x[0, 1])


def get_light_influence_radius(light):
    """Evaluate the radius of influence of the given object (light or emissive mesh)
    If evaluation fails, return (None, None) otherwise, returns (center, radius)
    Computed based on mesures on real renders, per radius, for 1/10/100/1000 energy
    """
    if not light.vlmSettings.enable_aoi:
        return (None, None)
    if light.type == 'LIGHT':
        emission_strength = 0
        has_emission = False
        if light.data.use_nodes:
            for n in [n for n in light.data.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission']:
                if n.inputs['Strength'].is_linked:
                    return (None, None) # Strength is not a constant (Unsupported)
                has_emission = True
                emission_strength += n.inputs['Strength'].default_value
        if not has_emission: emission_strength = 1
        if light.data.type == 'POINT' or light.data.type == 'SPOT':
            radius = light.data.shadow_soft_size
            power = emission_strength * light.data.energy
            threshold = vlm_utils.get_lm_threshold()
            aoi_radius = math.sqrt(0.0254 * power / threshold)
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
                #logger.info(f'M {light.name:>20} {radius} {r} {i} {a} => {aoi_radius}')
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
    
    modelview_matrix = camera.matrix_world.inverted()
    projection_matrix = camera.calc_matrix_camera(
        depsgraph,
        x = scene.render.resolution_x,
        y = scene.render.resolution_y,
        scale_x = scene.render.pixel_aspect_x,
        scale_y = scene.render.pixel_aspect_y,
    )
    proj = projection_matrix @ modelview_matrix

    aoi = (
        max(0, project_point(proj, center + Vector((-radius, 0, 0))).x),
        min(1, project_point(proj, center + Vector(( radius, 0, 0))).x),
        max(0, project_point(proj, center + camera.rotation_quaternion @ Vector((0,  radius, 0))).y), 
        min(1, project_point(proj, center + camera.rotation_quaternion @ Vector((0, -radius, 0))).y))

    if aoi[1] <= aoi[0] or aoi[3] <= aoi[2]:
        return None
    
    if not mask: # No mask, just return the bounds of the area of influence of the light
        return aoi

    min_x = min(w-1, max(0, int(math.floor(aoi[0] * (w-1)))))
    max_x = min(w-1, max(0, int(math.ceil (aoi[1] * (w-1)))))
    min_y = min(h-1, max(0, int(math.floor(aoi[2] * (h-1)))))
    max_y = min(h-1, max(0, int(math.ceil (aoi[3] * (h-1)))))
    light_center = project_point(proj, center)
    light_center.x *= w - 1
    light_center.y *= h - 1
    alpha_y = (max_y - min_y) / (max_x - min_x)
    max_r2 = (max_x - min_x) * (max_x - min_x) / 4
    opt_min_x, opt_max_x, opt_min_y, opt_max_y = (w-1, 0, h-1, 0)
    influenced = False
    for y in range(min_y, max_y + 1):
        py = (y + 0.5 - light_center.y) * alpha_y
        py2 = py * py
        for x in range(min_x, max_x + 1):
            px = x + 0.5 - light_center.x
            if px*px+py2 < max_r2 and mask[x + y * w] > 0: # inside the influence elipsoid, with an influenced object
                influenced = True
                opt_min_x, opt_max_x, opt_min_y, opt_max_y = (min(x, opt_min_x), max(x, opt_max_x), min(y, opt_min_y), max(y, opt_max_y))
    if influenced and opt_min_x < opt_max_x and opt_min_y < opt_max_y:
        return (float(opt_min_x) / (w-1), float(opt_max_x) / (w-1), float(opt_min_y) / (h-1), float(opt_max_y) / (h-1))
    else:
        return None


def check_min_render_size(scene):
    w = scene.render.border_max_x - scene.render.border_min_x
    if int(w * scene.render.resolution_x) < 1:
        return False
    h = scene.render.border_max_y - scene.render.border_min_y
    if int(h * scene.render.resolution_y) < 1:
        return False
    return True


def setup_light_scenario(scene, depsgraph, camera, scenario, group_mask, render_col):
    """Apply a light scenario for rendering, returning the previous state and a lambda to restore it
    """
    name, is_lightmap, light_col, lights = scenario
    prev_world = scene.world
    if is_lightmap:
        scene.render.use_border = True
        scene.world = light_col.vlmSettings.world
        scene.render.image_settings.color_mode = 'RGB'
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
            logger.info(f". light scenario '{name}' influence area computed to: {influence}")
            if not check_min_render_size(scene):
                logger.info(f". light scenario '{name}' has no render region, skipping (influence area: {influence})")
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
        initial_state = (0, lights)
    for light in lights:
        render_col.objects.link(light)
    return initial_state, lambda initial_state : restore_light_setup(initial_state, render_col, lights, scene, prev_world)


def restore_light_setup(initial_state, render_col, lights, scene, prev_world):
    """Restore state after setting up a light scenario for rendering
    """
    scene.world = prev_world
    for light in lights:
        render_col.objects.unlink(light)
    if initial_state[0] == 2: # RGB led, restore colors
        for obj, color in zip(initial_state[2], initial_state[3]): obj.data.color = color

    
def render_all_groups(op, context):
    """Render all render groups for all lighting situations
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering')
        return {'CANCELLED'}

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
    if not light_col:
        op.report({'ERROR'}, "No 'VLM.Lights' collection to process")
        return {'CANCELLED'}

    camera_object = context.scene.camera
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)
    opt_render_width, opt_render_height = vlm_utils.get_render_size(context)
    opt_ar = opt_render_width / opt_render_height
    if context.scene.vlmSettings.max_lighting == 0:
        max_scenarios_in_batch = 1024
    else:
        max_scenarios_in_batch = int(context.scene.vlmSettings.max_lighting * 4096 / opt_render_height)
    opt_force_render = False # Force rendering even if cache is available
    n_render_groups = vlm_utils.get_n_render_groups(context)
    light_scenarios = vlm_utils.get_lightings(context)
    bake_info_group = bpy.data.node_groups.get('VLM.BakeInfo')

    denoise_prefilter = context.scene.vlmSettings.denoise_prefilter

    # Create temp render scene, using the user render settings setup
    scene = bpy.data.scenes.new('VLM.Tmp Render Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    for prop in context.scene.render.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type', ''}:
            setattr(scene.render, prop.identifier, getattr(context.scene.render, prop.identifier))
    for prop in context.scene.cycles.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type', 'denoiser', ''}:
            setattr(scene.cycles, prop.identifier, getattr(context.scene.cycles, prop.identifier))
    scene.render.engine = 'CYCLES'
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    scene.render.resolution_x = opt_render_width
    scene.render.resolution_y = opt_render_height
    scene.render.film_transparent = True
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    # Setup the scene with all the bake objects with indirect render influence.
    #
    # Blender stores collection render flags (including `indirect_only`) on the
    # ViewLayer's LayerCollection, not on the datablock Collection itself.  In
    # Blender 5.x a ViewLayer created together with a temporary Scene can retain
    # a stale LayerCollection hierarchy after collections are linked.  Build the
    # temporary collections first, then recreate the ViewLayer so its hierarchy
    # is guaranteed to contain them.
    indirect_col = bpy.data.collections.new('Indirect')
    render_col = bpy.data.collections.new('Render')
    scene.collection.children.link(indirect_col)
    scene.collection.children.link(render_col)

    # Build a fresh ViewLayer AFTER the temporary collections are linked.
    # Blender always requires at least one ViewLayer, so create the replacement
    # first and only then remove the automatically-created one.  This guarantees
    # that the new layer receives the current scene collection hierarchy.
    if len(scene.view_layers) == 0:
        render_view_layer = scene.view_layers.new(name='ViewLayer')
    else:
        old_view_layer = scene.view_layers[0]
        render_view_layer = scene.view_layers.new(name='VLM Render')
        scene.view_layers.remove(old_view_layer)

    render_view_layer.use_pass_z = False
    set_compositing_enabled(scene, False)

    indirect_lc = vlm_collections.get_layer_collection(scene, indirect_col)
    if indirect_lc is None:
        raise RuntimeError(
            "VPX Lightmapper: Blender 5.2 did not create a LayerCollection for "
            "the temporary 'Indirect' collection."
        )
    indirect_lc.indirect_only = True

    for obj in bake_col.all_objects:
        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.link(obj)
    
    # Load the group masks to filter out the obviously non influenced scenarios
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')
    group_masks = []
    for i in range(n_render_groups):
        im = Image.open(bpy.path.abspath(f'{mask_path}Mask - Group {i} (Padded LD).png'))
        group_masks.append((im.size[0], im.size[1], im.tobytes("raw", "L")))

    # Prepare and report stats
    n_lighting_situations = len(light_scenarios)
    n_render_performed = n_skipped = n_existing = 0
    n_bake_objects = len([obj for obj in bake_col.all_objects if obj.vlmSettings.use_bake])
    n_total_render = (n_render_groups + n_bake_objects) * n_lighting_situations + (n_bake_objects*2)
    logger.info(f'\nEvaluating {n_total_render} renders ({n_render_groups} render groups and {n_bake_objects} bakes for {n_lighting_situations} lighting situations, {n_bake_objects*2} normal & albedo maps)')
    
    # Perform the actual rendering of all the passes
    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 1.0
    for group_index, group_mask in enumerate(group_masks):
        is_already_done = True
        for scenario in light_scenarios:
            name, is_lightmap, light_col, lights = scenario
            render_path = f'{bakepath}{name} - Group {group_index}.exr'
            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)):
                is_already_done = False
                break
        if is_already_done:
            logger.info(f'. Skipping group {group_index} since all scenarios are already rendered and cached')
            n_existing += len(light_scenarios)
            continue

        objects = [obj for obj in bake_col.all_objects if obj.vlmSettings.render_group == group_index and not obj.vlmSettings.use_bake]
        n_objects = len(objects)
        for obj in objects:
            if not obj.vlmSettings.hide_from_others:
                indirect_col.objects.unlink(obj)
            if obj.vlmSettings.bake_mask:
                render_col.objects.link(obj.vlmSettings.bake_mask)
            render_col.objects.link(obj)

        #########
        # Blender 3.2+ batch light pass rendering
        #
        # In Blender 3.2, we can render multiple lights at once and save there data separately using light groups for way faster rendering.
        # This needs to use the compositor to performs denoising and save to split file outputs.
        logger.info(f'\n. Processing batch render for group #{group_index+1}/{n_render_groups}')
        if max_scenarios_in_batch <= 1: # do not use batch rendering if doing a single scenario since it would be less efficient due to the compositor denoising
            scenarios_to_process = None
        else:
            scenarios_to_process = []
            for scenario in light_scenarios:
                name, is_lightmap, light_col, lights = scenario
                if not is_lightmap or light_col.vlmSettings.world:
                    scenario_influence = (0, 1, 0, 1)
                else:
                    scenario_influence = None
                    for light in lights:
                        light_influence = get_light_influence(scene, context.view_layer.depsgraph, camera_object, light, group_mask)
                        if light_influence:
                            if scenario_influence:
                                min_x, max_x, min_y, max_y = scenario_influence
                                min_x2, max_x2, min_y2, max_y2 = light_influence
                                scenario_influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                            else:
                                scenario_influence = light_influence
                if scenario_influence:
                    scenarios_to_process.append((scenario, scenario_influence))
        while scenarios_to_process:
            prev_world = scene.world
            render_world = None
            n_scenarios = 0
            set_compositing_enabled(scene, True)
            render_view_layer.cycles.denoising_store_passes = True
            scene.render.use_file_extension = False

            tree = get_compositor_tree(scene, create=True, clear=True)
            nodes = tree.nodes
            links = tree.links
            nodes.clear()
            links.clear()
            rl = nodes.new("CompositorNodeRLayers")
            rl.scene = scene
            rl.location.x = -200
            dec = max_scenarios_in_batch / 2.0
            batch = []
            influence = None
            remaining_scenarios = []
            scenarios_to_select = scenarios_to_process.copy()
            def sortByInfluenceArea(x):
                scenario, scenario_influence = x
                min_x, max_x, min_y, max_y = scenario_influence
                return (max_x - min_x) * (max_y - min_y)
            scenarios_to_select.sort(key=sortByInfluenceArea) # Start with the smaller scenarios (to optimize render area size, since smaller have possiblities to be grouped together)
            while len(batch) < max_scenarios_in_batch and scenarios_to_select:
                scenario, scenario_influence = scenarios_to_select.pop(0)
                name, is_lightmap, light_col, lights = scenario
                # Light pass does not work with emitter meshes (consider the scenario as processed for the batch since it will be processed later)
                if next((l for l in lights if l.type != 'LIGHT'), None): 
                    continue
                # One world bake maximum per batch
                if light_col.vlmSettings.world != None: 
                    if render_world is None: 
                        render_world = light_col.vlmSettings.world
                        render_world.lightgroup = name
                    else:
                        remaining_scenarios.append(scenario)
                        continue
                # Do not re-render existing cached renders
                render_path = f'{bakepath}{name} - Group {group_index}.exr'
                if not opt_force_render and os.path.exists(bpy.path.abspath(render_path)):
                    logger.info(f'. Skipping scenario {name} for group {group_index} since it is already rendered and cached')
                    n_existing += 1
                    continue
                # Only render if the scenario influence the objects in the group
                if not scenario_influence:
                    #Reported during normal render
                    #logger.info(f'. Skipping scenario {name} since it is not influencing group {group_index}')
                    #n_skipped += 1
                    continue

                # Compute Overall scenario influence
                if influence:
                    min_x, max_x, min_y, max_y = influence
                    min_x2, max_x2, min_y2, max_y2 = scenario_influence
                    influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                else:
                    influence = scenario_influence

                # Append scenario to render scene and batch
                render_view_layer.lightgroups.add(name=name.replace(".","_"))
                initial_state = (0, None)
                if vlm_utils.is_rgb_led(lights):
                    colored_lights = [o for o in lights if o.type=='LIGHT']
                    prev_colors = [o.data.color for o in colored_lights]
                    for o in colored_lights: o.data.color = (1.0, 1.0, 1.0)
                    initial_state = (1, zip(colored_lights, prev_colors))
                for light in lights:
                    light.lightgroup = name.replace(".","_")
                    render_col.objects.link(light)
                denoise = nodes.new("CompositorNodeDenoise")
                configure_denoise_node_52(denoise, denoise_prefilter) if bpy.app.version >= (5, 0, 0) else setattr(denoise, 'prefilter', denoise_prefilter)
                denoise.location.x = 200
                denoise.location.y = -(i-dec) * 200
                links.new(rl.outputs['Denoising Normal'], denoise.inputs['Normal'])
                links.new(rl.outputs['Denoising Albedo'], denoise.inputs['Albedo'])
                out = nodes.new("CompositorNodeOutputFile")
                out.location.x = 600
                out.location.y = -(i-dec) * 200
                if bpy.app.version >= (5, 0, 0):
                    configure_file_output_node_52(out, socket_name='Image')
                if is_lightmap:
                    links.new(denoise.outputs['Image'], out.inputs[0])
                else:
                    alpha = nodes.new("CompositorNodeSetAlpha")
                    alpha.location.x = 400
                    alpha.location.y = -(i-dec) * 200
                    links.new(denoise.outputs['Image'], alpha.inputs['Image'])
                    links.new(rl.outputs['Alpha'], alpha.inputs['Alpha'])
                    links.new(alpha.outputs['Image'], out.inputs[0])
                batch.append((scenario, denoise, out, initial_state, scenario_influence))

                # Sort remaining scenarios to priorize the ones that will lead to the smaller render area, or if the result is the same area, choose the smallest ones
                def sortkey(x):
                    scenario, scenario_influence = x
                    min_x, max_x, min_y, max_y = influence
                    min_x2, max_x2, min_y2, max_y2 = scenario_influence
                    min_x3, max_x3, min_y3, max_y3 = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                    return (max_x3 - min_x3) * (max_y3 - min_y3) * 100 - (max_x2 - min_x2) * (max_y2 - min_y2)
                scenarios_to_select.sort(key=sortkey)

            remaining_scenarios.extend(scenarios_to_select)
            if not batch:
                scenarios_to_process = remaining_scenarios
                continue

            scene.world = render_world

            for scenario, denoise, out, initial_state, scenario_influence in batch:
                name, is_lightmap, light_col, lights = scenario
                links.new(rl.outputs[f'Combined_{name.replace(".","_")}'], denoise.inputs[0])
                if bpy.app.version >= (5, 0, 0):
                    configure_file_output_node_52(
                        out,
                        socket_name='Image',
                        directory=bakepath,
                        file_name=f'{name} - Group {group_index}.exr',
                        file_format='OPEN_EXR',
                        color_mode='RGB' if is_lightmap else 'RGBA',
                        color_depth='16',
                        exr_codec='ZIP',
                        use_file_extension=False,
                    )
                else:
                    out.base_path = f'{bakepath}'
                    out.file_slots[0].path = f'{name} - Group {group_index}.exr'
                    out.file_slots[0].use_node_format = True
                    out.format.file_format = 'OPEN_EXR'
                    out.format.color_mode = 'RGB' if is_lightmap else 'RGBA'
                    out.format.exr_codec = 'ZIP' # Lossless compression
                    out.format.color_depth = '16'
                logger.info(f'. Scenario {name} selected, render area: {scenario_influence}')
            
            elapsed = time.time() - start_time
            msg = f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for {len(batch)} lighting scenarios (render area: {(influence[1]-influence[0])*(influence[3]-influence[2]):5.2%} {influence}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
            if elapsed > 0 and n_render_performed > 0:
                elapsed_per_render = elapsed / n_render_performed
                remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
            logger.info(msg)

            # Setup AOI
            if influence != (0, 1, 0, 1):
                min_x, max_x, min_y, max_y = influence
                scene.render.use_border = True
                scene.render.border_min_x = min_x
                scene.render.border_max_x = max_x
                scene.render.border_min_y = 1 - max_y
                scene.render.border_max_y = 1 - min_y
            else:
                scene.render.use_border = False
            
            # Blender 5.x File Output always appends a frame number (and may also
            # include the socket name) to compositor output filenames. The old
            # implementation assumed the exact legacy pattern ``name.exr0001``
            # and therefore failed to recognize some valid Blender 5.2 outputs.
            # Snapshot the directory so only files produced/changed by this render
            # are considered; never rename or delete unrelated cached files.
            batch_output_dir = bpy.path.abspath(f'{bakepath}')
            batch_output_before = {}
            try:
                for entry in os.scandir(batch_output_dir):
                    if entry.is_file():
                        try:
                            stat = entry.stat()
                            batch_output_before[entry.name] = (stat.st_mtime_ns, stat.st_size)
                        except OSError:
                            pass
            except OSError:
                batch_output_before = {}

            bpy.ops.render.render(write_still=False, scene=scene.name)

            # Collect files that were created or modified by this compositor render.
            batch_output_after = {}
            try:
                for entry in os.scandir(batch_output_dir):
                    if entry.is_file():
                        try:
                            stat = entry.stat()
                            batch_output_after[entry.name] = (stat.st_mtime_ns, stat.st_size)
                        except OSError:
                            pass
            except OSError:
                batch_output_after = {}

            batch_output_changed = {
                name for name, state in batch_output_after.items()
                if name not in batch_output_before or batch_output_before[name] != state
            }

            # Resolve each compositor output back to the stable VLM cache path.
            # Blender 5.2 can produce variants such as:
            #   <file_name>0001.exr
            #   <file_name>Image0001.exr
            #   <file_name>0001
            # depending on File Output node settings.
            for scenario, denoise, out, initial_state, scenario_influence in batch:
                name = scenario[0]
                target_name = f'{name} - Group {group_index}.exr'
                target_path = os.path.join(batch_output_dir, target_name)
                if os.path.isfile(target_path):
                    continue

                node_file_name = os.path.basename(getattr(out, 'file_name', '') or target_name)
                node_stem = os.path.splitext(node_file_name)[0]
                target_stem = os.path.splitext(target_name)[0]

                candidates = []
                for filename in batch_output_changed:
                    lower = filename.lower()
                    # File Output can omit the extension when
                    # use_file_extension=False, so accept extensionless files too.
                    # Reject clearly unrelated image formats.
                    if lower.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')):
                        continue
                    if filename == target_name:
                        continue
                    # Prefer the exact node filename prefix, then the stable VLM
                    # target prefix without extension.
                    score = None
                    if filename.startswith(node_file_name):
                        score = 0
                    elif filename.startswith(node_stem):
                        score = 1
                    elif filename.startswith(target_name):
                        score = 2
                    elif filename.startswith(target_stem):
                        score = 3
                    if score is not None:
                        candidates.append((score, filename))

                if candidates:
                    candidates.sort(key=lambda item: (item[0], len(item[1])))
                    source_name = candidates[0][1]
                    source_path = os.path.join(batch_output_dir, source_name)
                    try:
                        if os.path.isfile(target_path):
                            os.remove(target_path)
                        os.replace(source_path, target_path)
                        logger.info(f'. Batch compositor output normalized: {source_name} -> {target_name}')
                    except OSError as output_error:
                        logger.warning(f'. Could not normalize batch compositor output {source_name} -> {target_name}: {output_error}')
                else:
                    logger.warning(
                        f'. Batch compositor output for {target_name} was not found. '
                        f'Changed files: {sorted(batch_output_changed)}'
                    )

            # Count only batch renders that actually produced their expected cache
            # files. If an output is missing, the normal rendering pass below will
            # retry that scenario instead of falsely marking it as complete.
            batch_completed = 0
            for scenario, denoise, out, initial_state, scenario_influence in batch:
                target_path = bpy.path.abspath(f'{bakepath}{scenario[0]} - Group {group_index}.exr')
                if os.path.isfile(target_path):
                    batch_completed += 1
            n_render_performed += batch_completed

            for scenario, denoise, out, initial_state, scenario_influence in batch:
                name, is_lightmap, light_col, lights = scenario
                for light in lights:
                    render_col.objects.unlink(light)
                if initial_state[0] == 1:
                    for o, c in initial_state[1]: o.data.color = c
                with bpy.context.temp_override(scene=scene):
                    bpy.ops.scene.view_layer_remove_lightgroup()
            nodes.clear()
            links.clear()
            set_compositing_enabled(scene, False)
            scene.world = prev_world
            render_view_layer.cycles.denoising_store_passes = False
            scene.render.use_border = False
        
            scenarios_to_process = remaining_scenarios
    
        #########
        # Default rendering
        #
        # Light pass batch rendering does not support emitter mesh, so we use the legacy per light scenario rendering to process them
        for i, scenario in enumerate(light_scenarios, start=1):
            name, is_lightmap, light_col, lights = scenario
            render_path = f'{bakepath}{scenario[0]} - Group {group_index}.exr'
            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)):
                state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, group_mask, render_col)
                elapsed = time.time() - start_time
                msg = f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
                if elapsed > 0 and n_render_performed > 0:
                    elapsed_per_render = elapsed / n_render_performed
                    remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                    msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
                if state:
                    logger.info(msg)
                    scene.render.filepath = render_path
                    scene.render.image_settings.file_format = 'OPEN_EXR'
                    scene.render.image_settings.color_mode = 'RGB' if is_lightmap else 'RGBA'
                    scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression
                    scene.render.image_settings.color_depth = '16'
                    bpy.ops.render.render(write_still=True, scene=scene.name)
                    restore_func(state)
                    logger.info('\n')
                    n_render_performed += 1
                else:
                    logger.info(f'{msg} - Skipped (no influence)')
                    n_skipped += 1

        for obj in objects:
            if not obj.vlmSettings.hide_from_others:
                indirect_col.objects.link(obj)
            if obj.vlmSettings.bake_mask:
                render_col.objects.unlink(obj.vlmSettings.bake_mask)
            render_col.objects.unlink(obj)

    #########
    # Traditional UV unwrapped baking
    #
    # Baking using rendering and projective texture gives (surprisingly) good results in most situations but it will look wrong for some
    # objects that will need traditional baking. This is especially true for movable parts like spinners, flipper bats,...
    # These objects will be processed with traditional bake which requires them to be UV unwrapped

    # Create temp render scene for rendering object masks & influence map & denoising

    temp_denoise_scene = bpy.data.scenes.new(name="VLM.Tmp Denoise Scene")
    set_compositing_enabled(temp_denoise_scene, True)
    denoise_nodetree = get_compositor_tree(temp_denoise_scene, create=True, clear=True)
    denoise_nodes = denoise_nodetree.nodes
    denoise_links = denoise_nodetree.links
    denoise_nodes.clear()
    denoise_image_node = denoise_nodes.new(type="CompositorNodeImage")
    denoise_image_node.location = (0, 0)
    denoise_normal_map_node = denoise_nodes.new(type="CompositorNodeImage")
    denoise_normal_map_node.location = (0, 300)
    denoise_albedo_map_node = denoise_nodes.new(type="CompositorNodeImage")
    denoise_albedo_map_node.location = (0, 600)
    denoise_node = denoise_nodes.new(type="CompositorNodeDenoise")
    configure_denoise_node_52(denoise_node, denoise_prefilter) if bpy.app.version >= (5, 0, 0) else setattr(denoise_node, 'prefilter', denoise_prefilter)
    denoise_node.location = (300, 0)
    denoise_viewer_node = denoise_nodes.new(type="CompositorNodeViewer")
    denoise_viewer_node.location = (600, 0)
    denoise_file_output_node = denoise_nodes.new(type="CompositorNodeOutputFile")
    denoise_file_output_node.location = (600, -300)
    if bpy.app.version >= (5, 0, 0):
        configure_file_output_node_52(
            denoise_file_output_node,
            socket_name='Image',
            directory='',
            file_name='',
            file_format='OPEN_EXR',
            color_mode='RGBA',
            color_depth='16',
            exr_codec='ZIP',
            use_file_extension=False,
        )
    else:
        denoise_file_output_node.base_path = bpy.path.abspath("")
        denoise_file_output_node.format.file_format = 'OPEN_EXR'
        denoise_file_output_node.format.exr_codec = 'ZIP'
        denoise_file_output_node.format.color_depth = '16'
    denoise_links.new(denoise_image_node.outputs['Image'], denoise_node.inputs['Image'])
    denoise_links.new(denoise_node.outputs['Image'], denoise_viewer_node.inputs['Image'])
    denoise_links.new(denoise_node.outputs['Image'], denoise_file_output_node.inputs[0])
    denoise_links.new(denoise_normal_map_node.outputs['Image'], denoise_node.inputs['Normal'])
    denoise_links.new(denoise_albedo_map_node.outputs['Image'], denoise_node.inputs['Albedo'])
    

    opt_mask_size = 1024 # Height used for the object masks
    opt_mask_pad = math.ceil(opt_mask_size * 2 / opt_render_height)
    mask_scene = bpy.data.scenes.new('VLM.Tmp Mask Scene')
    mask_scene.collection.objects.link(camera_object)
    mask_scene.camera = camera_object
    set_eevee_engine(mask_scene)
    mask_scene.render.film_transparent = True
    mask_scene.render.pixel_aspect_x = context.scene.render.pixel_aspect_x
    mask_scene.render.image_settings.color_depth = '8'
    if hasattr(mask_scene.eevee, 'taa_render_samples'):
        mask_scene.eevee.taa_render_samples = 1
    mask_scene.view_settings.view_transform = 'Raw'
    mask_scene.view_settings.look = 'None'
    mask_scene.world = None
    set_compositing_enabled(mask_scene, False)

    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 2.0
    for obj in [obj for obj in bake_col.all_objects if obj.vlmSettings.use_bake]:
        if obj.vlmSettings.bake_mask:
            render_col.objects.link(obj.vlmSettings.bake_mask)
        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.unlink(obj)
        
        # Create a duplicate and apply modifiers since they can generate/modify the UV map
        dup = obj.copy()
        dup.data = dup.data.copy()
        
        

        render_col.objects.link(dup)
        with context.temp_override(active_object=dup, selected_objects=[dup]):
            for modifier in dup.modifiers:
                if modifier.show_render:
                    try:
                        bpy.ops.object.modifier_apply(modifier=modifier.name)
                    except:
                        logger.info(f'. ERROR {obj.name} has an invalid modifier which was not applied')
            dup.modifiers.clear()
        
        elapsed = time.time() - start_time
        
        # Render object mask (or load from cache if available)
        mask_scene.render.resolution_y = opt_mask_size
        mask_scene.render.resolution_x = int(opt_mask_size * opt_ar)
        mask_scene.render.image_settings.file_format = "PNG"
        mask_scene.render.image_settings.color_mode = 'RGBA'
        mask_scene.render.filepath = f'{mask_path}{vlm_utils.clean_filename(obj.name)}.png'
        need_render = not os.path.exists(bpy.path.abspath(mask_scene.render.filepath))
        if not need_render:
            im = Image.open(bpy.path.abspath(mask_scene.render.filepath))
            need_render = im.size[0] != mask_scene.render.resolution_x or im.size[1] != mask_scene.render.resolution_y
        if need_render:
            mask_scene.collection.objects.link(dup)
            bpy.ops.render.render(write_still=True, scene=mask_scene.name)
            mask_scene.collection.objects.unlink(dup)
            im = Image.open(bpy.path.abspath(mask_scene.render.filepath))
        for p in range(opt_mask_pad):
            im.alpha_composite(im, (0, 1))
            im.alpha_composite(im, (0, -1))
            im.alpha_composite(im, (1, 0))
            im.alpha_composite(im, (-1, 0))
        obj_mask = (im.size[0], im.size[1], im.tobytes("raw", "A"))
        
        # Bake object for each lighting scenario
        render_ratio = context.scene.vlmSettings.render_ratio / 100.0
        bake_img = bpy.data.images.get('Bake')
        if not bake_img:
            bake_img = bpy.data.images.new('Bake', int(dup.vlmSettings.bake_width * render_ratio), int(dup.vlmSettings.bake_height * render_ratio), alpha=True, float_buffer=True)
        bake_img_albedo = bpy.data.images.get('Bake_Albedo')
        if not bake_img_albedo:
            bake_img_albedo = bpy.data.images.new('Bake_Albedo', int(dup.vlmSettings.bake_width * render_ratio), int(dup.vlmSettings.bake_height * render_ratio), alpha=True, float_buffer=True)
        bake_img_normal = bpy.data.images.get('Bake_Normal')
        if not bake_img_normal:
            bake_img_normal = bpy.data.images.new('Bake_Normal', int(dup.vlmSettings.bake_width * render_ratio), int(dup.vlmSettings.bake_height * render_ratio), alpha=True, float_buffer=True)
        mask_scene.render.resolution_x = opt_render_width
        mask_scene.render.resolution_y = opt_render_height
        for mat in dup.data.materials:
            node_uvmap = mat.node_tree.nodes.get("VLM_UVMapNode")
            if not node_uvmap:
                node_uvmap = mat.node_tree.nodes.new(type='ShaderNodeAttribute')
                node_uvmap.attribute_name = 'UVMap'
                node_uvmap.name = "VLM_UVMapNode"

            ti = mat.node_tree.nodes.get("VLM_BakeImage")
            if not ti:
                ti = mat.node_tree.nodes.new("ShaderNodeTexImage")
                ti.name = "VLM_BakeImage"

            ti_albedo = mat.node_tree.nodes.get("VLM_AlbedoImage")
            if not ti_albedo:
                ti_albedo = mat.node_tree.nodes.new("ShaderNodeTexImage")
                ti_albedo.name = "VLM_AlbedoImage" 

            ti_normal = mat.node_tree.nodes.get("VLM_NormalImage")
            if not ti_normal:
                ti_normal = mat.node_tree.nodes.new("ShaderNodeTexImage")
                ti_normal.name = "VLM_NormalImage"

            ti.image = bake_img
            ti_albedo.image = bake_img_albedo
            ti_normal.image = bake_img_normal

            if not any(link.to_node == ti for link in node_uvmap.outputs[0].links):
                mat.node_tree.links.new(node_uvmap.outputs[0], ti.inputs[0])
            
            if not any(link.to_node == ti_albedo for link in node_uvmap.outputs[0].links):
                mat.node_tree.links.new(node_uvmap.outputs[0], ti_albedo.inputs[0])
            
            if not any(link.to_node == ti_normal for link in node_uvmap.outputs[0].links):
                mat.node_tree.links.new(node_uvmap.outputs[0], ti_normal.inputs[0])

            mat.node_tree.nodes.active = ti_normal

        dup.data.uv_layers['UVMap'].active = True 

        render_path_nm = f'{bakepath}NormalMap - Bake - {obj.name}.exr'
        normalmap_enabled = bool(dup.vlmSettings.bake_normalmap)

        # Normal maps are persistent cached bake assets.  If the EXR already
        # exists, load it and NEVER run the NORMAL bake again unless forced.
        if normalmap_enabled:
            abs_normal_path = bpy.path.abspath(render_path_nm)
            if not opt_force_render and os.path.exists(abs_normal_path):
                try:
                    bake_img_normal = bpy.data.images.load(abs_normal_path, check_existing=True)
                    for mat in dup.data.materials:
                        ti_normal = mat.node_tree.nodes.get("VLM_NormalImage")
                        if ti_normal:
                            ti_normal.image = bake_img_normal
                    logger.info(f"Baking {obj.name} normal map. - Skipped: cached EXR exists")
                    n_existing += 1
                except Exception as e:
                    logger.warning(f"Could not load cached normal map '{abs_normal_path}', rebuilding it: {e}")
                    need_normal_bake = True
                else:
                    need_normal_bake = False
            else:
                need_normal_bake = True

            if need_normal_bake:
                elapsed = time.time() - start_time
                msg = f". Baking '{obj.name}' normal map. Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
                if elapsed > 0 and n_render_performed > 0:
                    elapsed_per_render = elapsed / n_render_performed
                    remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                    msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
                logger.info(msg)
                if bake_info_group and 'IsNormalMap' in bake_info_group.nodes:
                    bake_info_group.nodes['IsNormalMap'].outputs["Value"].default_value = 1.0
                scene.render.filepath = render_path_nm
                scene.render.image_settings.file_format = 'OPEN_EXR'
                scene.render.image_settings.color_mode = 'RGB'
                scene.render.image_settings.exr_codec = 'ZIP'
                scene.render.image_settings.color_depth = '16'

                with context.temp_override(scene=scene, active_object=dup, selected_objects=[dup]):
                    bpy.ops.object.bake(
                        type='NORMAL',
                        normal_space='OBJECT',
                        normal_r='POS_X',
                        normal_g='NEG_Y',
                        normal_b='NEG_Z',
                        margin=context.scene.vlmSettings.padding,
                        use_selected_to_active=False,
                        use_clear=True
                    )

                # Persist the actual baked image buffer to the Renders cache.
                # Image.save_render can target the render buffer in some Blender versions;
                # for baked images use the image datablock's own save() API explicitly.
                os.makedirs(os.path.dirname(abs_normal_path), exist_ok=True)
                bake_img_normal.filepath_raw = abs_normal_path
                bake_img_normal.file_format = 'OPEN_EXR'
                bake_img_normal.save()
                if not os.path.isfile(abs_normal_path):
                    raise RuntimeError(f"Normal map bake completed but file was not created: {abs_normal_path}")

                logger.info(f"Saved normal map: {abs_normal_path}")
                n_render_performed += 1
                if bake_info_group and 'IsNormalMap' in bake_info_group.nodes:
                    bake_info_group.nodes['IsNormalMap'].outputs["Value"].default_value = 0.0
        else:
            logger.info(f"Baking {obj.name} normal map. - Skipped: bake_normalmap is disabled")

        render_path_diffuse = f'{bakepath}DiffuseColor - Bake - {obj.name}.exr'
        abs_diffuse_path = bpy.path.abspath(render_path_diffuse)

        # Albedo is intentionally controlled by the same "Bake Normal" option.
        # If Bake Normal is disabled, do NOT load, bake, or save an albedo map.
        # This prevents the diffuse/albedo bake from running independently.
        if not normalmap_enabled:
            logger.info(f"Baking {obj.name} diffuse color (albedo). - Skipped: bake_normalmap is disabled")
            need_diffuse_bake = False
        # Albedo is also a persistent bake asset. Reuse the cached EXR instead
        # of baking it again when a render is resumed.
        elif not opt_force_render and os.path.exists(abs_diffuse_path):
            try:
                bake_img_albedo = bpy.data.images.load(abs_diffuse_path, check_existing=True)
                for mat in dup.data.materials:
                    ti_albedo = mat.node_tree.nodes.get("VLM_AlbedoImage")
                    if ti_albedo:
                        ti_albedo.image = bake_img_albedo
                logger.info(f"Baking {obj.name} diffuse color (albedo). - Skipped: cached EXR exists")
                n_existing += 1
            except Exception as e:
                logger.warning(f"Could not load cached albedo '{abs_diffuse_path}', rebuilding it: {e}")
                need_diffuse_bake = True
            else:
                need_diffuse_bake = False
        else:
            need_diffuse_bake = True

        if need_diffuse_bake:
            elapsed = time.time() - start_time
            msg = f". Baking '{obj.name}' diffuse color (albedo). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
            if elapsed > 0 and n_render_performed > 0:
                elapsed_per_render = elapsed / n_render_performed
                remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
            logger.info(msg)

            if bake_info_group and 'IsDiffuse' in bake_info_group.nodes:
                bake_info_group.nodes['IsDiffuse'].outputs["Value"].default_value = 1.0

            scene.render.filepath = render_path_diffuse
            scene.render.image_settings.file_format = 'OPEN_EXR'
            scene.render.image_settings.color_mode = 'RGB'
            scene.render.image_settings.exr_codec = 'ZIP'
            scene.render.image_settings.color_depth = '16'

            for mat in dup.data.materials:
                mat.node_tree.nodes.active = mat.node_tree.nodes.get("VLM_AlbedoImage")

            with context.temp_override(scene=scene, active_object=dup, selected_objects=[dup]):
                set_diffuse_color_pass_enabled(bpy.context.view_layer, True)
                bpy.ops.object.bake(
                    type='DIFFUSE',
                    pass_filter={'COLOR'},
                    use_selected_to_active=False,
                    use_clear=True,
                    margin=context.scene.vlmSettings.padding
                )

            # Persist the actual baked image buffer to the Renders cache.
            os.makedirs(os.path.dirname(abs_diffuse_path), exist_ok=True)
            bake_img_albedo.filepath_raw = abs_diffuse_path
            bake_img_albedo.file_format = 'OPEN_EXR'
            bake_img_albedo.save()
            if not os.path.isfile(abs_diffuse_path):
                raise RuntimeError(f"Albedo bake completed but file was not created: {abs_diffuse_path}")

            logger.info(f"Saved albedo: {abs_diffuse_path}")
            logger.info('\n')
            n_render_performed += 1

            if bake_info_group and 'IsDiffuse' in bake_info_group.nodes:
                bake_info_group.nodes['IsDiffuse'].outputs["Value"].default_value = 0.0

        for mat in dup.data.materials:
            mat.node_tree.nodes.active = mat.node_tree.nodes.get("VLM_BakeImage")

        for i, scenario in enumerate(light_scenarios, start=1):
            name, is_lightmap, light_col, lights = scenario
            render_path = f'{bakepath}{scenario[0]} - Bake - {obj.name}.exr'
            influence_path = f'{bakepath}{scenario[0]} - Influence - {obj.name}.exr'
            msg = f". Baking '{obj.name}' for '{scenario[0]}' ({i}/{n_lighting_situations}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"


            light_linking_collections = []

            for light_obj in light_col.all_objects:
                if hasattr(light_obj, 'light_linking'):
                    receiver_collection = light_obj.light_linking.receiver_collection
                    if receiver_collection:
                        for index, link_obj in enumerate(receiver_collection.all_objects):
                            if link_obj.name == obj.name and dup.name not in receiver_collection.all_objects:  
                                logger.info(f'Linking light for {obj.name} to Recevier Collection {receiver_collection.name}')
                                light_linking_collections.append(receiver_collection)
                                receiver_collection.objects.link(dup)
                                receiver_collection.collection_objects[-1].light_linking.link_state = receiver_collection.collection_objects[index].light_linking.link_state

                blocker_collection = light_obj.light_linking.blocker_collection
                if blocker_collection:
                    for index, link_obj in enumerate(receiver_collection.all_objects):
                        if link_obj.name == obj.name and dup.name not in blocker_collection.all_objects:
                            logger.info(f'Linking light for {obj.name} to Blocker Collection {blocker_collection.name}')
                            light_linking_collections.append(blocker_collection)
                            blocker_collection.objects.link(dup)
                            blocker_collection.collection_objects[-1].light_linking.link_state = blocker_collection.collection_objects[index].light_linking.link_state


            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)) or not os.path.exists(bpy.path.abspath(influence_path)):
                state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, obj_mask, render_col)
                elapsed = time.time() - start_time
                if elapsed > 0 and n_render_performed > 0:
                    elapsed_per_render = elapsed / n_render_performed
                    remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                    msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
                if state:
                    logger.info(msg)
                    scene.render.filepath = render_path
                    scene.render.image_settings.file_format = 'OPEN_EXR'
                    scene.render.image_settings.color_mode = 'RGB' if is_lightmap else 'RGBA'
                    scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression
                    scene.render.image_settings.color_depth = '16'
                    # Bake texture (context needs an active, linked, not hidden, mesh)
                    with context.temp_override(scene=scene, active_object=dup, selected_objects=[dup]):
                        scene.render.bake.view_from = 'ACTIVE_CAMERA'
                        scene.cycles.use_denoising = False
                        # Load the rendered image (make sure to set the correct path to the rendered image)
                        bpy.ops.object.bake(type='COMBINED', margin=context.scene.vlmSettings.padding, use_selected_to_active=False, use_clear=True)
                        #bake_img.save_render(bpy.path.abspath(render_path), scene=scene)                        
                    
                    with context.temp_override(scene=temp_denoise_scene):
                        denoise_file_output_node.format.color_mode = 'RGB' if is_lightmap else 'RGBA'
                        render_path_denoise = f'{bakepath}{scenario[0]} - Bake - {obj.name}'
                        denoise_image_node.image = bake_img

                        denoise_normal_map_node.image = bake_img_normal
                        denoise_albedo_map_node.image = bake_img_albedo

                        if bpy.app.version >= (5, 0, 0):
                            configure_file_output_node_52(
                                denoise_file_output_node,
                                socket_name='Image',
                                directory=os.path.dirname(bpy.path.abspath(render_path_denoise)),
                                file_name=os.path.basename(render_path_denoise),
                                file_format='OPEN_EXR',
                                color_mode='RGB' if is_lightmap else 'RGBA',
                                color_depth='16',
                                exr_codec='ZIP',
                                use_file_extension=False,
                            )
                        else:
                            denoise_file_output_node.base_path = os.path.dirname(bpy.path.relpath(render_path_denoise))
                            denoise_file_output_node.file_slots[0].path = os.path.basename(bpy.path.relpath(render_path_denoise))
                        bpy.ops.render.render(use_viewport=False, write_still=False)
                        os.rename(bpy.path.abspath(f'{render_path_denoise}0001.exr'), bpy.path.abspath(f'{render_path_denoise}.exr'))
                        
                    restore_func(state)
                    # Render for influence map
                    dup2 = dup.copy()
                    dup2.data = dup2.data.copy()
                    for poly in dup2.data.polygons:
                        poly.material_index = 0
                    dup2.data.materials.clear()
                    mat = bpy.data.materials.new(name)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    nodes.clear()
                    links = mat.node_tree.links
                    node_emission = nodes.new('ShaderNodeEmission')
                    node_transparent = nodes.new('ShaderNodeBsdfTransparent')
                    node_add = nodes.new('ShaderNodeAddShader')
                    node_output = nodes.new(type='ShaderNodeOutputMaterial')   
                    node_tex = nodes.new(type='ShaderNodeTexImage')
                    node_tex.image = bake_img
                    node_uvmap = nodes.new(type='ShaderNodeUVMap')
                    node_uvmap.uv_map = 'UVMap'
                    links.new(node_uvmap.outputs[0], node_tex.inputs[0])
                    links.new(node_tex.outputs[0], node_emission.inputs[0])
                    links.new(node_tex.outputs[1], node_emission.inputs[1])
                    links.new(node_emission.outputs[0], node_add.inputs[0])
                    links.new(node_transparent.outputs[0], node_add.inputs[1])
                    links.new(node_add.outputs[0], node_output.inputs[0])
                    set_material_surface_render_method(mat, 'BLEND')
                    dup2.data.materials.append(mat)
                    mask_scene.render.filepath = influence_path
                    mask_scene.collection.objects.link(dup2)
                    mask_scene.render.image_settings.file_format = "OPEN_EXR"
                    mask_scene.render.image_settings.color_mode = 'RGB'
                    mask_scene.render.image_settings.exr_codec = 'DWAA'
                    mask_scene.render.image_settings.color_depth = '16'
                    bpy.ops.render.render(write_still=True, scene=mask_scene.name)
                    mask_scene.collection.objects.unlink(dup2)
                    mask_scene.render.image_settings.file_format = "PNG"
                    bpy.data.materials.remove(mat)
                    logger.info('\n')
                    n_render_performed += 1
                    with context.temp_override(active_object=dup2, selected_objects=[dup2]):
                        bpy.ops.object.delete()
                else:
                    logger.info(f'{msg} - Skipped (no influence)')
                    n_skipped += 1
            else:
                logger.info(f'{msg} - Skipped since it is already rendered and cached')
                n_existing += 1
                
            for light_collection in light_linking_collections:
                light_collection.objects.unlink(dup)


        for mat in dup.data.materials:
            node = mat.node_tree.nodes.get("VLM_UVMapNode")
            if node:
                mat.node_tree.nodes.remove(node)
            node = mat.node_tree.nodes.get("VLM_BakeImage")
            if node:
                mat.node_tree.nodes.remove(node)
            node = mat.node_tree.nodes.get("VLM_AlbedoImage")
            if node:
                mat.node_tree.nodes.remove(node)
            node = mat.node_tree.nodes.get("VLM_NormalImage")
            if node:
                mat.node_tree.nodes.remove(node)

        bpy.data.images.remove(bake_img)
        bpy.data.images.remove(bake_img_albedo)
        bpy.data.images.remove(bake_img_normal)

        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.link(obj)

        if obj.vlmSettings.bake_mask:
            render_col.objects.unlink(obj.vlmSettings.bake_mask)
        
        

        with context.temp_override(active_object=dup, selected_objects=[dup]):
            bpy.ops.object.delete()

    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 0.0
    compositor_trees = []
    for temp_scene in (scene, mask_scene, temp_denoise_scene):
        tree = getattr(temp_scene, 'compositing_node_group', None) if bpy.app.version >= (5, 0, 0) else getattr(temp_scene, 'node_tree', None)
        if tree is not None:
            compositor_trees.append(tree)
    bpy.data.scenes.remove(scene)
    bpy.data.scenes.remove(mask_scene)
    bpy.data.scenes.remove(temp_denoise_scene)
    # Blender 5.x stores compositor trees as separate node-group datablocks.
    # Remove only orphaned trees belonging to these temporary scenes.
    if bpy.app.version >= (5, 0, 0):
        for tree in dict.fromkeys(compositor_trees):
            if tree.name in bpy.data.node_groups and tree.users == 0:
                bpy.data.node_groups.remove(tree)
    # Remove exactly the temporary collections created by this render run.
    # Do not look them up by global name, because a user project may already
    # contain collections named 'Indirect' or 'Render'.
    if indirect_col and indirect_col.name in bpy.data.collections:
        bpy.data.collections.remove(indirect_col)
    if render_col and render_col.name in bpy.data.collections:
        bpy.data.collections.remove(render_col)
    length = time.time() - start_time
    vlm_utils.set_diagnostic_stage('Rendering complete')
    vlm_utils.diagnostic_snapshot('rendering-complete')
    logger.info(f"\nRendering finished in a total time of {vlm_utils.format_time(length)}")
    if n_existing > 0: logger.info(f". {n_existing:>3} renders were skipped since they were already existing")
    if n_skipped > 0: logger.info(f". {n_skipped:>3} renders were skipped since objects were outside of lights influence")
    if n_render_performed > 0: logger.info(f". {n_render_performed:>3} renders were computed ({vlm_utils.format_time(length/n_render_performed)} per render)")

    return {'FINISHED'}
