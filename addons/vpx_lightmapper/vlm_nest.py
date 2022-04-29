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

from collections import defaultdict
from pprint import pprint
from math import fabs, sqrt
import os
import bpy
import math
import time
import datetime
import numpy as np
from mathutils import Vector
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from PIL import Image # External dependency


## Code taken from Blender's core Magic UV add-on

def parse_island(bm, face_idx, faces_left, island, face_to_verts, vert_to_faces):
    faces_to_parse = [face_idx]
    while faces_to_parse:
        fidx = faces_to_parse.pop(0)
        if fidx in faces_left:
            faces_left.remove(fidx)
            island.append(bm.faces[fidx])
            for v in face_to_verts[fidx]:
                connected_faces = vert_to_faces[v]
                for cf in connected_faces:
                    faces_to_parse.append(cf)


def get_island(bm, face_to_verts, vert_to_faces, uv_layer):
    uv_island_lists = []
    faces_left = set(face_to_verts.keys())
    while faces_left:
        current_island = []
        face_idx = list(faces_left)[0]
        parse_island(bm, face_idx, faces_left, current_island, face_to_verts, vert_to_faces)
        max_uv = Vector((-10000000.0, -10000000.0))
        min_uv = Vector((10000000.0, 10000000.0))
        for face in current_island:
            for l in face.loops:
                uv = l[uv_layer].uv
                max_uv.x = max(uv.x, max_uv.x)
                max_uv.y = max(uv.y, max_uv.y)
                min_uv.x = min(uv.x, min_uv.x)
                min_uv.y = min(uv.y, min_uv.y)
        uv_island_lists.append(
            {'faces': current_island, 
            'render_group': current_island[0].material_index,
            'max': max_uv, 
            'min': min_uv,
            'size': (max_uv - min_uv), 
            'bb_area': (max_uv.x-min_uv.x)*(max_uv.y-min_uv.y)})
    return uv_island_lists


def create_vert_face_db(faces, uv_layer):
    face_to_verts = defaultdict(set)
    vert_to_faces = defaultdict(set)
    for f in faces:
        for l in f.loops:
            u,v = l[uv_layer].uv.to_tuple(5)
            id_ = (u, v, f.material_index), l.vert.index
            face_to_verts[f.index].add(id_)
            vert_to_faces[id_].add(f.index)
    return (face_to_verts, vert_to_faces)


## Code for 2D nesting algorithm


