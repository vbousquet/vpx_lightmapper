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
import io
import struct
import time
import gpu
import datetime
import numpy as np
import bpy_extras.mesh_utils
from math import radians
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from . import vlm_collections
from . import vlm_uvpacker
from PIL import Image # External dependency


def render_packmaps_gpu(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation optimized for Blender's GPU module, that only support rotation of 0/90/180/270
    """
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True) # Purge unlinked datas to avoid out of memory error
    vlmProps = context.scene.vlmSettings
    opt_force_render = False # Force rendering even if cache is available
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
                    brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale) if obj.vlmSettings.bake_type == 'lightmap' else  1.0
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
    opt_tex_factor = float(context.scene.vlmSettings.packmap_tex_factor)
    opt_padding = context.scene.vlmSettings.padding
    
    # Purge unlinked datas to avoid out of memory error
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    
    cg = vlm_utils.push_render_settings(True)
    col_state = vlm_collections.push_state()

    # FIXME setup color grading according to VPX tone mapping (experimental, to be improved)
    context.scene.view_settings.view_transform = 'Filmic'
    context.scene.view_settings.look = 'Low Contrast'
    context.scene.view_settings.exposure = -3
    context.scene.view_settings.gamma = 1

    rlc = context.view_layer.layer_collection
    result_col = vlm_collections.get_collection('BAKE RESULT')
    vlm_collections.find_layer_collection(rlc, result_col).exclude = False
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    packmap_index = 0
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break

        basepath = f"{bakepath}Packmap {packmap_index}"
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f'. Rendering packmap #{packmap_index} containing {len(objects)} bake/light map')
        
        if opt_force_render or not os.path.exists(path_png):
            tex_width = int(objects[0].vlmSettings.bake_packmap_width * opt_tex_factor)
            tex_height = int(objects[0].vlmSettings.bake_packmap_height * opt_tex_factor)
            pack_image = bpy.data.images.new(f"PackMap{packmap_index}", tex_width, tex_height, alpha=True)
            context.scene.render.bake.margin = opt_padding
            context.scene.render.bake.use_selected_to_active = False
            context.scene.render.bake.use_clear = True
            for obj in objects:
                bpy.ops.object.select_all(action='DESELECT')
                context.view_layer.objects.active = obj
                obj.select_set(True)
                is_light = obj.vlmSettings.bake_type == 'lightmap'
                n_materials = len(obj.data.materials)
                brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale) if obj.vlmSettings.bake_type == 'lightmap' else 1.0
                print(f'  . {obj.name} => HDR Scale: {obj.vlmSettings.bake_hdr_scale:>7.2f} => Brightness factor: {brightness:>7.2f}')
                if sequential_baking: # Bake each render gorup separately. Slow but nneded by low memory system
                    for i in range(n_materials):
                        if obj.vlmSettings.bake_type == 'playfield_fv': # FIXME reimplement Solid does not mean anything
                            path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}Solid - {obj.vlmSettings.bake_objects}.exr"
                        else:
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
                        if obj.vlmSettings.bake_type == 'playfield_fv': # FIXME reimplement Solid does not mean anything
                            path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}Solid - {obj.vlmSettings.bake_objects}.exr"
                        else:
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
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
            Image.open(path_png).save(path_webp, 'WEBP')

        packmap_index += 1

    vlm_collections.pop_state(col_state)
    vlm_utils.pop_render_settings(cg)


def render_packmaps_eevee(context):
    """Render all packmaps corresponding for the available current bake results
    Implementation using Eevee render. Works fine. No padding support for the time being
    """
    opt_force_render = False # Force rendering even if cache is available
    opt_tex_factor = float(context.scene.vlmSettings.packmap_tex_factor)
    opt_padding = context.scene.vlmSettings.padding
    
    col_state = vlm_collections.push_state()
    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    tmp_col = vlm_collections.get_collection('BAKETMP')
    result_col = vlm_collections.get_collection('BAKE RESULT')
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    vlm_collections.find_layer_collection(rlc, tmp_col).exclude = False

    cg = vlm_utils.push_render_settings(True)
    context.scene.render.engine = 'BLENDER_EEVEE'
    context.scene.render.film_transparent = True
    context.scene.eevee.taa_render_samples = 1
    context.scene.render.image_settings.file_format = 'OPEN_EXR'
    context.scene.render.image_settings.color_mode = 'RGBA'
    context.scene.render.image_settings.color_depth = '16'
    context.scene.render.pixel_aspect_x = 1.0
    context.scene.render.pixel_aspect_y = 1.0

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
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        pass_path = ''
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        
        if not opt_force_render and os.path.exists(path_png):
            packmap_index += 1
            continue
        
        tex_width = int(objects[0].vlmSettings.bake_packmap_width * opt_tex_factor)
        tex_height = int(objects[0].vlmSettings.bake_packmap_height * opt_tex_factor)
        context.scene.render.resolution_x = tex_width
        context.scene.render.resolution_y = tex_height
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
            brightness = vlm_utils.brightness_from_hdr(obj.vlmSettings.bake_hdr_scale) if obj.vlmSettings.bake_type == 'lightmap' else  1.0
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
    vlm_utils.pop_render_settings(cg)


def render_packmaps(op, context):
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering packmaps')
        return {'CANCELLED'}

    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

    start_time = time.time()
    print(f'\nRendering packmaps (mode is {context.scene.vlmSettings.bake_packmap_mode})')
    #render_packmaps_nest(op, context)
    if True:
        if context.scene.vlmSettings.bake_packmap_mode == 'gpu':
            render_packmaps_gpu(context)
        elif context.scene.vlmSettings.bake_packmap_mode == 'eevee':
            render_packmaps_eevee(context)
        elif context.scene.vlmSettings.bake_packmap_mode == 'cycle_seq':
            render_packmaps_bake(op, context, True)
        elif context.scene.vlmSettings.bake_packmap_mode == 'cycle':
            render_packmaps_bake(op, context, False)
    print(f'\nPackmaps rendered in {vlm_utils.format_time(time.time() - start_time)}.')
    context.scene.vlmSettings.last_bake_step = 'packmaps'
    return {'FINISHED'}


def render_packmaps_data_transfer_v0(op, context):
    """Render all packmaps corresponding for the available current bake results
    Implementation optimized for Blender's GPU module, that only support rotation of 0/90/180/270
    """
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True) # Purge unlinked datas to avoid out of memory error
    vlmProps = context.scene.vlmSettings
    opt_force_render = False # Force rendering even if cache is available
    opt_padding = int(vlmProps.padding)
    opt_uv_padding = opt_padding / int(vlmProps.tex_size)
    result_col = vlm_collections.get_collection('BAKE RESULT')
    bakepath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(bakepath)
    packmap_index = -1
    while True:
        packmap_index += 1
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break
        basepath = f'{bakepath}Packmap {packmap_index}'
        path_png = bpy.path.abspath(basepath + '.png')
        path_webp = bpy.path.abspath(basepath + ".webp")
        print(f". Rendering packmap #{packmap_index} containing {len(objects)} bake/light map")
        if opt_force_render or not os.path.exists(path_png):
            bake_type = objects[0].vlmSettings.bake_type
            tex_width = int(objects[0].vlmSettings.bake_packmap_width)
            tex_height = int(objects[0].vlmSettings.bake_packmap_height)
            if bake_type == 'playfield_fv': # FIXME reimplement Solid does not mean anything
                path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}Solid - {objects[0].vlmSettings.bake_objects}.exr"
                render = bpy.data.images.load(path, check_existing=False)
                render.scale(render.size[0], render.size[1])
                render.filepath_raw = path_png
                render.file_format = 'PNG'
                render.save()
                bpy.data.images.remove(render)
            else:


                vertex_shader = '''
                    in vec2 pos;
                    void main() {
                        gl_Position = vec4(2.0 * (pos - vec2(0.5)), 0.0, 1.0);
                    }
                '''
                fragment_shader = '''
                    out vec4 FragColor;
                    void main() {
                        FragColor = vec4(1.0);
                    }
                '''
                mask_shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
                copy_shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
                mask_offscreen = gpu.types.GPUOffScreen(tex_width, tex_height)
                packmap_offscreen = gpu.types.GPUOffScreen(tex_width, tex_height)

                for obj in objects:
                    # for each object, evaluate islands
                    mesh = obj.data
                    uv_layer = mesh.uv_layers['UVMap']
                    uvp_layer = mesh.uv_layers['UVMap Packed']
                    islands = bpy_extras.mesh_utils.mesh_linked_uv_islands(mesh)
                    print(f'{obj.name} has {len(islands)} islands')
                    poly_loops = [poly.loop_indices for poly in mesh.polygons]
                    # for each island, check scale, evaluate rotation and offset
                    # render mask (island with padding)
                    # Perform data transfer (masked pixel perfect render)
                    for island in islands:
                        mask_pts = []
                        draw_pts = []
                        draw_uvs = []
                        a = (2, 2)
                        b = (-1, -1)
                        a = b = None
                        dist = -1
                        pa = pb = -1
                        for poly_index in island:
                            loop_indices = poly_loops[poly_index]
                            for loop_index in loop_indices:
                                p = mathutils.Vector(uv_layer.data[loop_index].uv)
                                if a is None:
                                    a = p
                                    pa = loop_index
                                elif b is not None and (a-b).length_squared > dist:
                                    dist = (a-b).length_squared
                                    a = p
                                    pa = loop_index
                                elif b is None:
                                    b = p
                                    pb = loop_index
                                elif a is not None and (a-b).length_squared > dist:
                                    dist = (a-b).length_squared
                                    b = p
                                    pb = loop_index
                            p0 = uvp_layer.data[loop_indices[0]].uv
                            p1 = uvp_layer.data[loop_indices[1]].uv
                            p2 = uvp_layer.data[loop_indices[2]].uv
                            mask_pts.append(p0)
                            mask_pts.append(p1)
                            mask_pts.append(p2)
                        
                        ab1 = mathutils.Vector(uv_layer.data[pa].uv) - mathutils.Vector(uv_layer.data[pb].uv)
                        ab2 = mathutils.Vector(uvp_layer.data[pa].uv)- mathutils.Vector(uvp_layer.data[pb].uv)
                        if ab2.length > 0:
                            print(f'Scale={ab1.length / ab2.length} Angle={math.degrees(ab1.angle(ab2))}')
                        else:
                            print(f'Null ? {ab1.length} / {ab2.length}')
                        continue
                        
                        # Render the mask
                        with mask_offscreen.bind():
                            fb = gpu.state.active_framebuffer_get()
                            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                            mask_shader.bind()
                            gpu.state.blend_set('NONE') # Simple copy
                            batch_for_shader(mask_shader, 'TRIS', {"pos": mask_pts}).draw(mask_shader)
                            if False: # For debug purpose, save generated mask
                                print(f'. Saving light influence map to {bakepath}Test mask {packmap_index}.png')
                                mask = gpu.state.active_framebuffer_get().read_color(0, 0, tex_width, tex_height, 4, 0, 'UBYTE')
                                mask.dimensions = tex_width * tex_height * 4
                                image = bpy.data.images.new("debug", tex_width, tex_height)
                                image.pixels = [v / 255 for v in mask]
                                image.filepath_raw = f'{bakepath}Test mask {packmap_index}.png'
                                image.file_format = 'PNG'
                                image.save()
                                bpy.data.images.remove(image)
                        
                        # Perform masked data transfer
                        with packmap_offscreen.bind():
                            fb = gpu.state.active_framebuffer_get()
                            copy_shader.bind()
                            gpu.state.blend_set('NONE') # Simple copy
                            mask_offscreen.texture_color # < GPUTexture of the mask
                        
                mask_offscreen.free()
                packmap_offscreen.free()
                                

        # if opt_force_render or not os.path.exists(path_webp) or os.path.getmtime(path_webp) < os.path.getmtime(path_png):
            # Image.open(path_png).save(path_webp, 'WEBP')
    
    
    





def render_packmaps_nest(op, context):
    """Render all packmaps by using an external nesting algorithm
    The nesting algorithm ensure that source texture is moved/rotated with exact pixel transfer
    It uses a nesting algorithm like the one used for wood laser cutting (see SVGNest) to place
    the parts optimally without affecting there scale.
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering packmaps')
        return {'CANCELLED'}

    result_col = vlm_collections.get_collection('BAKE RESULT')
    exportpath = vlm_utils.get_bakepath(context, type='EXPORT')
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(exportpath)
    bake_results = [obj for obj in result_col.all_objects]
    bake_results.sort(key=lambda obj: obj.vlmSettings.bake_tex_factor, reverse=True)
    data = io.BytesIO()

    data.write(struct.pack("<I", int(context.scene.vlmSettings.tex_size)))
    data.write(struct.pack("<d", context.scene.vlmSettings.render_aspect_ratio))
    data.write(struct.pack("<I", int(context.scene.vlmSettings.padding)))
    for bake_index, bake in enumerate(bake_results):
        if bake.vlmSettings.bake_type == 'playfield':
            # TODO scale image to target
            print(f'Reprojecting playfield. You should use fixed view playfield rendering mode instead...')
            print(f'ERROR: not implemented')
        elif bake.vlmSettings.bake_type == 'playfield_fv':
            # TODO copy image
            pass
        else:
            print(f'. Exporting bake model {bake.name}')
            data.write(struct.pack("<I", len(bake.data.materials)))
            for i, mat in enumerate(bake.data.materials):
                path = f'{bakepath}{bake.vlmSettings.bake_name} - Group {i}.exr'.encode('utf-8')
                length = len(path)
                while length > 0: # see https://docs.microsoft.com/en-us/dotnet/api/system.io.binaryreader.readstring
                    data.write(bytes([0x80 | (length & 0x7F)]) if length >= 128 else bytes([length]))
                    length = length >> 7
                data.write(path)
            uv_layer = bake.data.uv_layers["UVMap"]
            ngon_reported = False
            for poly_index, poly in enumerate(bake.data.polygons):
                if len(poly.loop_indices) != 3:
                    if not ngon_reported: print(f'Bug, {bake.name} has polygons which are not triangles...')
                    ngon_reported = True
                    continue
                if poly.material_index < 0:
                    continue
                data.write(struct.pack("<i", poly.material_index))
                for loop_index in poly.loop_indices:
                    data.write(struct.pack("<2f", *uv_layer.data[loop_index].uv))
            data.write(struct.pack("<i", -1))
    data.write(struct.pack("<i", -1))
    if data.tell() == 0:
        op.report({'WARNING'}, f'No data to nest')
        return {'FINISHED'}
    with open(bpy.path.abspath(f'{exportpath}packmap.nest'),'wb') as nest_file:
        nest_file.write(data.getvalue())
    if False:
        packerDir = os.path.dirname(os.path.realpath(__file__))
        packerExe = packerDir + "\\PackmapNest.exe"
        process = None
        try:
            subprocess.run([packerExe])
        except:
            op.report({'ERROR'}, f'PackmapNest executable not found. Please copy PackmapNest.exe to: {packerDir}')
            return {'CANCELLED'}
    return {'FINISHED'}
