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
from math import radians
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency

global_scale = vlm_utils.global_scale

# TODO
# - Allow to use either internal UV packing or UVPacker addon
# - Allow to have an object (or a group) to be baked to a target object (like bake selected to active in Blender) for inserts,...
# - Implement 'Movable' bake mode (each object is baked to a separate mesh, keeping its origin)
# - Perform tests with transparent elements (especially ramps)


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
        (scenario id, light collection, single light, custom data)
    """
    light_scenarios = {"Environment": ["Environment", None, None, None]}
    lights_col = vlm_collections.get_collection('LIGHTS', create=False)
    if lights_col is not None:
        for light_col in lights_col.children:
            lights = light_col.objects
            if light_col.hide_render == False and len(lights) > 0:
                if light_col.vlmSettings.light_mode:
                    name = vlm_utils.strip_vlm(light_col.name)
                    light_scenarios[name] = [name, light_col, None, None]
                else:
                    for light in lights:
                        name = f"{vlm_utils.strip_vlm(light_col.name)} - {light.name}"
                        light_scenarios[name] = [name, light_col, light, None]
    return light_scenarios


def get_n_lightings(context):
    return len(get_lightings(context))
    
    
def get_n_render_groups(context):
    i = 0
    root_bake_col = vlm_collections.get_collection('BAKE', create=False)
    if root_bake_col is not None:
        while True:
            if next((obj for obj in root_bake_col.all_objects if obj.vlmSettings.render_group == i), None) is None:
                break
            i += 1
    return i
    

def compute_render_groups(context):
    """Evaluate the set of bake groups (groups of objects that do not overlap when rendered 
    from the camera point of view) and store the result in the object properties.
    """
    start_time = time.time()
    print(f"\nEvaluating render groups")
    opt_mask_size = 1024 # Height used for the object masks
    opt_mask_threshold = 0.0 # Alpha threshold used when building object groups
    opt_force_render = False # Force rendering even if cache is available
    
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
    context.scene.render.resolution_x = opt_mask_size / 2
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
        print(f". Evaluating object mask #{i:>3}/{len(all_objects)} for '{obj.name}'")
        # Render object visibility mask (basic low res render)
        context.scene.render.filepath = f"{bakepath}{obj.name}.png"
        if opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath)):
            initial_collection = vlm_collections.move_to_col(obj, tmp_col)
            bpy.ops.render.render(write_still=True)
            vlm_collections.restore_col_links(initial_collection)
        # Evaluate if this object can be grouped with previous renders (no alpha overlaps)
        im = Image.open(bpy.path.abspath(context.scene.render.filepath))
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
    context.scene.eevee.taa_render_samples = 64
    context.scene.render.engine = 'CYCLES'
    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.pop_state(col_state)
    print(f"\n{len(object_groups)} render groups defined in {int(time.time() - start_time)}s.")


def render_all_groups(context):
    """Render all render groups for all lighting situations
    """
    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)
    vlmProps = context.scene.vlmSettings
    opt_tex_size = int(vlmProps.tex_size)
    opt_force_render = False # Force rendering even if cache is available
    context.scene.render.resolution_y = opt_tex_size
    context.scene.render.resolution_x = opt_tex_size / 2
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '16'
    context.scene.view_layers["ViewLayer"].use_pass_z = True
    context.scene.render.film_transparent = True
    context.scene.use_nodes = False
    cg = vlm_utils.push_color_grading(True)
    n_render_performed = 0

    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    tmp_col = vlm_collections.get_collection('BAKETMP')
    indirect_col = vlm_collections.get_collection('INDIRECT')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    lights_col = vlm_collections.get_collection('LIGHTS')
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

    # Apply a ligth scenario for rendering, returning the previous state and a lambda to apply it
    def setup_light_scenario(context, scenario):
        if scenario[1] is None: # Base render
            context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
            return 0, lambda a : a
        else:
            context.scene.world = bpy.data.worlds["VPX.Env.Black"]
            if scenario[2] is None: # Light group render
                return vlm_collections.move_all_to_col(scenario[1].all_objects, tmp_col), lambda initial_state : vlm_collections.restore_all_col_links(initial_state)
            else: # single light render
                return vlm_collections.move_to_col(scenario[2], tmp_col), lambda initial_state : vlm_collections.restore_col_links(initial_state)

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
                state, restore_func = setup_light_scenario(context, scenario)
                n_render_performed = n_render_performed + 1
                bpy.ops.render.render(write_still=True)
                restore_func(state)
        vlm_collections.restore_all_col_links(initial_collections)
        vlm_collections.find_layer_collection(rlc, overlay_col).indirect_only = True
        context.scene.render.image_settings.use_zbuffer = False
        
        # Prepare compositor to apply overlay for the upcoming renders
        nodes = context.scene.node_tree.nodes
        nodes.clear() # I did not find a way to switch the active composer output, so we clear it each time
        links = context.scene.node_tree.links
        #if 'OverlayImage' not in nodes:
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

    print(f"\nRendering {n_render_groups} render groups for {n_lighting_situations} lighting situations")
    for group_index in range(n_render_groups):
        objects = [obj for obj in root_bake_col.all_objects if obj.vlmSettings.render_group == group_index]
        n_objects = len(objects)
        print(f"\nRendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for {n_lighting_situations} lighting situations")
        initial_collections = vlm_collections.move_all_to_col(objects, tmp_col)
        for i, (name, scenario) in enumerate(light_scenarios.items(), start=1):
            context.scene.render.filepath = f"{bakepath}{scenario[0]} - Group {group_index}.exr"
            if opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath)):
                print(f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]} ({i}/{n_lighting_situations})'")
                state, restore_func = setup_light_scenario(context, scenario)
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
        vlm_collections.restore_all_col_links(initial_collections)

    context.scene.node_tree.nodes.clear()

    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    vlm_utils.pop_color_grading(cg)
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.pop_state(col_state)
    print(f"\n{n_render_performed} groups rendered in {int(time.time() - start_time)}s.")


def create_bake_meshes(context):
    """Create all bake meshes, building from the render groups and renders cached during the previous steps
    """
    print("\nCreating all bake meshes")
    start_time = time.time()
    camera = bpy.data.objects['Camera']
    vlmProps = context.scene.vlmSettings
    n_render_groups = get_n_render_groups(context)

    # Purge unlinked datas to avoid wrong names
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    # Texture packing
    opt_padding = vlmProps.padding
    opt_tex_size = int(vlmProps.tex_size)

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
    vlm_collections.find_layer_collection(rlc, result_col).exclude = True
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, indirect_col).exclude = False
    vlm_collections.find_layer_collection(rlc, indirect_col).indirect_only = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False
    vlm_collections.find_layer_collection(rlc, root_bake_col).exclude = False
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    for bake_col in root_bake_col.children:
        vlm_collections.find_layer_collection(rlc, bake_col).exclude = False
        vlm_collections.find_layer_collection(rlc, bake_col).indirect_only = True

    # Prepare the list of lighting situation with a packmap material per render group, and a merge group per light situation
    light_merge_groups = {}
    light_scenarios = get_lightings(context)
    for name, light_scenario in light_scenarios.items():
        light_merge_groups[name] = []
        mats = []
        packmat = bpy.data.materials["VPX.Core.Mat.PackMap"]
        is_light = light_scenario[1] is not None
        for index in range(n_render_groups):
            mat = packmat.copy()
            mat.name = f"VPX.PM.{name}.RG{index}"
            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
            if is_light:
                mat.blend_method = 'BLEND'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.blend_method = 'OPAQUE'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
            mats.append(mat)
        light_scenario[3] = mats

    bake_results = []
    bakepath = vlm_utils.get_bakepath(context)
    for bake_col in root_bake_col.children:
        bake_group_name = vlm_utils.strip_vlm(bake_col.name)
        baked_objects = [obj for obj in bake_col.objects]
        bake_mode = bake_col.vlmSettings.bake_mode # either 'default' / 'movable' / 'playfield' see vlm_commons
    
        # Join all objects to build baked objects (converting to mesh, and preserving split normals)
        print(f"\n[{bake_col.name}] Building base bake target model")
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
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = to_join[0]
        for obj in to_join:
            obj.select_set(True)
        bpy.ops.object.join()
        bake_target = context.view_layer.objects.active
        bake_mesh = bake_target.data
        bake_mesh.name = "VLM.Bake Target"
        print(f". Objects merged ({len(bake_mesh.vertices)} vertices, {len(bake_mesh.polygons)} faces)")
        
        # Remove backfacing faces
        if opt_backface_limit_angle < 90.0:
            n_faces = len(bake_target.data.polygons)
            remove_backfacing(context, bake_target, camera.location, opt_backface_limit_angle)
            print(f". {n_faces - len(bake_target.data.polygons)} backfacing faces removed (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")
        
        # Simplify mesh
        if opt_optimize_mesh:
            n_faces = len(bake_target.data.polygons)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold = 0.001 * global_scale)
            bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.1))
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
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Subdivide long edges to avoid visible projection distortion, and allow better lightmap face pruning (recursive subdivisions)
        bpy.ops.object.mode_set(mode='EDIT')
        while True:
            bme = bmesh.from_edit_mesh(bake_mesh)
            long_edges = []
            for face in bme.faces:
                face.select = False
            for edge in bme.edges:
                edge.select = False
                ua, va = edge.verts[0].link_loops[0][bme.loops.layers.uv.active].uv
                ub, vb = edge.verts[1].link_loops[0][bme.loops.layers.uv.active].uv
                l = math.sqrt(0.25*(ub-ua)*(ub-ua)+(vb-va)*(vb-va))
                if l > 0.1: # 0.2 is sufficient for distortion, lower value is needed for lightmap face pruning
                    edge.select = True
                    long_edges.append(edge)
            if not long_edges:
                bmesh.update_edit_mesh(bake_mesh)
                break
            bmesh.ops.subdivide_edges(bme, edges=long_edges, cuts=1, use_grid_fill=True)
            bmesh.update_edit_mesh(bake_mesh)
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.project_from_view(override)
            print(f". {len(long_edges)} long edges subdivided to avoid projection distortion.")
        bpy.ops.object.mode_set(mode='OBJECT')
        
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
        
        # Triangulate (in the end, VPX only deals with triangles, and this simplify the lightmap pruning process)
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(bake_mesh)
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
        bmesh.update_edit_mesh(bake_mesh)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Create lightmap shell (extrude mesh along vertex normal according to its shell factor)
        light_mesh = bake_mesh.copy()
        bm = bmesh.new()
        bm.from_mesh(light_mesh)
        n = 0
        coords = []
        for v in bm.verts:
            sf = min(v.calc_shell_factor(), 10.0)
            coords.append(v.co + v.normal * sf * shell_size)
        for v, nv in zip(bm.verts, coords):
            v.co = nv
        bm.to_mesh(light_mesh)
        bm.free()

        # Build the visibility maps for the light shell
        vmap_instance = bpy.data.objects.new(f"VLM.Bake.Tmp.{bake_group_name}.{name}", light_mesh)
        tmp_col.objects.link(vmap_instance)
        bpy.ops.object.select_all(action='DESELECT')
        vmap_instance.select_set(True)
        context.view_layer.objects.active = vmap_instance
        vmaps = build_visibility_map(vmap_instance.data, n_render_groups, opt_lightmap_prune_res)
        tmp_col.objects.unlink(vmap_instance)
        bpy.data.objects.remove(vmap_instance)

        # Build bake object for each lighting situation
        for i, (name, light_scenario) in enumerate(light_scenarios.items(), start = 1):
            print(f"[{bake_col.name} {i:>3}/{len(light_scenarios)}] Creating bake model for {name}")
            is_light = light_scenario[1] is not None
            if is_light:
                bake_instance = bpy.data.objects.new(f"LM.{name}", light_mesh.copy())
            else:
                bake_instance = bpy.data.objects.new(f"BM.{bake_group_name}", bake_mesh.copy())
            bake_instance.vlmSettings.bake_objects = bake_group_name
            bake_instance_mesh = bake_instance.data
            tmp_col.objects.link(bake_instance)
            bpy.ops.object.select_all(action='DESELECT')
            bake_instance.select_set(True)
            context.view_layer.objects.active = bake_instance
            for index in range(n_render_groups):
                bake_instance_mesh.materials[index] = light_scenario[3][index]
            if is_light: # Remove unlit faces of lightmaps (lighting < threshold)
                #prune_lightmap_by_heatmap(bake_instance_mesh, bakepath, name, n_render_groups, opt_tex_size)
                #prune_lightmap_by_rasterization(bake_instance_mesh, bakepath, name, n_render_groups, opt_lightmap_prune_res)
                prune_lightmap_by_visibility_map(bake_instance_mesh, vlm_utils.get_bakepath(context, type='RENDERS'), name, n_render_groups, vmaps, opt_lightmap_prune_res)

            # Skip mesh if we do not have any polygons left
            if not bake_instance.data.polygons:
                tmp_col.objects.unlink(bake_instance)
                print(f". Mesh {name} has no more faces after optimization")
                continue

            # Compute texture density (depends on the amount of remaining faces)
            density = compute_uvmap_density(bake_instance_mesh, bake_instance_mesh.uv_layers["UVMap"])

            # Pack UV map (only if this bake mesh since it won't be merged afterward)
            if not is_light:
                bake_results.append(bake_instance)
                if bake_mode == 'playfield':
                    uv_layer_packed = bake_instance_mesh.uv_layers["UVMap Packed"]
                    uv_layer_packed.active = True
                    l, t, w, h = context.scene.vlmSettings.playfield_size
                    for loop in bake_instance_mesh.loops:
                        pt = bake_instance_mesh.vertices[loop.vertex_index].co
                        uv_layer_packed.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
                    density = -1

            # Save in result collection
            bake_instance.vlmSettings.bake_name = name
            bake_instance.vlmSettings.bake_tex_factor = density
            if is_light:
                bake_instance.vlmSettings.bake_type = 'lightmap'
                if light_scenario[2] is not None:
                    bake_instance.vlmSettings.bake_light = light_scenario[2].name
                else:
                    bake_instance.vlmSettings.bake_light = light_scenario[1].name
                light_merge_groups[name].append(bake_instance)
            elif bake_mode == 'playfield':
                bake_instance.vlmSettings.bake_type = 'playfield'
                bake_instance.vlmSettings.bake_light = ''
            else:
                bake_instance.vlmSettings.bake_type = 'bake'
                bake_instance.vlmSettings.bake_light = ''
            tmp_col.objects.unlink(bake_instance)
            result_col.objects.link(bake_instance)

        # Clean up for next bake group
        tmp_col.objects.unlink(bake_target)

    # Final view setup
    vlm_collections.delete_collection(tmp_col)
    vlm_collections.find_layer_collection(rlc, result_col).exclude = False
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    for bake_col in root_bake_col.children:
        vlm_collections.find_layer_collection(rlc, bake_col).exclude = True
        vlm_collections.find_layer_collection(rlc, bake_col).indirect_only = False

    # Merge light groups across bake groups
    print(f"\nMerging light map bake meshes")
    for name, bakes in light_merge_groups.items():
        if bakes:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bakes[0]
            density = 0
            for obj in bakes:
                density += obj.vlmSettings.bake_tex_factor
                obj.select_set(True)
            bpy.ops.object.join()
            bake_instance = context.view_layer.objects.active
            bake_instance.name = f'LM.{name}'
            bake_instance.vlmSettings.bake_tex_factor = density
            bake_instance.vlmSettings.bake_objects = ''
            if bake_instance.data.polygons:
                bake_results.append(bake_instance)
            else:
                print(f". Warning: light '{name}' has no influence and was entirely removed")
                [col.objects.unlink(obj) for col in bake_instance.users_collection]

    # Sort from higher texture fill factor to lowest, then fillup packmap buckets
    print(f"\nMerging and packing UV maps")
    bake_results.sort(key=lambda obj: obj.vlmSettings.bake_tex_factor, reverse=True)
    packmaps = []
    for bake in bake_results:
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
                # which needs uv to be adapt for to avoid texel density distorsion on the x axis
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
            print(f'.   Packmap #{index}: {w:>4}x{h:>4} playfield render')
        else:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bakes[0]
            for obj in bakes:
                obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')
            bpy.ops.uv.pack_islands(margin=opt_padding / opt_tex_size)
            bpy.ops.object.mode_set(mode='OBJECT')
            packed_density = 0
            for obj in bakes:
                packed_density += compute_uvmap_density(obj.data, obj.data.uv_layers["UVMap Packed"])
            print(f'.   Packmap #{index}: {density:>6.2%} density for {len(bakes)} objects => {w:>4}x{h:>4} texture size with {packed_density:>6.2%} packed density')

    # Purge unlinked datas
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f"\nbake meshes created in {int(time.time() - start_time)}s.")


def orient2d(ax, ay, bx, by, x, y):
    """Evaluate on which side of a line a-b, a given point stand
    """
    return (bx-ax)*(y-ay) - (by-ay)*(x-ax)


def build_visibility_map(bake_instance_mesh, n_render_groups, height):
    """Build a set of rasterized maps where each pixels contains the list of 
    visible faces for the given render group.
    """
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    width = max(1, int(height/2))
    vmaps = [[[] for xy in range(width * height)] for g in range(n_render_groups)]
    bm.faces.ensure_lookup_table()
    for face in bm.faces:
        group = face.material_index
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
        min_x = min(ax, bx, cx)
        min_y = min(ay, by, cy)
        max_x = max(ax, bx, cx)
        max_y = max(ay, by, cy)
        A01 = ay - by
        B01 = bx - ax
        A12 = by - cy
        B12 = cx - bx
        A20 = cy - ay
        B20 = ax - cx
        w0_row = orient2d(bx, by, cx, cy, min_x, min_y)
        w1_row = orient2d(cx, cy, ax, ay, min_x, min_y)
        w2_row = orient2d(ax, ay, bx, by, min_x, min_y)
        for y in range(min_y, max_y + 1):
            w0 = w0_row
            w1 = w1_row
            w2 = w2_row
            for x in range(min_x, max_x + 1):
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    vmaps[face.material_index][x + y * width].append(face.index)
                w0 += A12
                w1 += A20
                w2 += A01
            w0_row += B12
            w1_row += B20
            w2_row += B01
    bpy.ops.object.mode_set(mode='OBJECT')
    return vmaps


def prune_lightmap_by_visibility_map(bake_instance_mesh, render_path, name, n_render_groups, vmaps, map_height):
    """Prune faces based on there visibility in the precomputed visibility maps
    """
    bpy.ops.object.mode_set(mode='EDIT')
    n_faces = len(bake_instance_mesh.polygons)
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    for face in bm.faces:
        face.tag = False
    bm.faces.ensure_lookup_table()
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
        in vec2 uvInterp;
        out vec4 FragColor;
        void main() {
            vec4 t = texture(image, uvInterp).rgba;
            float v = t.a * dot(t.rgb, vec3(0.2989, 0.5870, 0.1140));
            FragColor = vec4(v, v, v, 1.0);
        }
    '''
    bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)
    for i in range(n_render_groups):
        image = bpy.data.images.load(f"{render_path}{name} - Group {i}.exr", check_existing=False)
        im_width, im_height = image.size
        h = min(map_height, im_height)
        w = int(im_width * h / im_height)
        # Rescale, convert to black and white, apply alpha, in a single pass on the GPU
        offscreen = gpu.types.GPUOffScreen(w, h)
        with offscreen.bind():
            bw_shader.bind()
            bw_shader.uniform_sampler("image", gpu.texture.from_image(image))
            batch_for_shader(
                bw_shader, 'TRI_FAN',
                {
                    "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                    "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
                },
            ).draw(bw_shader)
            bw = gpu.state.active_framebuffer_get().read_color(0, 0, w, h, 4, 0, 'UBYTE')
            bw.dimensions = w * h * 4
        offscreen.free()
        bpy.data.images.remove(image)
        for xy in range(w * h):
            if bw[4 * xy] > 2:
                for face_index in vmaps[i][xy]:
                    bm.faces[face_index].tag = True
    n_delete = 0
    for face in bm.faces:
        if not face.tag:
            face.select_set(True)
            n_delete += 1
    bmesh.update_edit_mesh(bake_instance_mesh)
    if n_delete > 0:
        bpy.ops.mesh.delete(type='FACE')
        print(f". Mesh optimized for {name} to {n_faces - n_delete} faces out of {n_faces} faces")
    bpy.ops.object.mode_set(mode='OBJECT')


def prune_lightmap_by_rasterization(bake_instance_mesh, bakepath, name, n_render_groups, opt_max_height=8192):
    """ Pruning by performing basic face rasterization in the corresponding render group
    This gives very good results, but is very slow and does not scale well.
    Rasterizer derived from: https://fgiesen.wordpress.com/2013/02/08/triangle-rasterization-in-practice/
    """
    bpy.ops.object.mode_set(mode='EDIT')
    n_faces = len(bake_instance_mesh.polygons)
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    n_delete = 0
    images = []
    for i in range(n_render_groups):
        image = bpy.data.images.load(f"{vlm_utils.get_bakepath(context, type='RENDERS')}{name} - Group {i}.exr", check_existing=False)
        im_width, im_height = image.size
        h = min(opt_max_height, im_height)
        w = int(im_width * h / im_height)
        if h < im_height:
            image.scale(w, h)
        images.append(image)
    for face in bm.faces:
        image = images[face.material_index]
        im_width, im_height = image.size
        if len(face.loops) != 3: # This should not happen
            continue
        a = face.loops[0][uv_layer].uv
        b = face.loops[1][uv_layer].uv
        c = face.loops[2][uv_layer].uv
        ax = int(a.x * im_width)
        ay = int(a.y * im_height)
        bx = int(b.x * im_width)
        by = int(b.y * im_height)
        cx = int(c.x * im_width)
        cy = int(c.y * im_height)
        min_x = min(ax, bx, cx)
        min_y = min(ay, by, cy)
        max_x = max(ax, bx, cx)
        max_y = max(ay, by, cy)
        A01 = ay - by
        B01 = bx - ax
        A12 = by - cy
        B12 = cx - bx
        A20 = cy - ay
        B20 = ax - cx
        w0_row = orient2d(bx, by, cx, cy, min_x, min_y)
        w1_row = orient2d(cx, cy, ax, ay, min_x, min_y)
        w2_row = orient2d(ax, ay, bx, by, min_x, min_y)
        influenced = False
        for y in range(min_y, max_y + 1):
            w0 = w0_row
            w1 = w1_row
            w2 = w2_row
            for x in range(min_x, max_x + 1):
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    pos = 4 * (x + y * im_width)
                    intensity = 299 * image.pixels[pos] + 587 * image.pixels[pos+1] + 114 * image.pixels[pos+2]
                    if intensity > 15: # 0.015 * 1000
                        influenced = True
                        break
                w0 += A12
                w1 += A20
                w2 += A01
            if influenced:
                break
            w0_row += B12
            w1_row += B20
            w2_row += B01
        if not influenced:
            face.select_set(True)
            n_delete += 1
    bmesh.update_edit_mesh(bake_instance_mesh)
    if n_delete > 0:
        bpy.ops.mesh.delete(type='FACE')
        print(f". Mesh optimized for {name} to {n_faces - n_delete} faces out of {n_faces} faces")
    for i in range(n_render_groups):
        bpy.data.images.remove(images[i])
    bpy.ops.object.mode_set(mode='OBJECT')


def prune_lightmap_by_heatmap(bake_instance_mesh, bakepath, name, n_render_groups, opt_tex_size):
    """Pruning unlit faces using a GPU generated 'heatmap' (max filtered mipmap)
    This is fast and works well but is not optimal enough, lots of faces are not pruned when
    they should."""

    opt_save_heatmap = False # Save the heatmap (for debug purpose only)
    
    # Shader code to generate 'heatmap' on the GPU to identify which faces are influenced by a given light
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
        uniform float threshold;
        in vec2 uvInterp;
        out vec4 FragColor;
        void main() {
            float v = dot(texture(image, uvInterp).rgb, vec3(0.2989, 0.5870, 0.1140));
            v = step(threshold, v);
            FragColor = vec4(v, v, v, 1.0);
        }
    '''
    downscale_fragment_shader = '''
        uniform sampler2D image;
        uniform float deltaU;
        uniform float deltaV;
        uniform float threshold;
        in vec2 uvInterp;
        out vec4 FragColor;
        void main() {
            float p0 = texture(image, uvInterp + vec2(   0.0,    0.0)).r;
            float p1 = texture(image, uvInterp + vec2(deltaU,    0.0)).r;
            float p2 = texture(image, uvInterp + vec2(   0.0, deltaV)).r;
            float p3 = texture(image, uvInterp + vec2(deltaU, deltaV)).r;
            float v = max(max(p0, p1), max(p2, p3));
            v = step(threshold, v);
            FragColor = vec4(v, v, v, 1.0);
        }
    '''
    downscale_shader = gpu.types.GPUShader(vertex_shader, downscale_fragment_shader)
    bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)

    bpy.ops.object.mode_set(mode='EDIT')
    n_faces = len(bake_instance_mesh.polygons)
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    n_delete = 0
    heatmaps = {}
    n_levels = opt_tex_size.bit_length()
    heatmaps = [[None for i in range(n_levels)] for j in range(n_render_groups)]
    for face in bm.faces:
        # Compute needed heatmap size that allows to test with a single lookup
        xmin = ymin = 1000000
        xmax = ymax = -1000000
        for loop in face.loops:
            uv = loop[uv_layer].uv
            xmin = min(xmin, uv.x)
            ymin = min(ymin, uv.y)
            xmax = max(xmax, uv.x)
            ymax = max(ymax, uv.y)
        heatmap_max_height = 2.0 / max((xmax - xmin) * 0.5, ymax - ymin)
        heatmap_level = min(max(1, int(heatmap_max_height).bit_length() - 1), n_levels-1)
        heatmap_height = 1 << heatmap_level
        heatmap_width = 1 << max(0, heatmap_level - 1)
        heatmap = heatmaps[face.material_index][heatmap_level]
        if heatmap is None:
            select_threshold = 0.02
            image = bpy.data.images.load(f"{vlm_utils.get_bakepath(context, type='RENDERS')}{name} - Group {face.material_index}.exr", check_existing=False)
            height = image.size[1]
            while height >= 2:
                im_width, im_height = image.size
                width = max(1, int(height / 2))
                if height == im_height: # Initial pass is a simple BW conversion
                    offscreen = gpu.types.GPUOffScreen(width, height)
                    with offscreen.bind():
                        bw_shader.bind()
                        bw_shader.uniform_sampler("image", gpu.texture.from_image(image))
                        bw_shader.uniform_float("threshold", select_threshold)
                        x0 = 0
                        x1 = 1.0
                        y0 = 0
                        y1 = 1.0
                        batch_for_shader(
                            bw_shader, 'TRI_FAN',
                            {
                                "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                                "uv": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
                            },
                        ).draw(bw_shader)
                        heatmap = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'UBYTE')
                        heatmap.dimensions = width * height * 4
                    offscreen.free()
                else: # Following passes are a 2x downscale with a 'max' filter
                    if True: #GPU path
                        offscreen = gpu.types.GPUOffScreen(width, height)
                        with offscreen.bind():
                            downscale_shader.bind()
                            downscale_shader.uniform_sampler("image", gpu.texture.from_image(image))
                            downscale_shader.uniform_float("deltaU", 1.0 / im_width)
                            downscale_shader.uniform_float("deltaV", 1.0 / im_height)
                            downscale_shader.uniform_float("threshold", select_threshold)
                            x0 =       0.5 / im_width
                            x1 = 1.0 - 1.5 / im_width
                            y0 =       0.5 / im_height
                            y1 = 1.0 - 1.5 / im_height
                            batch_for_shader(
                                downscale_shader, 'TRI_FAN',
                                {
                                    "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                                    "uv": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
                                },
                            ).draw(downscale_shader)
                            heatmap = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'UBYTE')
                            heatmap.dimensions = width * height * 4
                        offscreen.free()
                    else: # CPU path (this is kept as the reference path, and for the time being, GPU path is not strictly identical... color conversion bug ?)
                        new_heatmap = [255 for i in range(width*height*4)]
                        for x in range(width):
                            for y in range(height):
                                p = 4 * (x + y * width)
                                p2 = 4 * (2*x   + 2*y * im_width)
                                p3 = 4 * (2*x+1 + 2*y * im_width)
                                p4 = 4 * (2*x   + (2*y+1) * im_width)
                                p5 = 4 * (2*x+1 + (2*y+1) * im_width)
                                v = max(heatmap[p2], heatmap[p3])
                                v2 = max(heatmap[p4], heatmap[p5])
                                new_heatmap[p+0] = new_heatmap[p+1] = new_heatmap[p+2] = max(v,v2)
                        heatmap = new_heatmap
                bpy.data.images.remove(image)
                image = bpy.data.images.new("HeatMap", width, height)
                image.colorspace_settings.is_data = True
                image.colorspace_settings.name = 'Raw'
                image.pixels = [v / 255 for v in heatmap]
                heatmaps[face.material_index][height.bit_length() - 1] = [heatmap[4 * i] for i in range(width*height)]
                if opt_save_heatmap:
                    image.filepath_raw = bpy.path.abspath(f"{bakepath}{name} - Group {face.material_index} - Heatmap {height}.png")
                    image.file_format = 'PNG'
                    image.save()
                height = height >> 1
            bpy.data.images.remove(image)
            heatmaps[face.material_index][0] = [255] # last level is 1x1, always passing
            heatmap = heatmaps[face.material_index][heatmap_level]
        influenced = False
        for loop in face.loops:
            uv = loop[uv_layer].uv
            px = int(uv.x * heatmap_width)
            py = int(uv.y * heatmap_height)
            if 0 <= px < heatmap_width and 0 <= py < heatmap_height and heatmap[px + py * heatmap_width] != 0:
                influenced = True
                break
        if not influenced:
            face.select_set(True)
            n_delete += 1
    bmesh.update_edit_mesh(bake_instance_mesh)
    if n_delete > 0:
        bpy.ops.mesh.delete(type='FACE')
        print(f". Mesh optimized for {name} to {n_faces-n_delete} faces out of {n_faces} faces")
    bpy.ops.object.mode_set(mode='OBJECT')


def render_packmaps_gpu(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation using Blender's GPU module: fast and efficient, but the offscreen is always RGBA8
    Padding is not handled, color conversion neither
    """
    start_time = time.time()
    print(f"\nRendering packmaps")
    vlmProps = context.scene.vlmSettings

    opt_force_render = False # Force rendering even if cache is available
    opt_padding = vlmProps.padding
    
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
    context.scene.cycles.samples = 1
    context.scene.cycles.use_denoising = False
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break

        basepath = f"{bakepath}Packmap {packmap_index}"
        path_exr = bpy.path.abspath(basepath + '.exr')
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        
        if opt_force_render or not os.path.exists(path_exr):
            tex_width = objects[0].vlmSettings.bake_packmap_width
            tex_height = objects[0].vlmSettings.bake_packmap_height
            pack_image = bpy.data.images.new(f"PackMap{packmap_index}", tex_width, tex_height, alpha=True)
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
                void main() {
                    FragColor = texture(render, uvInterp).rgba;
                }
            '''
            shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
            offscreen = gpu.types.GPUOffScreen(tex_width, tex_height)
            with offscreen.bind():
                fb = gpu.state.active_framebuffer_get()
                fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                shader.bind()
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
                        for loop_index in poly.loop_indices:
                            uvs[poly.material_index].append(uv_layer.data[loop_index].uv)
                            pts[poly.material_index].append(uv_layer_packed.data[loop_index].uv)
                    for i,_ in enumerate(mesh.materials):
                        if pts[i]:
                            path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                            unload = vlm_utils.image_by_path(path) is None
                            render = bpy.data.images.load(path, check_existing=True)
                            shader.uniform_sampler("render", gpu.texture.from_image(render))
                            batch_for_shader(shader, 'TRIS', {"pos": pts[i], "uv": uvs[i]}).draw(shader)
                            if unload:
                                bpy.data.images.remove(render)
                buffer = offscreen.texture_color.read()
                buffer.dimensions = tex_width * tex_height * 4
            offscreen.free()
            pack_image.pixels = [v / 255 for v in buffer]
            pack_image.filepath_raw = path_exr
            pack_image.file_format = 'OPEN_EXR'
            pack_image.save()
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)

        if opt_force_render or not os.path.exists(path_webp) or os.path.getmtime(path_webp) < os.path.getmtime(path_png):
            Image.open(path_png).save(path_webp, 'WEBP')

        packmap_index += 1

    context.scene.cycles.samples = 64
    context.scene.cycles.use_denoising = True
    vlm_collections.pop_state(col_state)
    vlm_utils.pop_color_grading(cg)
    print(f"\n{packmap_index} packmaps rendered in {int(time.time() - start_time)}s.")


def render_packmaps_bake(context):
    """Render all packmaps corresponding for the available current bake results.
    Implementation using Blender Cycle's builtin bake. This works perfectly but is rather slow.
    """
    start_time = time.time()
    print(f"\nRendering packmaps")
    vlmProps = context.scene.vlmSettings

    opt_force_render = False # Force rendering even if cache is available
    opt_padding = vlmProps.padding
    
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
    context.scene.cycles.samples = 1
    context.scene.cycles.use_denoising = False
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break

        basepath = f"{bakepath}Packmap {packmap_index}"
        path_exr = bpy.path.abspath(basepath + '.exr')
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        
        if opt_force_render or not os.path.exists(path_exr):
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
                unloads = []
                for i, mat in enumerate(obj.data.materials):
                    path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                    render = vlm_utils.image_by_path(path)
                    if render is None:
                        render = bpy.data.images.load(path, check_existing=False)
                        unloads.append(render)
                    mat.node_tree.nodes["BakeTex"].image = render
                    mat.node_tree.nodes["PackMap"].inputs[2].default_value = vlm_utils.select(is_light, 1.0, 0.0)
                    mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake
                    mat.node_tree.nodes["PackTex"].image = pack_image
                    mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
                    mat.blend_method = 'OPAQUE'
                bpy.ops.object.bake(type='COMBINED', pass_filter={'EMIT', 'DIRECT'}, margin=opt_padding)
                for mat in obj.data.materials:
                    mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview
                    mat.blend_method = vlm_utils.select(is_light, 'BLEND', 'OPAQUE')
                for render in unloads:
                    bpy.data.images.remove(render)
                context.scene.render.bake.use_clear = False
            pack_image.filepath_raw = path_exr
            pack_image.file_format = 'OPEN_EXR'
            pack_image.save()
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)

        if opt_force_render or not os.path.exists(path_webp) or os.path.getmtime(path_webp) < os.path.getmtime(path_png):
            Image.open(path_png).save(path_webp, 'WEBP')

        packmap_index += 1

    context.scene.cycles.samples = 64
    context.scene.cycles.use_denoising = True
    vlm_collections.pop_state(col_state)
    vlm_utils.pop_color_grading(cg)
    print(f"\n{packmap_index} packmaps rendered in {int(time.time() - start_time)}s.")


def render_packmaps_eevee(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation using Eevee render. Works fine. No padding support for the time being
    """
    start_time = time.time()
    print(f"\nRendering packmaps")
    vlmProps = context.scene.vlmSettings

    opt_force_render = False # Force rendering even if cache is available
    opt_padding = vlmProps.padding
    
    # Purge unlinked datas to avoid out of memory error
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    tmp_col = vlm_collections.get_collection('BAKETMP')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    context.scene.render.engine = 'BLENDER_EEVEE'
    context.scene.render.film_transparent = True
    context.scene.eevee.taa_render_samples = 1
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '16'
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

        basepath = f"{bakepath}Packmap {packmap_index}"
        path_exr = bpy.path.abspath(basepath + '.exr')
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        
        if not opt_force_render and os.path.exists(path_exr) and os.path.exists(path_png) and os.path.exists(path_webp):
            packmap_index += 1
            continue
        
        tex_width = objects[0].vlmSettings.bake_packmap_width
        tex_height = objects[0].vlmSettings.bake_packmap_height
        context.scene.render.resolution_x = tex_width
        context.scene.render.resolution_y = tex_height
        context.scene.render.filepath = path_exr
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
            for mat_index,_ in enumerate(obj.data.materials):
                path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {mat_index}.exr"
                img = vlm_utils.image_by_path(path)
                if img is None:
                    img = bpy.data.images.load(path, check_existing=False)
                    unloads.append(path)
                mat = bpy.data.materials.new(f'Tmp.Pack.{obj_index}.{mat_index}')
                mat.blend_method = 'BLEND'
                mat.shadow_method = 'NONE'
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                nodes.clear()
                node_tex = nodes.new(type='ShaderNodeTexImage')
                node_tex.image = img
                node_premul = nodes.new("ShaderNodeMixRGB")
                node_emit = nodes.new("ShaderNodeEmission")
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

            # Render it from an ortho projection with Eevee, and combine with previous render with the compositor
            bpy.ops.render.render(write_still=True)

            tmp_col.objects.unlink(pack_obj)
            for path in unloads:
                img = vlm_utils.image_by_path(path)
                if img:
                    bpy.data.images.remove(img)
            for mat in mats:
                bpy.data.materials.remove(mat)
                
            # Prepare compositor to combine bake/light maps (we can not render all at once, since this will crash by out of memory on large renders)
            context.scene.use_nodes = True
            context.scene.node_tree.nodes['PackmapImage'].image = bpy.data.images.load(path_exr)

        img = bpy.data.images.load(path_exr, check_existing=True)
        img.filepath_raw = path_png
        img.file_format = 'PNG'
        img.save()
        bpy.data.images.remove(img)
        Image.open(path_png).save(path_webp, 'WEBP')
        
        packmap_index += 1
        
    context.scene.camera = prev_camera
    tmp_col.objects.unlink(camera)
    context.scene.eevee.taa_render_samples = 64
    context.scene.render.engine = 'CYCLES'
    vlm_collections.pop_state(col_state)
    vlm_utils.pop_color_grading(cg)
    print(f"\n{packmap_index} packmaps rendered in {int(time.time() - start_time)}s.")
    
 
def render_packmaps(context):
    render_packmaps_bake(context)