def nest(context, objects, render_size, tex_w, tex_h, nestmap_name, nestmap_offset):
    '''Perform nesting of a group of objects to a minimal (not optimal) set of nestmaps
    Eventually splitting objects that can't fit into a single nestmap.
    '''
    pack_threshold = int(tex_w * tex_h * 0.85)
    src_w, src_h = render_size
    # adds one to the padding to take in account uv positionning inside pixel
    padding = context.scene.vlmSettings.padding + 1

    # Evaluate all islands with their masks
    islands_to_pack = []
    for obj in objects:
        obj.vlmSettings.bake_nestmap = -1
        islands_to_pack.append(prepare_nesting(obj, render_size, padding))

    # Nest groups of islands into nestmaps
    splitted_objects = []
    nestmap_index = 0
    while islands_to_pack:
        # Sort from biggest squared pixcount sum to smallest
        islands_to_pack.sort(key=lambda p: p[4], reverse=True)

        # Select island groups
        total_pixcount = sum([block[3] for block in islands_to_pack])
        n_min_pages = max(1, int(total_pixcount / pack_threshold))
            
        pixcount = 0
        selection = []
        # Dispatch blocks on the total amount of remaining pages (don't put all the big one in the first page)
        for block in islands_to_pack[::n_min_pages]:
            if pixcount == 0 or pixcount + block[3] <= pack_threshold:
                selection.append(block)
                pixcount += block[3]
        # Then fill up with small blocks
        for block in reversed(islands_to_pack):
            if pixcount == 0 or pixcount + block[3] <= pack_threshold and not block in selection:
                selection.append(block)
                pixcount += block[3]
        

        while True:
            pixcount = 0
            selected_islands = []
            for block in selection:
                pixcount += block[3]
                selected_islands.extend(block[2])
            selection_names = [block[0].name for block in selection]
            print(f'\nTrying to nest in a single texture the {len(selected_islands)} islands ({pixcount/float(tex_w*tex_h):6.2%} fill with {pixcount} px / {tex_w*tex_h} content) of {selection_names}')

            # Save UV for undoing nesting if needed
            uv_undo = []
            for island in selected_islands:
                obj, bm = island['source']
                uv_layer = bm.loops.layers.uv.verify()
                for face in island['faces']:
                    for loop in face.loops:
                        uv_undo.append(Vector(loop[uv_layer].uv))

            nestmap = perform_nesting(selected_islands, src_w, src_h, tex_w, tex_h, padding, only_one_page=(len(selection) > 1))
            _, _, _, nested_islands, targets, _ = nestmap
            if len(targets) == 1:
                print(f'Nesting of {selection_names} succeeded.')
                # Success: store result for later nestmap render
                render_nestmap(context, selection, nestmap, nestmap_name,  nestmap_offset + nestmap_index)
                nestmap_index = nestmap_index + 1
                for block in selection:
                    islands_to_pack.remove(block)
                    obj, bm, block_islands, block_pix_count, block_pix_count_squared = block
                    for face in bm.faces:
                        face.tag = True
                    for island in block_islands:
                        for face in island['faces']:
                            face.tag = False
                    faces_to_remove = []
                    for face in bm.faces:
                        if face.tag: faces_to_remove.append(face)
                    if faces_to_remove: bmesh.ops.delete(bm, geom=faces_to_remove, context='FACES')
                    bm.to_mesh(obj.data)
                    bm.free()
                break
            else:
                # remove last block and start again with a smaller group
                if len(selection) > 1:
                    #print(f'Nesting of {selection_names} overflowed from a single texture page. Retrying with a smaller content.')
                    incompatible_blocks = []
                    for overflow_object in set([island['source'][0] for island in nested_islands]):
                        overflow_block = next((block for block in selection if block[0] == overflow_object))
                        incompatible_blocks.append(overflow_block)
                    incompatible_blocks.sort(key=lambda p: p[4])
                    # select smallest incompatible block with an impact of at least 1% of the overall pixel count
                    overflow_block = next((block for block in incompatible_blocks if block[3] > int(tex_w * tex_h * 0.01)), incompatible_blocks[0])
                    # for reference: remove the first overflowing block
                    #overflow_island = next((island for island in nested_islands if island['place'][0] > 0))
                    #overflow_object = overflow_island['source'][0]
                    print(f'Nesting has overflowed. Removing {overflow_block[0].name} from nesting group (smallest incompatible nest block).')
                    selection.remove(overflow_block)
                    # reset uv
                    index = 0
                    for island in selected_islands:
                        obj, bm = island['source']
                        uv_layer = bm.loops.layers.uv.verify()
                        for face in island['faces']:
                            for loop in face.loops:
                                loop[uv_layer].uv = uv_undo[index]
                                index = index + 1
                else:
                    # This single block did not fit inside a single page. We have performed a full nest, so we can keep the first page, and split the other islands
                    # to be nested with other blocks
                    block = selection[0]
                    islands_to_pack.remove(block)
                    obj, bm, block_islands, block_pix_count, block_pix_count_squared = block
                    src_w, src_h, padding, islands, targets, target_heights = nestmap
                    print(f'Object {obj.name} does not fit on a single page. Splitting it.')
                    # Consider the parts that fitted the first page as successfull
                    obj_copy = obj.copy()
                    obj_copy.data = obj_copy.data.copy()
                    for col in obj.users_collection:
                        col.objects.link(obj_copy)
                    splitted_objects.append(obj_copy)
                    bm_copy = bmesh.new()
                    bm_copy.from_mesh(obj_copy.data)
                    bm.faces.ensure_lookup_table()
                    bm_copy.faces.ensure_lookup_table()
                    uv_layer = bm.loops.layers.uv.verify()
                    uv_layer_copy = bm_copy.loops.layers.uv.verify()
                    for face in bm.faces:
                        face_copy = bm_copy.faces[face.index]
                        for loop_copy, loop in zip(face_copy.loops, face.loops):
                            loop_copy[uv_layer_copy].uv = loop[uv_layer].uv
                    faces_to_remove = []
                    processed_islands = []
                    remaining_islands = []
                    index = 0
                    processed_pix_count = 0
                    for island in block_islands:
                        n, x, y, rot = island['place']
                        for face in island['faces']:
                            for loop in face.loops:
                                if n > 0: loop[uv_layer].uv = uv_undo[index] # Only reset UV of islands that we neeed to nest again
                                index = index + 1
                        if n > 0:
                            remaining_islands.append(island)
                            for face in island['faces']:
                                faces_to_remove.append(bm_copy.faces[face.index])
                        else:
                            processed_islands.append(island)
                            island['source'] = (obj_copy, None)
                            block_pix_count = block_pix_count - island['pixcount']
                            block_pix_count_squared = block_pix_count_squared - island['pixcount squared']
                            processed_pix_count = processed_pix_count + island['pixcount']
                    bmesh.ops.delete(bm_copy, geom=faces_to_remove, context='FACES')
                    bm_copy.to_mesh(obj_copy.data)
                    bm_copy.free()
                    nestmap = (src_w, src_h, padding, processed_islands, targets[0:1], target_heights[0:1])
                    render_nestmap(context, [(obj_copy, None, processed_islands, processed_pix_count)], nestmap, nestmap_name, nestmap_offset + nestmap_index)
                    nestmap_index = nestmap_index + 1
                    print(f'. {len(processed_islands)} islands were nested on the first page and kept.')
                    # Continue nesting with all the remaining islands
                    print(f'. {len(remaining_islands)} islands were splitted, and still need to be nested.')
                    islands_to_pack.append( (obj, bm, remaining_islands, block_pix_count, block_pix_count_squared) )
                    break

    # Free unprocessed data if any
    for (obj, bm, islands, obj_pixcount) in islands_to_pack:
        bm.free()
        
    return (nestmap_index, splitted_objects)


