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
# - Object groups are created per bake group: they should be made per scene to limit the number of renders
# - Allow to use either internal UV packing or UVPacker addon
# - Support pack mapping to a non square texture
# - Combine multiple light pack maps into a single pack map
# - Save renders as HDR images (OpenEXR 16 bits, eventually 32 bits, to be evaluated) since colorgrading is performed on final export
# - Allow to have an object (or a group) to be baked to a target object (like bake seclected to active) for inserts, for playfield with text overlay,...
# - Implement 'Movable' bake mode (each object is baked to a separate mesh, keeping its origin)
# - Allow to have 'overlays' (for insert overlays) which are not baked but overlayed on all others
#     Render overlay group (with depth pass) and save to an OpenEXR Multilayer file
#     Compose all renders with overlay pass, filtering by z and alpha
# - Do not load renders when creating bake models since this will cause an OOM crash with high number of lights, add an operator to load them on demand
# - Split the baking process for a more interactive use
#  x  Stage 1: compute render groups => ability to invalidate cache, store group ownership for each object, show it in UI
#  x  Stage 2: rendering => ability to invalidate cache / recompute individually or globally the renders
#     Stage 3: create bake mesh, create packmap groups (merge of multiple lightmaps), pack UVs
#     Stage 4: review meshes, edit packmaps groups, adapt UV packing (ability to load/unload the renders to avoid crashing by OOM)




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
    from the camera point of view) and  store the result in the object properties.
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

    object_groups = []
    bakepath = vlm_utils.get_bakepath(context)
    if not os.path.exists(bpy.path.abspath(f"{bakepath}Object masks/")):
        os.mkdir(bpy.path.abspath(f"{bakepath}Object masks/"))
    all_objects = [obj for obj in root_bake_col.all_objects]
    for i, obj in enumerate(all_objects, start=1):
        print(f". Evaluating object mask #{i:>3}/{1+len(all_objects)} for '{obj.name}'")
        # Render object visibility mask (basic low res render)
        context.scene.render.filepath = f"{bakepath}Object masks/{obj.name}.png"
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
    print("\nRendering all bake groups for all lighting situations")
    bakepath = f"{vlm_utils.get_bakepath(context)}Render groups/"
    if not os.path.exists(bpy.path.abspath(bakepath)):
        os.mkdir(bpy.path.abspath(bakepath))
    vlmProps = context.scene.vlmSettings
    opt_tex_size = vlmProps.tex_size
    opt_force_render = False # Force rendering even if cache is available
    context.scene.render.resolution_y = opt_tex_size
    context.scene.render.resolution_x = opt_tex_size / 2
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '16'
    cg = vlm_utils.push_color_grading(True)

    col_state = vlm_collections.push_state()

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
    vlm_collections.find_layer_collection(rlc, lights_col).exclude = True
    vlm_collections.find_layer_collection(rlc, root_bake_col).exclude = False
    for bake_col in root_bake_col.children:
        vlm_collections.find_layer_collection(rlc, bake_col).exclude = False
        vlm_collections.find_layer_collection(rlc, bake_col).indirect_only = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    # FIXME render overlay collection and save it, activate composer accordingly to overlay it on object group renders (z masked)

    n_render_performed = 0
    n_render_groups = get_n_render_groups(context)
    for group_index in range(n_render_groups):
        objects = [obj for obj in root_bake_col.all_objects if obj.vlmSettings.render_group == group_index]
        n_objects = len(objects)
        print(f"\nRendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for all lighting situations")

        def perform_render(lighting_name):
            print(f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{lighting_name}'")
            context.scene.render.filepath = f"{bakepath}{lighting_name} - Group {group_index}.exr"
            if opt_force_render or not os.path.exists(bpy.path.abspath(context.scene.render.filepath)):
                nonlocal n_render_performed
                n_render_performed = n_render_performed + 1
                bpy.ops.render.render(write_still=True)
    
        initial_collections = vlm_collections.move_all_to_col(objects, tmp_col)
        context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
        context.scene.render.film_transparent = True
        perform_render("Environment")
        context.scene.world = bpy.data.worlds["VPX.Env.Black"]
        context.scene.render.film_transparent = False
        for light_col in lights_col.children:
            lights = light_col.objects
            if light_col.hide_render == False and len(lights) > 0:
                if light_col.vlmSettings.light_mode:
                    previous_light_collections = vlm_collections.move_all_to_col(lights, tmp_col)
                    perform_render(f"{vlm_utils.strip_vlm(light_col.name)}")
                    vlm_collections.restore_all_col_links(previous_light_collections)
                else:
                    for light in lights:
                        previous_light_collections = vlm_collections.move_to_col(light, tmp_col)
                        perform_render(f"{vlm_utils.strip_vlm(light_col.name)} - {vlm_utils.strip_vlm(light.name)}")
                        vlm_collections.restore_col_links(previous_light_collections)
        vlm_collections.restore_all_col_links(initial_collections)

    context.scene.world = bpy.data.worlds["VPX.Env.IBL"]
    context.scene.render.film_transparent = True
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

    # Purge unlinked datas to avoid wrong names
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    # Texture packing
    opt_padding = vlmProps.padding
    opt_tex_size = vlmProps.tex_size

    # Bake mesh generation settings
    opt_backface_limit_angle = vlmProps.remove_backface
    opt_optimize_mesh = True
    shell_size = global_scale * 0.1 # amount of extrustion for light map shell

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

    # Light bakes to be merged together
    light_merge_groups = {}
    for light_col in lights_col.children:
        if light_col.vlmSettings.light_mode:
            light_merge_groups[vlm_utils.strip_vlm(light_col.name)] = []
        else:
            for light in light_col.objects:
                light_merge_groups[f"{vlm_utils.strip_vlm(light_col.name)}-{light.name}"] = []

    n_render_groups = get_n_render_groups(context)

    # Prepare the list of lighting situation with a packmap material per render group
    light_scenarios = {"Environment": ["Environment", None, None, None]}
    for light_col in lights_col.children:
        lights = light_col.objects
        if light_col.hide_render == False and len(lights) > 0:
            if light_col.vlmSettings.light_mode:
                name = vlm_utils.strip_vlm(light_col.name)
                light_scenarios[name] = [name, light_col, None, None]
            else:
                for light in lights:
                    name = f"{vlm_utils.strip_vlm(light_col.name)}-{light.name}"
                    light_scenarios[name] = [name, light_col, light, None]
    for name, light_scenario in light_scenarios.items():
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
        bake_mesh.name = "VPX.Bake Target"
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
        override = {}
        override["area"] = area
        override["space_data"] = area.spaces.active
        override["region"] = area.regions[-1]
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Subdivide long edges to avoid visible projection distortion (recursive subdivisions)
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
                if l > 0.2:
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
                uv_layer_packed.data[loop_index].uv = (0.5 + 0.5 * (u - 0.5), v) # Account for aspect ratio changed
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
        base_density = compute_uvmap_density(bake_mesh, bake_mesh.uv_layers["UVMap"])
        print(f". {big_poly_index} big sized poly separated for better UV island packing.")
        
        # Triangulate (in the end, VPX only deals with triangles)
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
            coords.append(v.co + v.normal * v.calc_shell_factor() * shell_size)
        for v, nv in zip(bm.verts, coords):
            v.co = nv
        bm.to_mesh(light_mesh)
        bm.free()

        # Code to generate 'heatmap' on the GPU to identify which faces are influenced by a given light
        vertex_shader = '''
            in vec2 position;
            in vec2 uv;
            out vec2 uvInterp;
            void main() {
                uvInterp = uv;
                gl_Position = vec4(position, 0.0, 1.0);
            }
        '''
        fragment_shader = '''
            uniform sampler2D image;
            uniform int dotSize;
            uniform float deltaU;
            uniform float deltaV;
            in vec2 uvInterp;
            out vec4 FragColor;
            void main() {
                float v = 0.0;
                for(int x=0; x<dotSize; x++)
                    for(int y=0; y<dotSize; y++)
                        v = max(v, dot(texture(image, uvInterp + vec2(x, y) * vec2(deltaU, deltaV)).rgb, vec3(0.2989, 0.5870, 0.1140)));
                //v = step(0.01, v);
                FragColor = vec4(v, v, v, 1.0);
            }
        '''
        shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
        batch = batch_for_shader(
            shader, 'TRI_FAN',
            {
                "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
            },
        )

        def compute_heat_map(group_images, heatmap_path, heatmap_height):
            heatmap_width = max(int(heatmap_height / 2), 1)
            offscreen = gpu.types.GPUOffScreen(heatmap_width, heatmap_height)
            with offscreen.bind():
                fb = gpu.state.active_framebuffer_get()
                fb.clear(color=(0.0, 0.0, 0.0, 1.0))
                with gpu.matrix.push_pop():
                    gpu.matrix.load_matrix(mathutils.Matrix.Identity(4))
                    gpu.matrix.load_projection_matrix(mathutils.Matrix.Identity(4))
                    gpu.state.blend_set('ADDITIVE')
                    for image in group_images:
                        im_width, im_height = image.size
                        shader.bind()
                        shader.uniform_sampler("image", gpu.texture.from_image(image))
                        shader.uniform_float("deltaU", 1.0 / im_width)
                        shader.uniform_float("deltaV", 1.0 / im_height)
                        shader.uniform_int("dotSize", int(im_width / heatmap_width))
                        batch.draw(shader)
                heatmap = fb.read_color(0, 0, heatmap_width, heatmap_height, 4, 0, 'UBYTE')
                heatmap.dimensions = heatmap_width * heatmap_height * 4
            offscreen.free()
            if False: # Save the heatmap (for debug purpose only)
                heatmap_img = bpy.data.images.new("HeatMap", heatmap_width, heatmap_height)
                heatmap_img.pixels = [v / 255 for v in heatmap]
                heatmap_img.save_render(bpy.path.abspath(heatmap_path.format(heatmap_height)))
            return heatmap

        # Build bake object for each lighting situation
        for name, light_scenario in light_scenarios.items():
            print(f"\n[{bake_col.name}] Creating bake model for {name}")
            is_light = light_scenario[1] is not None
            base_filepath = f"{bakepath}Render groups/{name} - Group {{0}}.exr"
            heatmap_path = f"{bakepath}{name} - HeatMap {{0}}.png"
            if is_light:
                bake_instance = bpy.data.objects.new(f"VPX.Bake.{bake_group_name}.{name}", light_mesh.copy())
            else:
                bake_instance = bpy.data.objects.new(f"VPX.Bake.{bake_group_name}.{name}", bake_mesh.copy())
            bake_instance_mesh = bake_instance.data
            tmp_col.objects.link(bake_instance)
            bpy.ops.object.select_all(action='DESELECT')
            bake_instance.select_set(True)
            context.view_layer.objects.active = bake_instance
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            n_faces = len(bake_instance_mesh.polygons)
            for index in range(n_render_groups):
                bake_instance_mesh.materials[index] = light_scenario[3][index]

            # Remove uninfluenced faces and build an optimized model and map for lights
            # 1. For each face, evaluate the tex size that would result in a one texel lookup
            # 2. Compute heat map: low resolution of all the groups merged together, downsampled through a max filter, at the found tex size
            # 3. Check if the face is inside a cold or hot texel, if cold, select it
            # 4. Delete selected faces
            # 5. Evaluate resulting UVMap area, select output texture size accordingly, adjust UVMap to new aspect ratio, pack islands
            # 6. Render pack map
            if is_light:
                # Check all faces against the adequate heat map, generating them lazily
                bpy.ops.mesh.select_all(action='DESELECT')
                bm = bmesh.from_edit_mesh(bake_instance_mesh)
                uv_layer = bm.loops.layers.uv["UVMap"]
                n_delete = 0
                heatmaps = {}
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
                    # FIXME the following /2 leads to a heatmap LOD jump for better packing. THis is handmade and not sure to be right
                    s = max(0.5 * (xmax - xmin), ymax - ymin) / 2.0
                    if s < 1.0 / opt_tex_size:
                        s = opt_tex_size
                    else:
                        s = 1.0 / s
                    heatmap_height = 1<<(int(s)-1).bit_length() # size is the power of 2 directly greater to s
                    heatmap_width = max(1, int(heatmap_height / 2))
                    # lazily compute heatmaps
                    if heatmap_height in heatmaps:
                        heatmap = heatmaps[heatmap_height]
                    else:
                        group_images = [bpy.data.images.load(base_filepath.format(index), check_existing=True) for index in range(n_render_groups)]
                        heatmap = compute_heat_map(group_images, heatmap_path, heatmap_height)
                        heatmaps[heatmap_height] = heatmap
                    influenced = False
                    for loop in face.loops: # TODO Testing against a single loop point should be enough
                        uv = loop[uv_layer].uv
                        px = int(uv.x * heatmap_width)
                        py = int(uv.y * heatmap_height)
                        # FIXME the threshold to 2 has to validate by tests
                        if 0 <= px < heatmap_width and 0 <= py < heatmap_height and heatmap[px * 4 + py * 4 * heatmap_width] > 2:
                            influenced = True
                    if not influenced:
                        face.select = True
                        n_delete += 1
                bmesh.update_edit_mesh(bake_instance_mesh)
                if n_delete > 0:
                    bpy.ops.mesh.delete(type='FACE')
                    print(f". Mesh optimized for {name} to {n_faces-n_delete} faces out of {n_faces} faces")
            bpy.ops.object.mode_set(mode='OBJECT')

            # Compute target texture size (depends on the amount of remaining faces)
            # FIXME the texture size should be computed only when exporting or merging light maps
            density = compute_uvmap_density(bake_instance_mesh, bake_instance_mesh.uv_layers["UVMap"])
            tex_size_ratio = math.sqrt(density/base_density)
            raw_tex_size = max(1, int(tex_size_ratio * opt_tex_size))
            tex_size = 1<<(int(raw_tex_size)-1).bit_length()
            if raw_tex_size != tex_size and raw_tex_size * raw_tex_size < 1.1 * 0.25 * tex_size * tex_size:
                tex_size = tex_size / 2 # if we need less than 10% of the upper size, use the one just below
            tex_width = tex_height = tex_size

            # Pack UV map (only if this bake mesh won't be merged afterward)
            if not is_light:
                if bake_mode == 'playfield':
                    uv_layer_packed = bake_instance_mesh.uv_layers["UVMap Packed"]
                    uv_layer_packed.active = True
                    l, t, w, h = context.scene.vlmSettings.playfield_size
                    for loop in bake_instance_mesh.loops:
                        pt = bake_instance_mesh.vertices[loop.vertex_index].co
                        uv_layer_packed.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
                    tex_height = raw_tex_size
                    tex_width = raw_tex_size * w / h
                else:
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.uv.select_all(action='SELECT')
                    bpy.ops.uv.pack_islands(margin=opt_padding / opt_tex_size)
                    bpy.ops.object.mode_set(mode='OBJECT')
                    packed_density = compute_uvmap_density(bake_instance_mesh, bake_instance_mesh.uv_layers["UVMap Packed"])
                    print(f". UVMap for {name} packed to a density of {packed_density:.1%}.")
                bake_results.append(bake_instance)
                print(f". Texture for {name} adjusted to {tex_width}x{tex_height} (ratio of {(tex_width*tex_height/(raw_tex_size * raw_tex_size)):.0%})")

            # Save in result collection
            bake_instance.vlmSettings.bake_name = name
            bake_instance.vlmSettings.bake_is_light = is_light
            bake_instance.vlmSettings.bake_tex_factor = tex_size_ratio
            if is_light:
                light_merge_groups[name].append(bake_instance)
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
            for obj in bakes:
                obj.select_set(True)
            bpy.ops.object.join()
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')
            bpy.ops.uv.pack_islands(margin=opt_padding / opt_tex_size)
            bpy.ops.object.mode_set(mode='OBJECT')
            bake_instance = context.view_layer.objects.active
            packed_density = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap Packed"])
            print(f". UVMap for {name} packed to a density of {packed_density:.1%}.")
            bake_results.append(bake_instance)

    # Purge unlinked datas
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f"\nbake meshes created in {int(time.time() - start_time)}s.")


def full_bake(context):
    pass