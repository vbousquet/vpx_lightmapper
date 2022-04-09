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


def create_bake_meshes(op, context):
    """Create all bake meshes, building from the render groups and renders cached during the previous steps
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before creating bake meshes')
        return {'CANCELLED'}
        
    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

    ctx_area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if not ctx_area:
        op.report({'ERROR'}, 'This operator must be used with a 3D view active')
        return {'CANCELLED'}
    ctx_area.regions[-1].data.view_perspective = 'CAMERA'

    root_bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not root_bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
    if not light_col:
        op.report({'ERROR'}, "No 'VLM.Lights' collection to process")
        return {'CANCELLED'}

    camera = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    print("\nCreating all bake meshes")
    start_time = time.time()
    global_scale = vlm_utils.get_global_scale(context)

    vlmProps = context.scene.vlmSettings
    n_render_groups = vlm_utils.get_n_render_groups(context)
    cursor_loc = context.scene.cursor.location
    context.scene.cursor.location = camera.location # Used for sorting faces by distance from view point
    result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result')

    # Purge unlinked datas to avoid wrong names
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    # Texture packing
    opt_padding = vlmProps.padding
    opt_tex_size = int(vlmProps.tex_size)
    opt_ar = vlmProps.render_aspect_ratio

    # Bake mesh generation settings
    opt_backface_limit_angle = vlmProps.remove_backface
    opt_vpx_reflection = vlmProps.keep_pf_reflection_faces
    opt_optimize_mesh = True
    shell_size = global_scale * 0.1 # amount of extrustion for light map shell
    opt_lightmap_prune_res = min(256, opt_tex_size) # resolution used in the algorithm for unlit face pruning (artefact observed at 256)

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
    
    # Prepare the list of lighting situation with a packmap material per render group, and a merge group per light situation
    light_merge_groups = {}
    light_scenarios = vlm_utils.get_lightings(context)
    for name, light_scenario in light_scenarios.items():
        light_merge_groups[name] = []
        mats = []
        packmat = bpy.data.materials["VPX.Core.Mat.PackMap"]
        for index in range(n_render_groups):
            mat = packmat.copy()
            mat.name = f"VPX.PM.{name}.RG{index}"
            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
            if light_scenario[1]:
                mat.blend_method = 'BLEND'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.blend_method = 'OPAQUE'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
            mats.append(mat)
        light_scenario[4] = mats

    bake_meshes = [] # one per bake group
    for bake_col in root_bake_col.children:
        bake_group_name = vlm_utils.strip_vlm(bake_col.name)
        bake_mode = bake_col.vlmSettings.bake_mode
        
        # FIXME implement movable baking
        if bake_mode == 'movable':
            continue
    
        # Join all objects to build baked objects (converting to mesh, and preserving split normals)
        print(f"\nBuilding base bake target model for '{bake_col.name}' bake group")
        poly_start = 0
        objects_to_join = []
        bake_col_object_set = sorted({obj.vlmSettings.bake_to.name if obj.vlmSettings.bake_to else obj.name for obj in bake_col.objects if not obj.hide_render and not obj.vlmSettings.indirect_only})
        for i, obj_name in enumerate(bake_col_object_set):
            print(f". Adding #{i+1}/{len(bake_col_object_set)}: {obj_name}")
            dup = bpy.data.objects[obj_name].copy()
            dup.data = dup.data.copy()
            result_col.objects.link(dup)
            dup_name = dup.name
            if dup.type != 'MESH':
                bpy.ops.object.select_all(action='DESELECT')
                dup.select_set(True)
                context.view_layer.objects.active = dup
                bpy.ops.object.convert(target='MESH')
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.1))
                bpy.ops.object.mode_set(mode='OBJECT')
                dup = bpy.data.objects[dup_name]
            dup.data.free_normals_split()
            dup.data.use_auto_smooth = False
            dup.data.materials.clear()
            dup.data.validate()
            for i in range(n_render_groups):
                dup.data.materials.append(list(light_scenarios.values())[0][4][i])
            for poly in dup.data.polygons:
                poly.material_index = dup.vlmSettings.render_group
            override = context.copy()
            override["object"] = override["active_object"] = dup
            override["selected_objects"] = override["selected_editable_objects"] = [dup]
            for modifier in dup.modifiers:
                if modifier.type == 'BEVEL' and modifier.width < 0.1: break
                bpy.ops.object.modifier_apply(override, modifier=modifier.name)
            dup = bpy.data.objects[dup_name]
            dup.modifiers.clear()
            dup.data.transform(dup.matrix_basis)
            for c in dup.children:
                c.matrix_local = dup.matrix_basis @ c.matrix_local
            dup.matrix_basis.identity()
            objects_to_join.append(dup)
        
        if len(objects_to_join) == 0: continue
        
        # Create joined mesh
        bake_mesh = bpy.data.meshes.new('VLM.Bake Target')
        bake_mesh.materials.clear()
        for index in range(n_render_groups):
            bake_mesh.materials.append(list(light_scenarios.values())[0][4][i])
        bm = bmesh.new()
        poly_start = 0
        for obj in objects_to_join:
            bm.from_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            poly_end = len(bm.faces)
            for poly in range(poly_start, poly_end):
                bm.faces[poly].material_index  = obj.vlmSettings.render_group
            poly_start = poly_end
            result_col.objects.unlink(obj)
        bm.to_mesh(bake_mesh)
        bm.free()
        bake_mesh.validate()
        bake_target = bpy.data.objects.new('VLM.Bake Target', bake_mesh)
        bake_target_name = bake_target.name
        result_col.objects.link(bake_target)
        
        # override = context.copy()
        # objects_to_join[0].name = 'VLM.Bake Target'
        # bake_target_name = objects_to_join[0].name
        # override["object"] = override["active_object"] = objects_to_join[0]
        # override["selected_objects"] = override["selected_editable_objects"] = objects_to_join
        # bpy.ops.object.join(override)
        
        bake_target = bpy.data.objects[bake_target_name]
        override["object"] = override["active_object"] = bake_target
        override["selected_objects"] = override["selected_editable_objects"] = [bake_target]
        bpy.ops.object.shade_flat(override)

        bpy.ops.object.select_all(action='DESELECT')
        bake_target.select_set(True)
        context.view_layer.objects.active = bake_target
        bpy.ops.object.mode_set(mode = 'EDIT')
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.object.mode_set(mode = 'OBJECT')
        print(f". Objects merged ({len(bake_target.data.vertices)} vertices, {len(bake_target.data.polygons)} untriangulated faces)")
        
        # Remove backfacing faces
        if opt_backface_limit_angle < 90.0:
            bake_target = bpy.data.objects[bake_target_name]
            dot_limit = math.cos(radians(opt_backface_limit_angle + 90))
            bpy.ops.object.mode_set(mode = 'EDIT')
            bm = bmesh.from_edit_mesh(bake_target.data)
            bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
            bm.faces.ensure_lookup_table()
            n_faces = len(bm.faces)
            faces = []
            for face in bm.faces:
                normal = face.normal
                if normal.length_squared < 0.5:
                    #face.select_set(False)
                    pass
                else:
                    face_center = face.calc_center_bounds()
                    dot_value = normal.dot(camera.location - face_center)
                    if dot_value >= dot_limit:
                        #face.select_set(False)
                        pass
                    elif opt_vpx_reflection:
                        # To support VPX reflection, check visibility from the playfield reflected ray
                        face_center.z = -face_center.z
                        reflected = (face_center - camera.location).normalized() # ray from eye to reflection of the face
                        reflected.z = -reflected.z
                        dot_value = -normal.dot(reflected) # negate since this is an incoming vector toward the face
                        #face.select_set(dot_value < dot_limit)
                        if dot_value < dot_limit: faces.append(face)
                    else:
                        #face.select_set(True)
                        faces.append(face)
            bmesh.ops.delete(bm, geom=faces, context='FACES')
            bmesh.update_edit_mesh(bake_target.data)
            #bpy.ops.mesh.delete(type='FACE')
            bpy.ops.object.mode_set(mode = 'OBJECT') 
            bake_target = bpy.data.objects[bake_target_name]
            print(f". {n_faces - len(bake_target.data.polygons)} backfacing faces removed (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")

        # Clean up and simplify mesh (except for playfield mesh)
        bake_target = bpy.data.objects[bake_target_name]
        n_faces = len(bake_target.data.polygons)
        if bake_mode.startswith('playfield'):
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
        bake_target = bpy.data.objects[bake_target_name]
        print(f". {n_faces - len(bake_target.data.polygons)} faces removed during cleanup (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")

        # Triangulate (in the end, VPX only deals with triangles, and this simplify the lightmap pruning process)
        bake_target = bpy.data.objects[bake_target_name]
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(bake_target.data)
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
        bmesh.update_edit_mesh(bake_target.data)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Compute base UV Map projection matching the render
        bake_target = bpy.data.objects[bake_target_name]
        uvs = [uv for uv in bake_target.data.uv_layers]
        while uvs:
            bake_target.data.uv_layers.remove(uvs.pop())
        uv_layer = bake_target.data.uv_layers.new(name='UVMap')
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        override = context.copy()
        override["object"] = override["active_object"] = bake_target
        override["selected_objects"] = override["selected_editable_objects"] = [bake_target]
        override["area"] = ctx_area
        override["space_data"] = ctx_area.spaces.active
        override["region"] = ctx_area.regions[-1]
        bpy.ops.uv.project_from_view(override)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Subdivide long edges to avoid visible projection distortion, and allow better lightmap face pruning (recursive subdivisions)
        opt_cut_threshold = 0.02 if bake_mode.startswith('playfield') else 0.1 # 0.2 seems sufficient for distortion, lower value is needed for lightmap face pruning especially on playfield for inserts
        for i in range(8):
            bake_target = bpy.data.objects[bake_target_name]
            bme = bmesh.new()
            bme.from_mesh(bake_target.data)
            bme.edges.ensure_lookup_table()
            bme.faces.ensure_lookup_table()
            bme.verts.ensure_lookup_table()
            long_edges = []
            longest_edge = 0
            uv_layer = bme.loops.layers.uv.verify()
            for edge in bme.edges:
                if len(edge.verts[0].link_loops) < 1 or len(edge.verts[1].link_loops) < 1:
                    continue
                ua, va = edge.verts[0].link_loops[0][uv_layer].uv
                ub, vb = edge.verts[1].link_loops[0][uv_layer].uv
                l = math.sqrt((ub-ua)*(ub-ua)*opt_ar*opt_ar+(vb-va)*(vb-va))
                longest_edge = max(longest_edge, l)
                if l >= opt_cut_threshold:
                    long_edges.append(edge)
            if not long_edges:
                bme.to_mesh(bake_target.data)
                bme.free()
                bake_target.data.update()
                break
            bmesh.ops.subdivide_edges(bme, edges=long_edges, cuts=1, use_grid_fill=True)
            bmesh.ops.triangulate(bme, faces=bme.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
            bme.to_mesh(bake_target.data)
            bme.free()
            bake_target.data.update()
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            override = context.copy()
            override["object"] = override["active_object"] = bake_target
            override["selected_objects"] = override["selected_editable_objects"] = [bake_target]
            override["area"] = ctx_area
            override["space_data"] = ctx_area.spaces.active
            override["region"] = ctx_area.regions[-1]
            bpy.ops.uv.project_from_view(override)
            bpy.ops.object.mode_set(mode='OBJECT')
            print(f". {len(long_edges):>5} edges subdivided to avoid projection distortion and better lightmap pruning (length threshold: {opt_cut_threshold}, longest edge: {longest_edge:4.2}).")
        
        # Separate big polys (out of there natural island) for better packing
        bake_target = bpy.data.objects[bake_target_name]
        bake_mesh = bake_target.data
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
        result_col.objects.unlink(bake_target)

    # Merge bake mesh accross bake groups for the light mesh, then extrude it along normal according to its shell factor, and compute its visibility map
    prunemap_width = int(opt_lightmap_prune_res * opt_ar)
    prunemap_height = opt_lightmap_prune_res
    print(f'\nCreating base lightmap meshes (prune map size={prunemap_width}x{prunemap_height})')
    light_mesh = bpy.data.meshes.new('LightMesh')
    bm = bmesh.new()
    for bake_col, bake_mesh in bake_meshes:
        bm.from_mesh(bake_mesh)
    coords = []
    for v in bm.verts:
        #coords.append(v.co + v.normal * shell_size * min(v.calc_shell_factor(), 10.0))
        coords.append(v.co) # no shell, just use exactly the same coordinates
    for v, nv in zip(bm.verts, coords):
        v.co = nv
    bm.to_mesh(light_mesh)
    bm.free()
    for index in range(n_render_groups):
        light_mesh.materials.append(list(light_scenarios.values())[0][4][index])
    obj = bpy.data.objects.new(f"LightMesh", light_mesh)
    result_col.objects.link(obj)
    bpy.ops.object.select_all(action='DESELECT')
    context.view_layer.objects.active = obj
    obj.select_set(True)
    lightmap_vmap = build_visibility_map(light_mesh, n_render_groups, prunemap_width, prunemap_height)
    result_col.objects.unlink(obj)

    bake_results = []
    print(f'\nOptimizing lightmap meshes (prune map size={prunemap_width}x{prunemap_height})')
    for i, (name, light_scenario) in enumerate(light_scenarios.items(), start = 1):
        _, is_lightmap, _, lights, materials = light_scenario
        if is_lightmap:
            bake_instance = bpy.data.objects.get(f'LM.{name}')
            if bake_instance:
                vlm_collections.unlink(bake_instance)
                bake_instance.data = light_mesh.copy()
            else:
                bake_instance = bpy.data.objects.new(f'LM.{name}', light_mesh.copy())
            for index in range(n_render_groups):
                bake_instance.data.materials[index] = materials[index]
            result_col.objects.link(bake_instance)
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bake_instance
            bake_instance.select_set(True)
            hdr_range = prune_lightmap_by_visibility_map(bake_instance.data, vlm_utils.get_bakepath(context, type='RENDERS'), name, n_render_groups, lightmap_vmap, prunemap_width, prunemap_height)
            if not bake_instance.data.polygons:
                result_col.objects.unlink(bake_instance)
                print(f". Mesh {name} has no more faces after optimization")
            bake_instance.vlmSettings.bake_hdr_scale = hdr_range
            bake_instance.vlmSettings.bake_name = name
            bake_instance.vlmSettings.bake_type = 'lightmap'
            if lights: bake_instance.vlmSettings.bake_light = lights[0].vlmSettings.vpx_object
            bake_instance.vlmSettings.bake_tex_factor = compute_uvmap_density(bake_instance.data, bake_instance.data.uv_layers["UVMap"])
            bake_results.append(bake_instance)
        else:
            for bake_col, bake_mesh in bake_meshes:
                bake_group_name = vlm_utils.strip_vlm(bake_col.name)
                bake_instance = bpy.data.objects.get(f'BM.{name}.{bake_group_name}')
                if bake_instance:
                    vlm_collections.unlink(bake_instance)
                    bake_instance.data = bake_mesh.copy()
                else:
                    bake_instance = bpy.data.objects.new(f'BM.{name}.{bake_group_name}', bake_mesh.copy())
                for index in range(n_render_groups):
                    bake_instance.data.materials[index] = materials[index]
                bake_instance.vlmSettings.bake_name = name
                bake_instance.vlmSettings.bake_objects = bake_group_name
                bake_instance.vlmSettings.bake_hdr_scale = 1.0
                if bake_col.vlmSettings.bake_mode.startswith('playfield'):
                    uv_layer = bake_instance.data.uv_layers['UVMap']
                    uv_layer_packed = bake_instance.data.uv_layers['UVMap Packed']
                    uv_layer_packed.active = True
                    l, t, w, h = context.scene.vlmSettings.playfield_size
                    for loop in bake_instance.data.loops:
                        pt = bake_instance.data.vertices[loop.vertex_index].co
                        uv_layer_packed.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
                        if bake_col.vlmSettings.bake_mode == 'playfield_fv': uv_layer.data[loop.index].uv = uv_layer_packed.data[loop.index].uv
                    bake_instance.vlmSettings.bake_tex_factor = 1
                    bake_instance.vlmSettings.bake_type = 'playfield'
                    bake_instance.vlmSettings.bake_light = ''
                    if bake_col.vlmSettings.bake_mode == 'playfield_fv':
                        bake_instance.vlmSettings.bake_type = 'playfield_fv'
                        bake_instance.data.materials.clear()
                        mat = bpy.data.materials["VPX.Core.Mat.PackMap"].copy()
                        mat.name = f"VPX.PM.Playfield.FV"
                        mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
                        mat.blend_method = 'OPAQUE'
                        mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
                        mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
                        bake_instance.data.materials.append(mat)
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

    # Sort from higher texture fill factor to lowest, then fillup packmap buckets
    print(f"\nMerging and packing UV maps")
    bake_results.sort(key=lambda obj: obj.vlmSettings.bake_tex_factor, reverse=True)
    packmaps = []
    lattice = bpy.data.objects.get('Layback')
    for bake in bake_results:
        if lattice: bake.modifiers.new('Layback', 'LATTICE').object = lattice
        bake_density = bake.vlmSettings.bake_tex_factor
        if bake.vlmSettings.bake_type == 'playfield':
            bake.vlmSettings.bake_packmap = len(packmaps)
            packmaps.append(([bake], 1, int(opt_tex_size/2), opt_tex_size, True, False))
            bake.vlmSettings.bake_packmap_width = int(opt_tex_size/2)
            bake.vlmSettings.bake_packmap_height = opt_tex_size
        elif bake.vlmSettings.bake_type == 'playfield_fv':
            bake.vlmSettings.bake_packmap = len(packmaps)
            packmaps.append(([bake], 1, int(opt_tex_size * opt_ar), opt_tex_size, True, False))
            bake.vlmSettings.bake_packmap_width = int(opt_tex_size * opt_ar)
            bake.vlmSettings.bake_packmap_height = opt_tex_size
        else:
            is_bake_hdr = bake_instance.vlmSettings.bake_hdr_scale > 1.0
            for index, (bakes, density, _, _, is_playfield, is_hdr) in enumerate(packmaps):
                if not is_playfield and density + bake_density <= 1 and is_bake_hdr == is_hdr:
                    bake.vlmSettings.bake_packmap = index
                    packmaps[index] = (bakes + [bake], density + bake_density, -1, -1, is_playfield, is_hdr)
                    bake_density = 0
                    break
            if bake_density > 0:
                bake.vlmSettings.bake_packmap = len(packmaps)
                packmaps.append(([bake], bake_density, -1, -1, False, is_bake_hdr))
    max_level = max(0, opt_tex_size.bit_length() - 1)
    for index, (bakes, density, w, h, is_playfield, is_hdr) in enumerate(packmaps):
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
            packmaps[index] = (bakes, density, tex_width, tex_height, is_playfield, is_hdr)
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
    for index, (bakes, density, w, h, is_playfield, is_hdr) in enumerate(packmaps):
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
            print(f'.  Packmap #{index}: {density:>6.2%} target density for {len(bakes)} objects => {w:>4}x{h:>4} texture size with {packed_density:>6.2%} effective packed density, HDR: {is_hdr}')

    # Purge unlinked datas and clean up
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f"\nbake meshes created in {str(datetime.timedelta(seconds=time.time() - start_time))}")

    context.scene.vlmSettings.last_bake_step = 'meshes'
    return {'FINISHED'}


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
        min_x = max(0, min(width - 1, min(ax, bx, cx)))
        min_y = max(0, min(height - 1, min(ay, by, cy)))
        max_x = max(0, min(width - 1, max(ax, bx, cx)))
        max_y = max(0, min(height - 1, max(ay, by, cy)))
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
        marked = False
        for y in range(min_y, max_y + 1):
            w0 = w0_row
            w1 = w1_row
            w2 = w2_row
            for x in range(min_x, max_x + 1):
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    marked = True
                    vmaps[x + y * width].append(face.index)
                w0 += A12
                w1 += A20
                w2 += A01
            w0_row += B12
            w1_row += B20
            w2_row += B01
        if not marked: # for triangles that occupy less than one pixel in the visibility map
            vmaps[min_x + min_y * width].append(face.index)
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
            else:
                print(f'. No render for {path_exr}')
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
        if bw[4 * xy] > 0:
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