def render_nestmap(context, selection, nestmap, nestmap_name, nestmap_index):
    src_w, src_h, padding, islands, targets, target_heights = nestmap
    n_render_groups = vlm_utils.get_n_render_groups(context)
    nestmaps = [np.zeros((len(target) * height * 4), 'f') for target, height in zip(targets, target_heights)]
    with_alpha = False
    render_path = vlm_utils.get_bakepath(context, type='RENDERS')
    for obj_name in {obj.name for (obj, _, _, _, _) in selection}:
        obj = bpy.data.objects[obj_name]
        hdr_scale = vlm_utils.get_hdr_scale(obj.vlmSettings.bake_hdr_range)
        print(f'. Copying renders for object {obj.name} from {obj.vlmSettings.bake_lighting} renders')
        # Load the render 
        render_data = []
        for i in range(n_render_groups):
            path = f"{render_path}{obj.vlmSettings.bake_lighting} - Group {i}.exr"
            loaded, render = vlm_utils.get_image_or_black(path, black_is_none=True)
            if render:
                pixel_data = np.zeros((src_w * src_h * 4), 'f') # using numpy is way faster
                render.pixels.foreach_get(pixel_data)
                render_data.append(pixel_data)
                if loaded == 'loaded': bpy.data.images.remove(render)
            else:
                render_data.append(None)
        # Render to the packed nest map
        for island in islands:
            island_obj, _ = island['source']
            if island_obj != obj: continue
            n, x, y, rot = island['place']
            unpadded_mask = island['unpadded_mask']
            mask = island['masks'][rot]
            mask_w = len(mask)
            min_x, min_y = island['min_i']
            island_render_group = island['render_group']
            if n > 0: # Skip islands that were nested to secondary pages: they have been splitted to other objects
                continue
            if island_render_group < 0 or island_render_group >= len(render_data) or render_data[island_render_group] is None:
                print('. Missing render group, skipping island')
                continue
            if obj.vlmSettings.bake_nestmap != nestmap_index:
                if obj.vlmSettings.bake_nestmap != -1:
                    print(f'ERROR: object {obj.name} was not splitted but has parts on multiple nestmaps')
                obj.vlmSettings.bake_nestmap = nestmap_index
            island_render = render_data[island_render_group]
            target_mask = targets[n]
            target_w = len(target_mask)
            target_h = target_heights[n]
            target_tex = nestmaps[n]

            # Identify opaque islands to process padding accordingly by fixing alpha on rendered borders (lightmaps are always opaque)
            if obj.vlmSettings.bake_type == 'lightmap':
                is_opaque = False # No border padding for lightmaps
            elif obj.vlmSettings.bake_type == 'active':
                is_opaque = False # No border padding for parts marked as translucent by the user
            else: # Apply border padding if the part is opaque
                alpha = 0
                n_alpha = 0
                for px, col_mask in enumerate(unpadded_mask):
                    for span in col_mask:
                        for py in range(span[0] + 1, span[1]):
                            dx = px + min_x - padding
                            dy = py + min_y - padding
                            if 0 <= dx and dx < src_w and 0 <= dy and dy < src_h:
                                alpha = alpha + island_render[4*(dx + dy*src_w) + 3]
                                n_alpha = n_alpha + 1
                is_opaque = alpha/n_alpha > 0.98 if n_alpha >= 32 else False
                #if n_alpha > 64: print(f'. Translucency: {alpha/n_alpha:>6.1%} for {n_alpha} points')
            with_alpha = with_alpha or not is_opaque
                
            for px, col_mask in enumerate(mask):
                for span in col_mask:
                    for py in range(span[0], span[1]+1):
                        if 0 <= x+px and x+px < target_w and 0 <= y+py and y+py < target_h:
                            if rot == 0:
                                dx = px
                                dy = py
                            elif rot == 1: # 90 rotation
                                dx = py
                                dy = mask_w - 1 - px
                            elif rot == 2: # Flipped on X
                                dx = mask_w - 1 - px
                                dy = py
                            elif rot == 3: # 90 rotation, Flipped on x
                                dx = py
                                dy = px
                            dx = min_x - padding + dx
                            dy = min_y - padding + dy
                            if 0 <= dx and dx < src_w and 0 <= dy and dy < src_h:
                                p  = 4 * ((x+px) + (y+py) * target_w)
                                p2 = 4 * (   dx  +    dy  * src_w   )
                                if is_opaque and island_render[p2+3] < 1:
                                    # border point: search nearest opaque (non border) color
                                    best_alpha = island_render[p2+3]
                                    best_pos = p2
                                    for d in range(1, padding + 1):
                                        for sx in range(dx-d, dx+d+1):
                                            if 0 <= sx and sx < src_w:
                                                if 0 <= dy-d and dy-d < src_h:
                                                    p3 = 4 * (sx + (dy-d) * src_w)
                                                    if island_render[p3+3] > best_alpha:
                                                        best_alpha = island_render[p3+3]
                                                        best_pos = p3
                                                if 0 <= dy+d and dy+d < src_h:
                                                    p3 = 4 * (sx + (dy+d) * src_w)
                                                    if island_render[p3+3] > best_alpha:
                                                        best_alpha = island_render[p3+3]
                                                        best_pos = p3
                                        for sy in range(dy-d +1, dy+d+1 -1):
                                            if 0 <= sy and sy < src_h:
                                                if 0 <= dx-d and dx-d < src_w:
                                                    p3 = 4 * ((dx-d) + sy * src_w)
                                                    if island_render[p3+3] > best_alpha:
                                                        best_alpha = island_render[p3+3]
                                                        best_pos = p3
                                                if 0 <= dx+d and dx+d < src_w:
                                                    p3 = 4 * ((dx+d) + sy * src_w)
                                                    if island_render[p3+3] > best_alpha:
                                                        best_alpha = island_render[p3+3]
                                                        best_pos = p3
                                        if best_alpha >= 1: break
                                    for j in range(3):
                                        target_tex[p+j] = island_render[best_pos+j] * hdr_scale
                                    target_tex[p+3] = 1
                                else:
                                    for j in range(3):
                                        target_tex[p+j] = island_render[p2+j] * hdr_scale
                                    target_tex[p+3] = island_render[p2+3]

    # Save the rendered nestmaps
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    base_filepath = f'{vlm_utils.get_bakepath(context, type="EXPORT")}{nestmap_name} {nestmap_index}'
    for i, target in enumerate(targets):
        target_w = len(target)
        target_h = target_heights[i]
        filled = 0
        # tex = np.ones((target_w * target_h * 4), 'f')
        for x in range(target_w):
            for span in target[x]:
                if span[0] < target_h:
                    filled += min(target_h - 1, span[1]) - span[0] + 1
                # for y in range(span[0], span[1] + 1):
                    # for j in range(4):
                        # if y < target_h:
                            # tex[4*(x + y*target_w) + j] = 0.0
                            # nestmaps[i][4*(x + y*target_w) + j] = 1.0
        pack_image = bpy.data.images.new(f'Nest {i}', target_w, target_h, alpha=with_alpha, float_buffer=True)
        # pack_image.pixels = tex
        pack_image.pixels = nestmaps[i]
        if len(targets) > 1:
            path_exr = bpy.path.abspath(f'{base_filepath} {i}.exr')
            path_png = bpy.path.abspath(f'{base_filepath} {i}.png')
            path_webp = bpy.path.abspath(f'{base_filepath} {i}.webp')
        else:
            path_exr = bpy.path.abspath(f'{base_filepath}.exr')
            path_png = bpy.path.abspath(f'{base_filepath}.png')
            path_webp = bpy.path.abspath(f'{base_filepath}.webp')
        scene.render.image_settings.color_mode = 'RGBA' if with_alpha else 'RGB'
        scene.render.image_settings.file_format = 'OPEN_EXR'
        scene.render.image_settings.exr_codec = 'DWAA'
        scene.render.image_settings.color_depth = '16'
        pack_image.save_render(path_exr, scene=scene)
        # Saving through save_render would save a linear PNG, not an sRGB one which is required by VPX
        pack_image.filepath_raw = path_png
        pack_image.file_format = 'PNG'
        pack_image.save()
        bpy.data.images.remove(pack_image)
        Image.open(path_png).save(path_webp, 'WEBP')
        print(f'. Texture #{i} has a size of {target_w}x{target_h} for a fill rate of {1.0 - (filled/(target_w*target_h)):>6.2%} (alpha: {with_alpha})')
    bpy.data.scenes.remove(scene)
    print(f'. Nest map generated and saved to {base_filepath}')



