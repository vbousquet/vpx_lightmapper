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
from mathutils import Matrix
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency


def create_bake_meshes(op, context):
    """Create all bake meshes, building from the render groups and renders cached during the previous steps
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before creating bake meshes')
        return {'CANCELLED'}
        
    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

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
    n_render_groups = vlm_utils.get_n_render_groups(context)
    cursor_loc = context.scene.cursor.location
    context.scene.cursor.location = camera.location # Used for sorting faces by distance from view point
    result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result')
    lc = vlm_collections.find_layer_collection(context.view_layer.layer_collection, result_col)
    if lc: lc.exclude = False

    # Purge unlinked datas to avoid wrong names
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    # Texture packing
    opt_padding = context.scene.vlmSettings.padding
    render_size = vlm_utils.get_render_size(context)
    proj_x = render_size[0] * context.scene.render.pixel_aspect_x
    proj_y = render_size[1] * context.scene.render.pixel_aspect_y
    opt_tex_size = int(context.scene.vlmSettings.render_height)
    opt_ar = context.scene.vlmSettings.render_aspect_ratio

    # Bake mesh generation settings
    opt_backface_limit_angle = context.scene.vlmSettings.remove_backface
    opt_limited_dissolve_limit = radians(1) # Test with 5 degrees resulted in artefact for long ball guide (on Warlok table)
    opt_merge_double_limit = 0.001 * global_scale
    opt_vpx_reflection = context.scene.vlmSettings.keep_pf_reflection_faces
    opt_lod_threshold = 16 * opt_tex_size / 4096  # start LOD for biggest face below 16x16 pixels for 4K renders (1 pixel for 256px renders)
    #opt_lod_threshold = 0 # Disable LOD
    opt_lod_threshold = int(opt_lod_threshold * opt_lod_threshold)
    opt_lightmap_prune_res = min(256, opt_tex_size) # resolution used in the algorithm for unlit face pruning (artefact observed at 256)
    prunemap_width = int(opt_lightmap_prune_res * opt_ar)
    prunemap_height = opt_lightmap_prune_res

    # Delete existing results
    to_delete = [obj for obj in result_col.all_objects]
    for obj in to_delete:
        bpy.data.objects.remove(obj, do_unlink=True)

    # Append core material (used to preview)
    if "VPX.Core.Mat.PackMap" not in bpy.data.materials:
        librarypath = vlm_utils.get_library_path()
        if not os.path.isfile(librarypath):
            op.report({'WARNING'},f"{librarypath} does not exist")
            return {'CANCELLED'}
        with bpy.data.libraries.load(librarypath, link=False) as (data_from, data_to):
            data_to.objects = data_from.objects
            data_to.materials = [name for name in data_from.materials if name == "VPX.Core.Mat.PackMap"]
            data_to.node_groups = data_from.node_groups
    
    # Prepare the list of lighting situation with a packmap material per render group, and a merge group per light situation
    light_merge_groups = {}
    light_scenarios = vlm_utils.get_lightings(context)
    #light_scenarios = [l for l in light_scenarios if l[0] == 'Inserts-L8'] # Debug: For quickly testing a single light scenario
    for light_scenario in light_scenarios:
        name = light_scenario[0]
        light_merge_groups[name] = []
        mats = []
        packmat = bpy.data.materials["VPX.Core.Mat.PackMap"]
        for index in range(n_render_groups):
            mat = packmat.copy()
            mat.name = f"VPX.PM.{name}.RG{index}"
            if light_scenario[1]:
                mat.blend_method = 'BLEND'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.blend_method = 'OPAQUE'
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mats.append(mat)
        light_scenario[4] = mats

    # Prepare the list of solid bake mesh to produce
    to_bake = []
    for bake_col in root_bake_col.children:
        object_names = sorted({obj.vlmSettings.bake_to.name if obj.vlmSettings.bake_to else obj.name for obj in bake_col.objects if not obj.hide_render and not obj.vlmSettings.indirect_only})
        if bake_col.vlmSettings.bake_mode == 'split':
            for obj_name in object_names:
                to_bake.append((obj_name, bake_col, [obj_name], obj_name, not bake_col.vlmSettings.is_opaque))
        else:
            sync_obj = None
            for obj_name in object_names:
                obj = bpy.data.objects[obj_name]
                if obj.vlmSettings.movable_script != '':
                    if sync_obj != None:
                        print(f'. ERROR: Bake collection {bake_col.name} bakes to a group but more than one object defines a move script.')
                    sync_obj = obj_name
            to_bake.append((bake_col.name, bake_col, object_names, sync_obj, not bake_col.vlmSettings.is_opaque))
        
    # Create all solid bake meshes
    bake_meshes = []
    for bake_name, bake_col, bake_col_object_set, sync_obj, is_translucent in to_bake:
        # Join all objects to build baked objects (converting to mesh, and preserving split normals)
        print(f"\nBuilding solid bake target model for '{bake_name}'")
        poly_start = 0
        objects_to_join = []
        last_obj = None
        for i, obj_name in enumerate(bake_col_object_set):
            dup = bpy.data.objects[obj_name].copy()
            dup.data = dup.data.copy()
            result_col.objects.link(dup)
            bpy.ops.object.select_all(action='DESELECT')
            dup.select_set(True)
            context.view_layer.objects.active = dup
            dup_name = dup.name
            if dup.type != 'MESH':
                bpy.ops.object.convert(target='MESH')
                dup = bpy.data.objects[dup_name]
            dup.data.free_normals_split()
            dup.data.use_auto_smooth = False
            dup.data.materials.clear()
            dup.data.validate()
            for j in range(n_render_groups):
                dup.data.materials.append(light_scenarios[0][4][j])
            for poly in dup.data.polygons:
                poly.material_index = dup.vlmSettings.render_group
            override = context.copy()
            override["object"] = override["active_object"] = dup
            override["selected_objects"] = override["selected_editable_objects"] = [dup]
            for modifier in dup.modifiers:
                if 'NoExp' in modifier.name: break # or (modifier.type == 'BEVEL' and modifier.width < 0.1)
                bpy.ops.object.modifier_apply(override, modifier=modifier.name)
            dup = bpy.data.objects[dup_name]
            dup.modifiers.clear()
            # Create base UV projected layer
            uvs = [uv for uv in dup.data.uv_layers]
            while uvs:
                dup.data.uv_layers.remove(uvs.pop())
            dup.data.uv_layers.new(name='UVMap')
            vlm_utils.project_uv(camera, dup, proj_x, proj_y)
            # Apply base transform
            dup.data.transform(dup.matrix_basis)
            dup.matrix_basis.identity()
            # Perform base mesh optimization
            # with context.temp_override(active_object=dup, selected_objects=dup):
            context.view_layer.objects.active = dup
            bpy.ops.object.mode_set(mode = 'EDIT')
            bpy.ops.mesh.reveal()
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold = opt_merge_double_limit)
            bpy.ops.mesh.dissolve_limited(angle_limit = opt_limited_dissolve_limit)
            bpy.ops.mesh.delete_loose()
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.object.mode_set(mode = 'OBJECT')
            # Optimize mesh: usual cleanup and evaluate biggest face size in pixels for decimate LOD
            bm = bmesh.new()
            bm.from_mesh(dup.data)
            bm.faces.ensure_lookup_table()
            uv_loop = bm.loops.layers.uv[0]
            triangle_loops = bm.calc_loop_triangles()
            areas = {face: 0.0 for face in bm.faces} 
            for loop in triangle_loops:
                areas[loop[0].face] += vlm_utils.tri_area( *(Vector( (*l[uv_loop].uv, 0) ) for l in loop) )
            bm.free()
            max_size = int(max(areas.values()) * proj_x * proj_y)
            if max_size < opt_lod_threshold:
                ratio = math.sqrt(max_size / opt_lod_threshold)
                bpy.ops.object.mode_set(mode = 'EDIT')
                bpy.ops.mesh.decimate(ratio=ratio)
                bpy.ops.object.mode_set(mode = 'OBJECT')
                print(f'. Object #{i+1:>3}/{len(bake_col_object_set):>3}: {obj_name} was decimated using a ratio of {ratio:.2%} from {len(areas)} to {len(dup.data.polygons)} faces')
            else:
                print(f'. Object #{i+1:>3}/{len(bake_col_object_set):>3}: {obj_name} was added (no LOD since max face size is {max_size:>8}px² with a threshold of {opt_lod_threshold}px²)')
            objects_to_join.append(dup)
        if len(objects_to_join) == 0: continue
        
        # Create merged mesh
        bake_mesh = bpy.data.meshes.new('VLM.Bake Target')
        bake_mesh.materials.clear()
        for j in range(n_render_groups):
            bake_mesh.materials.append(light_scenarios[0][4][j])
        bm = bmesh.new()
        poly_start = 0
        for obj in objects_to_join:
            bm.from_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            poly_end = len(bm.faces)
            for poly in range(poly_start, poly_end):
                bm.faces[poly].material_index = max(0, obj.vlmSettings.render_group)
            poly_start = poly_end
            result_col.objects.unlink(obj)
        bm.to_mesh(bake_mesh)
        bm.free()
        bake_mesh.validate()
        bake_target = bpy.data.objects.new('VLM.Bake Target', bake_mesh)
        bake_target_name = bake_target.name
        result_col.objects.link(bake_target)

        is_spinner = use_obj_pos = False
        if len(objects_to_join) == 1:
            is_spinner = objects_to_join[0].vlmSettings.is_spinner
            use_obj_pos = objects_to_join[0].vlmSettings.use_obj_pos
        
        bake_target = bpy.data.objects[bake_target_name]
        override["object"] = override["active_object"] = bake_target
        override["selected_objects"] = override["selected_editable_objects"] = [bake_target]
        bpy.ops.object.shade_flat(override)

        # if bake_name == 'Parts':
            # return {'FINISHED'}

        bpy.ops.object.select_all(action='DESELECT')
        bake_target.select_set(True)
        context.view_layer.objects.active = bake_target
        bpy.ops.object.mode_set(mode = 'EDIT')
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.object.mode_set(mode = 'OBJECT')
        print(f". Objects merged ({len(bake_target.data.vertices)} vertices, {len(bake_target.data.polygons)} untriangulated faces)")
        
        # Remove backfacing faces
        if sync_obj is None and opt_backface_limit_angle < 90.0:
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
                    pass
                else:
                    face_center = face.calc_center_bounds()
                    dot_value = normal.dot((camera.location - face_center).normalized())
                    if dot_value >= dot_limit:
                        pass
                    elif opt_vpx_reflection:
                        # To support VPX reflection, check visibility from the playfield reflected ray
                        face_center.z = -face_center.z
                        reflected = (face_center - camera.location).normalized() # ray from eye to reflection of the face
                        reflected.z = -reflected.z
                        dot_value = -normal.dot(reflected) # negate since this is an incoming vector toward the face
                        if dot_value < dot_limit: faces.append(face)
                    else:
                        faces.append(face)
            bmesh.ops.delete(bm, geom=faces, context='FACES')
            bmesh.update_edit_mesh(bake_target.data)
            bpy.ops.object.mode_set(mode = 'OBJECT') 
            bake_target = bpy.data.objects[bake_target_name]
            print(f". {n_faces - len(bake_target.data.polygons)} backfacing faces removed (model has {len(bake_target.data.vertices)} vertices and {len(bake_target.data.polygons)} faces)")

        # Clean up and simplify merged mesh
        bake_target = bpy.data.objects[bake_target_name]
        n_faces = len(bake_target.data.polygons)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.remove_doubles(threshold = opt_merge_double_limit)
        #bpy.ops.mesh.dissolve_limited(angle_limit = opt_limited_dissolve_limit) # don't do it twice: it's not worth and it makes it impossible to mimic in the render stage through modifiers
        bpy.ops.mesh.delete_loose()
        bpy.ops.mesh.select_all(action='SELECT')
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

        # Subdivide long edges to avoid visible projection distortion, and allow better lightmap face pruning (recursive subdivisions)
        if not is_spinner:
            opt_cut_threshold = 0.1
            bake_target = bpy.data.objects[bake_target_name]
            for i in range(8): # FIXME Limit the amount since there are situations were subdividing fails
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
                vlm_utils.project_uv(camera, bake_target, proj_x, proj_y)
                print(f". {len(long_edges):>5} edges subdivided to avoid projection distortion and better lightmap pruning (length threshold: {opt_cut_threshold}, longest edge: {longest_edge:4.2}).")
        
        # Sort front to back faces if opaque, back to front for translucent
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.sort_elements(type='CURSOR_DISTANCE', elements={'VERT', 'FACE'}, reverse=is_translucent)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Add a white vertex color layer for lightmap seam fading
        if not bake_mesh.vertex_colors:
            bake_mesh.vertex_colors.new()
        
        print(f'. Base solid mesh has {len(bake_mesh.polygons)} tris and {len(bake_mesh.vertices)} vertices')
        bake_meshes.append((bake_col, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos))
        result_col.objects.unlink(bake_target)

        # Save solid bake to the result collection
        for light_scenario in light_scenarios:
            light_name, is_lightmap, _, lights, materials = light_scenario
            if is_lightmap: continue
            obj_name = f'{bake_name}.BM.{light_name}' # if sync_obj else f'Table.BM.{light_name}.{bake_name}'
            bake_mesh = bake_mesh.copy()
            bake_instance = bpy.data.objects.new(obj_name, bake_mesh)
            if sync_obj:
                dup = bpy.data.objects[sync_obj]
                bake_mesh.transform(Matrix(dup.matrix_basis).inverted())
                bake_instance.matrix_basis = dup.matrix_basis
            else:
                bake_instance.matrix_basis.identity()
            for index in range(n_render_groups):
                bake_instance.data.materials[index] = materials[index]
            bake_instance.vlmSettings.bake_lighting = light_name
            bake_instance.vlmSettings.bake_objects = bake_col.name
            bake_instance.vlmSettings.bake_hdr_scale = 1.0
            bake_instance.vlmSettings.bake_sync_light = ''
            bake_instance.vlmSettings.bake_sync_trans = sync_obj if sync_obj is not None else ''
            bake_instance.vlmSettings.is_spinner = is_spinner
            bake_instance.vlmSettings.use_obj_pos = use_obj_pos
            if is_translucent:
                bake_instance.vlmSettings.bake_type = 'active'
            elif sync_obj is None:
                bake_instance.vlmSettings.bake_type = 'static'
            else:
                bake_instance.vlmSettings.bake_type = 'default'
            result_col.objects.link(bake_instance)
    
    # Merge opaque bake meshes with the same depth bias
    merged_bake_meshes = []
    opaque_bake_mesh = None
    for bake_col, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos in bake_meshes:
        if bake_col.vlmSettings.is_opaque and sync_obj is None:
            if opaque_bake_mesh:
                merged_bake_meshes.remove(opaque_bake_mesh)
                ex_bake_col, ex_bake_name, ex_bake_mesh, ex_sync_obj, ex_spinner, ex_use_obj_pos = opaque_bake_mesh
                print(f'\nMerging lightmaps for {ex_bake_col} and {bake_col.name}')
                bm = bmesh.new()
                bm.from_mesh(ex_bake_mesh)
                bm.from_mesh(bake_mesh)
                bm.to_mesh(bake_mesh)
                bm.free()
                opaque_bake_mesh = (f'{ex_bake_col};{bake_col.name}', ex_bake_name, bake_mesh, None, is_spinner and ex_spinner, use_obj_pos and ex_use_obj_pos)
                merged_bake_meshes.append(opaque_bake_mesh)
            else:
                opaque_bake_mesh = (bake_col.name, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos)
                merged_bake_meshes.append(opaque_bake_mesh)
        else:
            merged_bake_meshes.append( (bake_col.name, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos) )
    
    # Build all the visibility maps
    vmaps = []
    print(f'\nBuilding all lightmap meshes (prune map size={prunemap_width}x{prunemap_height})')
    for bake_col, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos in merged_bake_meshes:
        print(f'. Building lightmap meshes for {bake_name}')
        obj = bpy.data.objects.new(f"LightMesh", bake_mesh)
        result_col.objects.link(obj)
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = obj
        obj.select_set(True)
        lightmap_vmap = build_visibility_map(bake_name, bake_mesh, n_render_groups, prunemap_width, prunemap_height)
        vmaps.append(lightmap_vmap)
        result_col.objects.unlink(obj)

    # Process each of the bake meshes according to the light scenario, pruning unneeded faces
    render_path = vlm_utils.get_bakepath(context, type='RENDERS')
    lm_threshold = vlm_utils.get_lm_threshold()
    for light_scenario in light_scenarios:
        light_name, is_lightmap, _, lights, materials = light_scenario
        if not is_lightmap: continue
        influence = build_influence_map(render_path, light_name, n_render_groups, prunemap_width, prunemap_height)
        print(f'\nProcessing lightmaps for {light_name}')
        for (bake_col, bake_name, bake_mesh, sync_obj, is_spinner, use_obj_pos), lightmap_vmap in zip(merged_bake_meshes, vmaps):
            obj_name = f'{bake_name}.LM.{light_name}'
            bake_instance = bpy.data.objects.new(obj_name, bake_mesh.copy())
            n_faces = len(bake_instance.data.polygons)
            for index in range(n_render_groups):
                bake_instance.data.materials[index] = materials[index]
            result_col.objects.link(bake_instance)
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = bake_instance
            bake_instance.select_set(True)
            hdr_range = prune_lightmap_by_visibility_map(bake_instance.data, bake_name, light_name, lightmap_vmap, influence, prunemap_width, prunemap_height)
            if not bake_instance.data.polygons or hdr_range <= 2 * lm_threshold:
                result_col.objects.unlink(bake_instance)
                #print(f". Mesh {bake_name} has no more faces after optimization for {light_name} lighting")
            else:
                print(f'. {len(bake_instance.data.polygons):>6} faces out of {n_faces:>6} kept (HDR range: {hdr_range:>5.2f}) for {bake_col if sync_obj is None else bake_name}')
                if sync_obj:
                    dup = bpy.data.objects[sync_obj]
                    bake_instance.data.transform(Matrix(dup.matrix_basis).inverted())
                    bake_instance.matrix_basis = dup.matrix_basis
                else:
                    bake_instance.matrix_basis.identity()
                bake_instance.vlmSettings.bake_type = 'lightmap'
                bake_instance.vlmSettings.bake_lighting = light_name
                bake_instance.vlmSettings.bake_objects = bake_col
                bake_instance.vlmSettings.bake_hdr_range = hdr_range
                bake_instance.vlmSettings.bake_sync_light = ';'.join([l.name for l in lights]) if lights else ''
                bake_instance.vlmSettings.bake_sync_trans = sync_obj if sync_obj is not None else ''
                bake_instance.vlmSettings.is_spinner = is_spinner
                bake_instance.vlmSettings.use_obj_pos = use_obj_pos

    # Purge unlinked datas and clean up
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f'\nbake meshes created in {str(datetime.timedelta(seconds=time.time() - start_time))}')

    context.scene.vlmSettings.last_bake_step = 'meshes'
    return {'FINISHED'}


def orient2d(ax, ay, bx, by, x, y):
    """Evaluate on which side of a line a-b, a given point stand
    """
    return (bx-ax)*(y-ay) - (by-ay)*(x-ax)


def build_visibility_map(bake_name, bake_instance_mesh, n_render_groups, width, height):
    """Build a set of rasterized maps where each pixels contains the list of visible faces.
    The code here is derived from https://fgiesen.wordpress.com/2013/02/08/triangle-rasterization-in-practice/
    The odification consists in extending the rasterzed area by 1 pixel by changing the orient2d test.
    """
    bm = bmesh.new()
    bm.from_mesh(bake_instance_mesh)
    uv_layer = bm.loops.layers.uv["UVMap"]
    vmaps = [[] for xy in range(width * height)]
    bm.faces.ensure_lookup_table()
    dx = 1.0 / width
    dy = 1.0 / height
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
        lab = -math.sqrt((bx-ax)*(bx-ax)+(by-ay)*(by-ay))
        lac = -math.sqrt((cx-ax)*(cx-ax)+(cy-ay)*(cy-ay))
        lbc = -math.sqrt((bx-cx)*(bx-cx)+(by-cy)*(by-cy))
        min_x = max(0, min(width - 1, min(ax, bx, cx) - 1))
        min_y = max(0, min(height - 1, min(ay, by, cy) - 1))
        max_x = max(0, min(width - 1, max(ax, bx, cx) + 1))
        max_y = max(0, min(height - 1, max(ay, by, cy) + 1))
        A01 = ay - by
        B01 = bx - ax
        A12 = by - cy
        B12 = cx - bx
        A20 = cy - ay
        B20 = ax - cx
        w0_row = orient2d(bx, by, cx, cy, min_x, min_y)
        w1_row = orient2d(cx, cy, ax, ay, min_x, min_y)
        w2_row = orient2d(ax, ay, bx, by, min_x, min_y)
        marked = False
        for y in range(min_y, max_y + 1):
            w0 = w0_row
            w1 = w1_row
            w2 = w2_row
            for x in range(min_x, max_x + 1):
                if w0 >= lbc and w1 >= lac and w2 >= lab:
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
    if False: # For debug purpose, save generated visibility map
        print(f'. Saving visibility map {bake_name}')
        pixels = [1.0 for i in range(width*height*4)]
        for xy in range(width*height):
            pixels[xy*4] = len(vmaps[xy])
            pixels[xy*4+1] = len(vmaps[xy])
            pixels[xy*4+2] = len(vmaps[xy])
        image = bpy.data.images.new("debug", width, height, alpha=False, float_buffer=True)
        image.pixels = pixels
        image.filepath_raw = f'//{bake_name} - Visibility Map.exr'
        image.file_format = 'OPEN_EXR'
        image.save()
        bpy.data.images.remove(image)
    return vmaps


def build_influence_map(render_path, name, n_render_groups, w, h):
    """ Build influence maps by loading all renders, scaling them down using a max filter, then reducing to BW.
        A global (maximum of all light groups) influence map as well as one per render group.
        The red channel is the brightness. The blue channel contains the maximum of all render channel for HDR level evaluation.
    """
    vertex_shader = 'in vec2 position; in vec2 uv; in vec2 uv2; out vec2 uvInterp; out vec2 uvInterp2; void main() { uvInterp = uv; uvInterp2 = uv2; gl_Position = vec4(position, 0.0, 1.0); }'
    bw_fragment_shader = '''
        uniform sampler2D back;
        uniform sampler2D image;
        uniform float deltaU;
        uniform float deltaV;
        uniform float stacking;
        uniform int nx;
        uniform int ny;
        in vec2 uvInterp;
        in vec2 uvInterp2;
        out vec4 FragColor;
        void main() {
            vec3 t = stacking * texture(back, uvInterp2).rgb;
            for (int y=0; y<ny; y++) {
                for (int x=0; x<nx; x++) {
                    vec4 s = texture(image, uvInterp + vec2(x * deltaU, y * deltaV));
                    t = max(t, s.a * s.rgb);
                }
            }
            float v = dot(t.rgb, vec3(0.299, 0.587, 0.114));
            float m = max(max(t.r, t.g), t.b);
            FragColor = vec4(v, m, 0, 1.0);
        }
    '''
    # Rescale with a max filter, convert to black and white, apply alpha, in a single pass per image on the GPU
    gpu.state.blend_set('NONE')
    bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)
    offscreen = gpu.types.GPUOffScreen(w, h, format='RGBA32F')
    offscreen2 = gpu.types.GPUOffScreen(w, h, format='RGBA32F')
    offscreen3 = gpu.types.GPUOffScreen(w, h, format='RGBA32F')
    with offscreen3.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
    layers = (offscreen, offscreen2)
    for layer in layers:
        with layer.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
    imaps = [None for o in range(n_render_groups + 1)]
    for i in range(n_render_groups):
        path_exr = f"{render_path}{name} - Group {i}.exr"
        if os.path.exists(bpy.path.abspath(path_exr)):
            image = bpy.data.images.load(path_exr, check_existing=False)
            im_width, im_height = image.size
            nx = int(im_width / w)
            ny = int(im_height / h)
            batch = batch_for_shader(
                    bw_shader, 'TRI_FAN',
                    {
                        "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                        "uv": (
                            (     0.5          /im_width,      0.5          /im_height), 
                            (1 - (0.5 + nx - 1)/im_width,      0.5          /im_height), 
                            (1 - (0.5 + nx - 1)/im_width, 1 - (0.5 + ny - 1)/im_height), 
                            (     0.5          /im_width, 1 - (0.5 + ny - 1)/im_height)),
                        "uv2": (
                            (    0.5/w,     0.5/h), 
                            (1 - 0.5/w,     0.5/h), 
                            (1 - 0.5/w, 1 - 0.5/h), 
                            (    0.5/w, 1 - 0.5/h)),
                    },
                )
            bw_shader.bind()
            bw_shader.uniform_sampler("back", layers[0].texture_color)
            bw_shader.uniform_sampler("image", gpu.texture.from_image(image))
            bw_shader.uniform_float("deltaU", 1.0 / im_width)
            bw_shader.uniform_float("deltaV", 1.0 / im_height)
            bw_shader.uniform_int("nx", nx)
            bw_shader.uniform_int("ny", ny)
            with layers[1].bind():
                bw_shader.uniform_float("stacking", 1.0)
                batch.draw(bw_shader)
            with offscreen3.bind():
                bw_shader.uniform_float("stacking", 0.0)
                batch.draw(bw_shader)
            imaps[i+1] = offscreen3.texture_color.read()
            imaps[i+1].dimensions = w * h * 4
            bpy.data.images.remove(image)
            layers = (layers[1], layers[0]) # Swap layers
    imaps[0] = layers[0].texture_color.read()
    imaps[0].dimensions = w * h * 4
    for layer in layers:
        layer.free()
    if False: # For debug purpose, save generated influence map
        print(f'. Saving light influence map to {render_path}{name} - Influence Map.exr')
        image = bpy.data.images.new("debug", w, h, alpha=False, float_buffer=True)
        image.pixels = [v for v in imaps[0]]
        image.filepath_raw = f'{render_path}{name} - Influence Map.exr'
        image.file_format = 'OPEN_EXR'
        image.save()
        bpy.data.images.remove(image)
    return imaps


def prune_lightmap_by_visibility_map(bake_instance_mesh, bake_name, light_name, vmaps, imaps, w, h):
    """ Prune given lightmap mesh based on the given influence map / visibility map
    """
    lm_threshold = vlm_utils.get_lm_threshold()
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(bake_instance_mesh)
    bm.faces.ensure_lookup_table()
    # Mark faces that are actually influenced
    hdr_range = 0.0
    for face in bm.faces:
        face.tag = False
    for xy in range(w * h):
        if vmaps[xy] and imaps[0][4 * xy + 1] > lm_threshold: # prune by max channel
            hdr_range = max(hdr_range, imaps[0][4 * xy + 1]) # HDR Range is maximum of channels
            for face_index in vmaps[xy]:
                face = bm.faces[face_index]
                if face.material_index > -1 and imaps[face.material_index + 1] and imaps[face.material_index + 1][4 * xy] > lm_threshold:
                    face.tag = True
    if False:
        # Basic pruning: just remove the face under a lighting threshold
        faces = [face for face in bm.faces if not face.tag]
        if faces: bmesh.ops.delete(bm, geom=faces, context='FACES')
    else:
        # Keep neighbor faces and use them for fading out to limit seams in the resulting lightmaps
        kept_faces = [face for face in bm.faces if face.tag]
        color_layer = bm.loops.layers.color.verify()
        for face in kept_faces:
            for loop in face.loops:
                for neighbor_face in loop.vert.link_faces:
                    if not neighbor_face.tag:
                        neighbor_face.tag = True
        delete_faces = [face for face in bm.faces if not face.tag]
        if delete_faces:
            for face in bm.faces:
                for loop in face.loops:
                    loop[color_layer] = (0, 0, 0, 1)
            for face in kept_faces:
                for vert in face.verts:
                    for loop in vert.link_loops:
                        loop[color_layer] = (1, 1, 1, 1)
            bmesh.ops.delete(bm, geom=delete_faces, context='FACES')
    bmesh.update_edit_mesh(bake_instance_mesh)
    bpy.ops.object.mode_set(mode='OBJECT')
    return hdr_range