def prepare_nesting(obj, render_size, padding):
    src_w, src_h = render_size
    print(f'Preparing nesting of {obj.name} from {src_w}x{src_h} renders')

    print('. Identifying and sorting up all UV islands with render group splitting')
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    ftv, vtf = create_vert_face_db([f for f in bm.faces], uv_layer)
    islands = get_island(bm, ftv, vtf, uv_layer)

    # Compute island masks by rendering masks then creating a simplified span view
    offscreen = gpu.types.GPUOffScreen(src_w, src_h)
    vertex_shader = 'in vec2 pos; uniform vec2 ofs; void main() { gl_Position = vec4(2.0 * (pos + ofs) - vec2(1.0), 0.0, 1.0); }'
    fragment_shader = 'out vec4 FragColor; void main() { FragColor = vec4(1.0); }'
    shader_draw = gpu.types.GPUShader(vertex_shader, fragment_shader)
    gpu.state.blend_set('NONE')
    total_pix_count = 0
    total_pix_count_squared = 0
    for index, island in enumerate(islands, start=1):
        island_min = island['min']
        island_max = island['max']
        island_size = island['size']
        island_render_group = island['render_group']
        island['obj'] = obj
        island['masks'] = None
        min_x = math.floor(island_min.x * src_w)
        min_y = math.floor(island_min.y * src_h)
        max_x = math.ceil(island_max.x * src_w)
        max_y = math.ceil(island_max.y * src_h)
        island['min_i'] = (min_x, min_y)
        island['max_i'] = (max_x, max_y)
        island_w = min(src_w, max(1, max_x - min_x + 2*padding))
        island_h = min(src_h, max(1, max_y - min_y + 2*padding))
        ofs_u = min_x / float(src_w)
        ofs_v = min_y / float(src_h)
        
        # Render the island and create a discrete tuple model (vertical opaque spans) and a list of its column order
        pts=[]
        lines=[]
        for face in island['faces']:
            prev = first = None
            for loop in face.loops:
                uv = loop[uv_layer].uv
                uv = ((uv[0]-ofs_u) + padding/float(src_w), (uv[1]-ofs_v) + padding/float(src_h))
                pts.append(uv)
                if prev:
                    lines.append(prev)
                    lines.append(uv)
                else:
                    first = uv
                prev = uv
                lines.append(prev)
                lines.append(first)
        tri_batch = batch_for_shader(shader_draw, 'TRIS', {"pos": pts})
        pt_batch = batch_for_shader(shader_draw, 'POINTS', {"pos": pts})
        line_batch = batch_for_shader(shader_draw, 'LINES', {"pos": lines})

        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
            shader_draw.bind()
            shader_draw.uniform_float("ofs", (0.0, 0.0))
            tri_batch.draw(shader_draw)
        buffer = offscreen.texture_color.read()
        buffer.dimensions = src_w * src_h * 4

        if False:
            pack_image = bpy.data.images.new(f'{obj.name} - Island {index}', src_w, src_h, alpha=True)
            pack_image.pixels = [v / 255 for v in buffer]
            pack_image.filepath_raw = bpy.path.abspath(f'//{obj.name} - Island {index}.png')
            pack_image.file_format = 'PNG'
            pack_image.save()
            print(f'. Island mask saved to {pack_image.filepath_raw}')
            bpy.data.images.remove(pack_image)

        island_hor = []
        for x in range(island_w):
            ymin = -1
            spans = []
            for y in range(island_h):
                if buffer[4 * (x + y * src_w)] > 0:
                    if ymin == -1: ymin = y
                else:
                    if ymin != -1:
                        spans.append((ymin, y-1, y-1-ymin + 1))
                        ymin = -1
            if ymin != -1:
                spans.append((ymin, island_h-1, island_h-1-ymin + 1))
            island_hor.append(spans)
        island['unpadded_mask'] = island_hor

        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            shader_draw.bind()
            for px in range(-padding, padding+1):
                for py in range(-padding, padding+1):
                    shader_draw.uniform_float("ofs", (px/float(src_w), py/float(src_h)) )
                    tri_batch.draw(shader_draw)
                    pt_batch.draw(shader_draw)
                    line_batch.draw(shader_draw)
        buffer = offscreen.texture_color.read()
        buffer.dimensions = src_w * src_h * 4

        island_pix_count = 0
        island_pix_count_squared = 0
        island_hor = []
        for x in range(island_w):
            ymin = -1
            spans = []
            for y in range(island_h):
                if buffer[4 * (x + y * src_w)] > 0:
                    if ymin == -1: ymin = y
                else:
                    if ymin != -1:
                        spans.append((ymin, y-1, y-1-ymin + 1))
                        island_pix_count = island_pix_count + (y-1-ymin + 1)
                        island_pix_count_squared = island_pix_count_squared + (y-1-ymin + 1)*(y-1-ymin + 1)
                        ymin = -1
            if ymin != -1:
                spans.append((ymin, island_h-1, island_h-1-ymin + 1))
                island_pix_count = island_pix_count + (island_h-1-ymin + 1)
                island_pix_count_squared = island_pix_count_squared + (island_h-1-ymin + 1)*(island_h-1-ymin + 1)
            island_hor.append(spans)
        
        island_ver = []
        for y in range(island_h):
            xmin = -1
            spans = []
            for x in range(island_w):
                if buffer[4 * (x + y * src_w) + 3] > 0:
                    if xmin == -1: xmin = x
                else:
                    if xmin != -1:
                        spans.append((xmin, x-1, x-1-xmin + 1))
                        xmin = -1
            if xmin != -1:
                spans.append((xmin, island_w-1, island_w-1-xmin + 1))
            island_ver.append(spans)
        island_mask = island_hor

        island_masks = []
        island['masks'] = island_masks
        island_masks.append(island_hor) # 0 rotation
        island_masks.append([span for span in reversed(island_ver)]) # 90 rotation
        island_masks.append([span for span in reversed(island_hor)]) # 0 rotation, Flipped on X
        island_masks.append(island_ver) # 90 rotation, Flipped on X
        
        island['pixcount'] = island_pix_count
        island['pixcount squared'] = island_pix_count_squared
        total_pix_count = total_pix_count + island_pix_count
        total_pix_count_squared = total_pix_count_squared + island_pix_count_squared

        island['source'] = (obj, bm)

        # print(f'. Island #{index:>3}/{len(islands)} placement mask computed (size: {island_w:>4}x{island_h:>4}, pixcount: {island_pix_count:>7}px, fill rate: {island_pix_count/(island_w*island_h):>6.1%})')
        
    offscreen.free()
    
    return (obj, bm, islands, total_pix_count, total_pix_count_squared)


def round_for_mimpaps(x):
    '''Round a texture dimension (width/height) for clean mipmap support.
    To avoid blur artefacts, bakemaps and lightmaps need to have the same mipmapping capabilities.
    Therefore, texture dimensions need to be rounded to the next power of 2 of the mipmap level
    we want to support. For example, a 4K render on a 4K display, needs at least 1 mipmap level (so
    rounding to the next even size). A 4k render on a 0.5k display (landscape 1920x1080) will need
    at least 4 mipmap levels (rounding to the next multiple of 16).
    '''
    mipmap_level = 6 # Make a 8K render playable on a 0.25k display
    mask = 1 << mipmap_level
    return ((x + (mask-1)) & (-mask))
    # Next power of 2 would be excessive. The full mipmap chain down to 1 pixel is not needed
    #return 1<<(x-1).bit_length()
    
    
def perform_nesting(islands, src_w, src_h, tex_w, tex_h, padding, only_one_page=False):
    # Placement algorithm (simple discret bottom left direct placement)
    targets = []
    #islands = sorted(islands, key=lambda p:p['bb_area'], reverse=True)
    islands = sorted(islands, key=lambda p:p['pixcount'], reverse=True)
    for index, island in enumerate(islands, start=1):
        island_masks = island['masks']
        if not island_masks: continue
        island_w = len(island_masks[0]) # width is number of column
        island_h = len(island_masks[1]) # height is number of column of 90 rotated island
        if island_w > tex_w or island_h > tex_h:
            # FIXME this needs to be handled gracefully (here it skips, but it will likely crash afterward)
            print(f'. Island #{index:>3}/{len(islands)} size is {island_w}x{island_h} and cannot be placed in a {tex_w}x{tex_h} texture, skipping island')
            continue
        x = y = n = 0
        n_succeeded = 0
        col_index = 0
        rot_index = 0
        rot_order = [0, 2, 1, 3] if island_w <= island_h else [1, 3, 0, 2]
        while True:
            col = col_index
            rot = rot_order[rot_index]
            x_start = x
            y_start = y
            mask = island_masks[rot]
            w = len(mask)
            if n >= len(targets):
                targets.append([[(0, tex_h-1, tex_h)] for i in range(tex_w)])
            target = targets[n]
            col_index = (col_index + 1) % w
            # Find matching y, if any, that allows to place all island's column spans
            for span in mask[col]:
                # First target span that is above current y and large enough to host the island span
                place = next((place for place in target[x + col] if span[2] <= place[2] and y+span[1] <= place[1]), None)
                if not place:
                    x = x + 1
                    break
                if place[0] > y + span[0]:
                    y = place[0] - span[0]
            if x == x_start:
                if y == y_start:
                    n_succeeded = n_succeeded + 1
                    if n_succeeded >= w:
                        break
                else:
                    n_succeeded = 0
            else:
                n_succeeded = 0
                y = 0
                if x + w >= tex_w:
                    x = 0
                    col_index = 0
                    rot_index = rot_index + 1
                    if rot_index >= len(island_masks):
                        rot_index = 0
                        n = n + 1
                        if only_one_page: # Fast Fail if packing to a single page
                            island['place'] = (n, x, y, rot) # mark it to identify the first offender
                            print(f'. Island #{index:>3}/{len(islands)} could not be placed (single page mode) pixcount:{island["pixcount"]:>7}px  from {island["source"][0].name}')
                            return (src_w, src_h, padding, islands, [], [])
        island['place'] = (n, x, y, rot)
        # print(f'. Island #{index:>3}/{len(islands)} placed on nestmap #{n} at {x:>4}, {y:>4} o:{rot} pixcount:{island["pixcount"]:>7}px  from {island["source"][0].name}')
        
        # Update target mask
        target_mask = targets[n]
        for col, spans in enumerate(island_masks[rot]):
            for span in spans:
                for i, check in enumerate(target_mask[x + col]):
                    if span[2] <= check[2] and y+span[1] <= check[1]:
                        target_mask[x + col].pop(i)
                        if y+span[1] < check[1]:
                            target_mask[x + col].insert(i, (y+span[1], check[1], check[1] - (y+span[1]) + 1))
                        if check[0] < y+span[0]:
                            target_mask[x + col].insert(i, (check[0], y+span[0], y+span[0] - check[0] + 1))
                        break

    # Crop targets to smallest power of two (if not DX9 will lower the texture quality...)
    target_heights = []
    for target in targets:
        # Remove empty columns on the right
        while len(target)>0 and len(target[-1]) == 1 and target[-1][0][2] == tex_h:
            target.pop()
        target_w = round_for_mimpaps(len(target))
        while len(target) < target_w:
            target.append([(0, tex_h-1, tex_h)])
        # Evaluate upper bound
        ymax = 0
        for spans in target:
            if len(spans) > 0:
                ymax = max(ymax, spans[-1][0] - 1)
            else:
                ymax = tex_h
                break
        target_h = round_for_mimpaps(ymax)
        target_heights.append(target_h)
        
    # Update UV to the new placement
    for island in islands:
        obj, bm = island['source']
        uv_layer = bm.loops.layers.uv.verify()
        n, x, y, rot = island['place']
        min_x, min_y = island['min_i']
        max_x, max_y = island['max_i']
        target_w = len(targets[n])
        target_h = target_heights[n]
        for face in island['faces']:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                u0 = uv[0] * src_w - min_x
                v0 = uv[1] * src_h - min_y
                if rot == 0: # Original position
                    u = u0
                    v = v0
                elif rot == 1: # 90 rotation
                    u = (max_y - min_y) - v0
                    v = u0
                elif rot == 2: # Flipped on X
                    u = (max_x - min_x) - u0
                    v = v0
                elif rot == 3: # 90 rotation, Flipped on x
                    u = v0
                    v = u0
                loop[uv_layer].uv = ((x + padding + u)/float(target_w) + (n * 2), (y + padding + v)/float(target_h))
    
    return (src_w, src_h, padding, islands, targets, target_heights)



