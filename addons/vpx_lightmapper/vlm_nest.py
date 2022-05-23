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
from collections import namedtuple
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
        island = {'faces': current_island, 'render_group': current_island[0].material_index}
        update_island_bounds(island, uv_layer)
        uv_island_lists.append(island)
    return uv_island_lists


def tri_overlaps(a1, b1, c1, a2, b2, c2):
    e = 0.0
    det2d = lambda a,b,c: a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y)
    if det2d(a1, b1, c1) < 0.0: c1, b1, a1 = a1, b1, c1
    if det2d(a2, b2, c2) < 0.0: c2, b2, a2 = a2, b2, c2
    if det2d(a1, b1, a2)<e and det2d(a1, b1, b2)<e and det2d(a1, b1, c2)<e: return False
    if det2d(b1, c1, a2)<e and det2d(b1, c1, b2)<e and det2d(b1, c1, c2)<e: return False
    if det2d(c1, a1, a2)<e and det2d(c1, a1, b2)<e and det2d(c1, a1, c2)<e: return False
    if det2d(a2, b2, a1)<e and det2d(a2, b2, b1)<e and det2d(a2, b2, c1)<e: return False
    if det2d(b2, c2, a1)<e and det2d(b2, c2, b1)<e and det2d(b2, c2, c1)<e: return False
    if det2d(c2, a2, a1)<e and det2d(c2, a2, b1)<e and det2d(c2, a2, c1)<e: return False
    return True


def island_bounds_overlaps(i1, i2):
    i1_min = i1['min']
    i1_max = i1['max']
    i2_min = i2['min']
    i2_max = i2['max']
    return i1_min.x < i2_max.x and i1_max.x > i2_min.x and i1_max.y > i2_min.y and i1_min.y < i2_max.y
    

def get_merged_overlapping_islands(islands, uv_layer):
    merged_islands = []
    for island in islands:
        merged = False
        other_islands = [i for i in merged_islands if island['render_group'] == i['render_group'] and island_bounds_overlaps(island, i)]
        for f in island['faces']:
            a1 = f.loops[0][uv_layer].uv
            b1 = f.loops[1][uv_layer].uv
            c1 = f.loops[2][uv_layer].uv
            for other_island in other_islands:
                for g in other_island['faces']:
                    a2 = g.loops[0][uv_layer].uv
                    b2 = g.loops[1][uv_layer].uv
                    c2 = g.loops[2][uv_layer].uv
                    if tri_overlaps(a1, b1, c1, a2, b2, c2):
                        #print('. Overlapping islands merged')
                        merged = True
                        other_island['faces'].extend(island['faces'])
                        update_island_bounds(other_island, uv_layer)
                        break
            if merged:
                break
        if not merged:
            merged_islands.append(island)
    return merged_islands
    

def update_island_bounds(island, uv_layer):
    max_uv = Vector((-10000000.0, -10000000.0))
    min_uv = Vector((10000000.0, 10000000.0))
    for face in island['faces']:
        for l in face.loops:
            uv = l[uv_layer].uv
            max_uv.x = max(uv.x, max_uv.x)
            max_uv.y = max(uv.y, max_uv.y)
            min_uv.x = min(uv.x, min_uv.x)
            min_uv.y = min(uv.y, min_uv.y)
    island['max'] = max_uv
    island['min'] = min_uv
    island['size'] = max_uv - min_uv
    island['bb_area'] = (max_uv.x-min_uv.x)*(max_uv.y-min_uv.y)


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

NestBlock = namedtuple("NestBlock", "obj bm islands pix_count")
NestMap = namedtuple("NestMap", "src_w src_h padding islands targets target_heights")

def nest(context, objects, uv_proj_name, uv_name, render_size, tex_w, tex_h, nestmap_name, nestmap_offset):
    '''Perform nesting of a group of objects to a minimal (not optimal) set of nestmaps
    Eventually splitting objects that can't fit into a single nestmap.
    '''
    pack_threshold = int(tex_w * tex_h * 0.85)
    src_w, src_h = render_size
    # adds one to the padding to take in account uv positionning inside pixel
    padding = context.scene.vlmSettings.padding + 1
    start_time = time.time()

    # Evaluate all islands with their masks
    tick_time = time.time()
    islands_to_pack = []
    for obj in objects:
        obj.vlmSettings.bake_nestmap = -1
        islands_to_pack.append(prepare_nesting(obj, render_size, padding, uv_name))
    prepare_length = time.time() - tick_time

    # Nest groups of islands into nestmaps
    splitted_objects = []
    nestmap_index = 0
    n_failed = 0
    render_length = 0
    while islands_to_pack:
        # Sort from biggest squared pixcount sum to smallest
        islands_to_pack.sort(key=lambda p: p.pix_count*p.pix_count, reverse=True)

        # Select island groups
        total_pixcount = sum([block.pix_count for block in islands_to_pack])
        n_min_pages = max(1, int(total_pixcount / pack_threshold))
            
        pixcount = 0
        selection = []
        # Dispatch blocks on the total amount of remaining pages (don't put all the big one in the first page)
        for block in islands_to_pack[::n_min_pages]:
            if pixcount == 0 or pixcount + block.pix_count <= pack_threshold:
                selection.append(block)
                pixcount += block.pix_count
        # Then fill up with small blocks
        for block in reversed(islands_to_pack):
            if pixcount == 0 or pixcount + block.pix_count <= pack_threshold and not block in selection:
                selection.append(block)
                pixcount += block.pix_count
        
        retry_count = 0
        incompatible_sets = []
        while True:
            pixcount = 0
            selected_islands = []
            for block in selection:
                pixcount += block.pix_count
                selected_islands.extend(block.islands)
            selection_names = [block.obj.name for block in selection]
            print(f'\nTrying to nest in a single texture the {len(selected_islands)} islands ({pixcount/float(tex_w*tex_h):6.2%} fill with {pixcount} px / {tex_w*tex_h} content)\n. Source objects: {selection_names}')

            # Save UV for undoing nesting if needed
            uv_undo = []
            for island in selected_islands:
                obj, bm = island['source']
                uv_layer = bm.loops.layers.uv[uv_name]
                for face in island['faces']:
                    for loop in face.loops:
                        uv_undo.append(Vector(loop[uv_layer].uv))

            nestmap = perform_nesting(selected_islands, uv_name, src_w, src_h, tex_w, tex_h, padding, only_one_page=(len(selection) > 1))
            if len(nestmap.targets) == 1:
                print(f'. Nesting succeeded.')
                # Success: store result for later nestmap render
                tick_time = time.time()
                render_nestmap(context, selection, uv_proj_name, nestmap, nestmap_name, nestmap_offset + nestmap_index)
                render_length = time.time() - tick_time
                nestmap_index = nestmap_index + 1
                for block in selection:
                    islands_to_pack.remove(block)
                    obj, bm, block_islands, block_pix_count = block
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
                n_failed = n_failed + 1
                if len(selection) > 1:
                    retry_count = retry_count + 1
                    incompatible_blocks = []
                    for overflow_object in set([island['source'][0] for island in nestmap.islands]):
                        overflow_block = next((block for block in selection if block.obj == overflow_object))
                        incompatible_blocks.append(overflow_block)
                    incompatible_blocks.sort(key=lambda p: p.pix_count*p.pix_count)
                    incompatible_sets.append(set([block.obj.name for block in incompatible_blocks]))
                    # select smallest incompatible block with an impact of at least 1% of the overall pixel count, replace it with smaller blocks
                    overflow_block = next((block for block in incompatible_blocks if block.pix_count > int(tex_w * tex_h * 0.01)), incompatible_blocks[0])
                    pixcount = pixcount - overflow_block.pix_count
                    selection.remove(overflow_block)

                    limited_threshold = pack_threshold - int(tex_w * tex_h * 0.01 * retry_count) # Get down threshold after each retry to ensure finding a solution
                    n_added = added_pixcount = 0
                    for block in reversed(islands_to_pack):
                        if pixcount == 0 or pixcount + block.pix_count <= limited_threshold and not block in selection:
                            new_set = set([block.obj.name for block in selection])
                            new_set.add(block.obj.name)
                            if new_set not in incompatible_sets:
                                selection.append(block)
                                pixcount += block.pix_count
                                added_pixcount += block.pix_count
                                n_added += 1
                    print(f'. Nesting overflowed. Replacing {overflow_block.obj.name} ({overflow_block.pix_count}px) from nesting group (smallest incompatible nest block) with {n_added} smaller blocks ({added_pixcount}px)')
                    
                    # reset uv
                    index = 0
                    for island in selected_islands:
                        obj, bm = island['source']
                        uv_layer = bm.loops.layers.uv[uv_name]
                        for face in island['faces']:
                            for loop in face.loops:
                                loop[uv_layer].uv = uv_undo[index]
                                index = index + 1
                else:
                    # This single block did not fit inside a single page. We have performed a full nest, so we can keep the first page, and split the other islands
                    # to be nested with other blocks
                    block = selection[0]
                    islands_to_pack.remove(block)
                    obj, bm, block_islands, block_pix_count = block
                    src_w, src_h, padding, islands, targets, target_heights = nestmap
                    print(f'. Object {obj.name} did not fit on a single page. Splitting it.')
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
                    uv_layer = bm.loops.layers.uv[uv_name]
                    uv_layer_copy = bm_copy.loops.layers.uv[uv_name]
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
                            processed_pix_count = processed_pix_count + island['pixcount']
                    bmesh.ops.delete(bm_copy, geom=faces_to_remove, context='FACES')
                    bm_copy.to_mesh(obj_copy.data)
                    bm_copy.free()
                    nestmap = NestMap(src_w, src_h, padding, processed_islands, targets[0:1], target_heights[0:1])
                    tick_time = time.time()
                    render_nestmap(context, [NestBlock(obj_copy, None, processed_islands, processed_pix_count)], uv_proj_name, nestmap, nestmap_name, nestmap_offset + nestmap_index)
                    render_length = time.time() - tick_time
                    nestmap_index = nestmap_index + 1
                    print(f'. {len(processed_islands)} islands were nested on the first page and kept.')
                    # Continue nesting with all the remaining islands
                    print(f'. {len(remaining_islands)} islands were splitted, and still need to be nested.')
                    islands_to_pack.append( NestBlock(obj, bm, remaining_islands, block_pix_count) )
                    break

    # Free unprocessed data if any
    for block in islands_to_pack:
        block.bm.free()
    total_length = time.time() - start_time
    print(f'. Nestmapping finished ({n_failed} overflow were handled for {nestmap_index} generated nestmaps) in {str(datetime.timedelta(seconds=total_length))} (prepare={str(datetime.timedelta(seconds=prepare_length))}, nest={str(datetime.timedelta(seconds=total_length-prepare_length-render_length))}, render={str(datetime.timedelta(seconds=render_length))}).')
        
    return (nestmap_index, splitted_objects)


def get_nearest_opaque_pos(image, dx, dy, padding, src_w, src_h):
    ''' Find the nearest opaque (or at least less transparent) point position in the provided image
    Search is performed up to the given padding distance
    '''
    best_pos = 4 * (   dx  +    dy  * src_w   )
    best_alpha = image[best_pos+3]
    best_dist = (padding+1) * (padding+1) * 2
    for d in range(1, padding):
        for sx in range(dx-d, dx+d+1):
            if 0 <= sx and sx < src_w:
                if 0 <= dy-d and dy-d < src_h:
                    p3 = 4 * (sx + (dy-d) * src_w)
                    if image[p3+3] >= best_alpha:
                        dist = sx*sx+d*d
                        if image[p3+3] > best_alpha or dist < best_dist:
                            best_pos = p3
                            best_dist = dist
                            best_alpha = image[p3+3]
                if 0 <= dy+d and dy+d < src_h:
                    p3 = 4 * (sx + (dy+d) * src_w)
                    if image[p3+3] >= best_alpha:
                        dist = sx*sx+d*d
                        if image[p3+3] > best_alpha or dist < best_dist:
                            best_pos = p3
                            best_dist = dist
                            best_alpha = image[p3+3]
        for sy in range(dy-d +1, dy+d+1 -1):
            if 0 <= sy and sy < src_h:
                if 0 <= dx-d and dx-d < src_w:
                    p3 = 4 * ((dx-d) + sy * src_w)
                    if image[p3+3] >= best_alpha:
                        dist = sy*sy+d*d
                        if image[p3+3] > best_alpha or dist < best_dist:
                            best_pos = p3
                            best_dist = dist
                            best_alpha = image[p3+3]
                if 0 <= dx+d and dx+d < src_w:
                    p3 = 4 * ((dx+d) + sy * src_w)
                    if image[p3+3] >= best_alpha:
                        dist = sy*sy+d*d
                        if image[p3+3] > best_alpha or dist < best_dist:
                            best_pos = p3
                            best_dist = dist
                            best_alpha = image[p3+3]
        if best_alpha == 1.0 and best_dist <= (d+1)*(d+1):
            return best_pos
    return best_pos


def render_nestmap(context, selection, uv_name, nestmap, nestmap_name, nestmap_index):
    """
    TODO implement GPU rendering and lightmap seams fading
    
    For each island:
    - render mask of island, with padding and fading:
      . render opaque tris/lines/points for each of the padding x/y, with a value of 1 for red channel, interpolated vertex color for blue channel
      . render opaque tris/lines/points at the center, with a value of 1 for green channel
      => red channel is full mask, green channel is untouched mask, blue channel is fading
    - perform data transfer, using mask for fading and padding search: read mask and depending on its value
      . (0,.,.) => Discard
      . (1,a,1) => Copy, multiplied by 'a'
      . alpha < 0 => perform opaque padding for opaque islands
    
    """
    src_w, src_h, padding, islands, targets, target_heights = nestmap
    n_render_groups = vlm_utils.get_n_render_groups(context)
    nestmaps = [np.zeros((len(target) * height * 4), 'f') for target, height in zip(targets, target_heights)]
    with_alpha = False
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')
    render_path = vlm_utils.get_bakepath(context, type='RENDERS')
    padding_threshold = seam_threshold = 0.99
    # Load the render masks
    mask_data = []
    for i in range(n_render_groups):
        path = f"{mask_path}Mask Group {i}.png"
        loaded, render = vlm_utils.get_image_or_black(path, black_is_none=True)
        if render:
            pixel_data = np.zeros((src_w * src_h * 4), 'f') # using numpy is way faster
            render.pixels.foreach_get(pixel_data)
            mask_data.append(pixel_data)
            if loaded == 'loaded': bpy.data.images.remove(render)
        else:
            mask_data.append(None)
    render_data = []
    loaded_bake_lighting = None
    for obj_name in sorted(list({obj.name for (obj, _, _, _) in selection}), key=lambda x:bpy.data.objects[x].vlmSettings.bake_lighting):
        obj = bpy.data.objects[obj_name]
        hdr_range = obj.vlmSettings.bake_hdr_range
        if obj.vlmSettings.bake_type == 'lightmap':
            # Cut off pixels below the lightmap threshold to avoid seams:
            # v => (v + rgb_offset) * (hdr_range / (hdr_range + rgb_offset)) * hdr_scale
            rgb_offset = -vlm_utils.get_lm_threshold() * 2
            rgb_offset = 0.0 # FIXME disabled basic seam removal for HDR testing
            rgb_scale = min(8.0, hdr_range / (hdr_range + rgb_offset))
            rgb_scale = rgb_scale * vlm_utils.get_hdr_scale(obj)
            rgb_scale = 1.0
        else:
            rgb_offset = 0.0
            rgb_scale = 1.0
        print(f'. Copying renders (RGB rescaling offset={rgb_offset:6.3f} scale={rgb_scale:6.3f} HDR range={hdr_range:>7.2f}) for object {obj.name} from {obj.vlmSettings.bake_lighting} renders')
        # Load the render (if not already loaded)
        if obj.vlmSettings.bake_lighting != loaded_bake_lighting:
            render_data.clear()
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
            loaded_bake_lighting = obj.vlmSettings.bake_lighting

        # Offscreen surface where we render the seam fading mask for lightmaps
        offscreen = gpu.types.GPUOffScreen(src_w, src_h)
        vertex_shader = 'in vec2 pos; in vec4 col; uniform vec2 ofs; out vec4 colInterp; void main() { colInterp = col; gl_Position = vec4(2.0 * (pos + ofs) - vec2(1.0), 0.0, 1.0); }'
        fragment_shader = 'in vec4 colInterp; out vec4 FragColor; void main() { FragColor = colInterp; }'
        shader_draw = gpu.types.GPUShader(vertex_shader, fragment_shader)
        gpu.state.blend_set('NONE')

        # Render to the packed nest map
        for island in islands:
            island_obj, bm = island['source']
            if island_obj != obj: continue
            n, x, y, rot = island['place']
            mask = island['masks'][rot]
            mask_w = len(mask)
            min_x, min_y = island['min_i']
            island_render_group = island['render_group']
            if n > 0: # Skip islands that were nested to secondary pages: they have been splitted to other objects
                continue
            if island_render_group < 0 or island_render_group >= len(render_data):
                print('. Missing render group, skipping island (likely a bug)')
                continue
            if render_data[island_render_group] is None:
                print('. No render (likely uninfluenced lightmap), skipping island')
                continue
            if obj.vlmSettings.bake_nestmap != nestmap_index:
                if obj.vlmSettings.bake_nestmap != -1:
                    print(f'ERROR: object {obj.name} was not splitted but has parts on multiple nestmaps')
                obj.vlmSettings.bake_nestmap = nestmap_index
            island_render = render_data[island_render_group]
            island_group_mask = mask_data[island_render_group]
            target_mask = targets[n]
            target_w = len(target_mask)
            target_h = target_heights[n]
            target_tex = nestmaps[n]

            # Compute lightmap's seam fading mask
            seam_fade = 1.0
            is_lightmap = island_obj.vlmSettings.bake_type == 'lightmap'
            if is_lightmap:
                pts=[]
                pts_col=[]
                lines=[]
                lines_col=[]
                color_layer = bm.loops.layers.color.verify()
                uv_layer = bm.loops.layers.uv[uv_name]
                for face in island['faces']:
                    prev_uv = first_uv = prev_col = first_col = None
                    for loop in face.loops:
                        uv = loop[uv_layer].uv
                        col = loop[color_layer]
                        pts.append(uv)
                        pts_col.append(col)
                        if prev_uv:
                            lines.append(prev_uv)
                            lines_col.append(prev_col)
                            lines.append(uv)
                            lines_col.append(col)
                        else:
                            first_uv = uv
                            first_col = col
                        prev_uv = uv
                        prev_col = col
                    lines.append(prev_uv)
                    lines_col.append(prev_col)
                    lines.append(first_uv)
                    lines_col.append(first_col)
                with offscreen.bind():
                    fb = gpu.state.active_framebuffer_get()
                    fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                    shader_draw.bind()
                    tri_batch = batch_for_shader(shader_draw, 'TRIS', {"pos": pts, "col": pts_col})
                    pt_batch = batch_for_shader(shader_draw, 'POINTS', {"pos": pts, "col": pts_col})
                    line_batch = batch_for_shader(shader_draw, 'LINES', {"pos": lines, "col": lines_col})
                    for px in sorted(range(-padding, padding+1), key=lambda x:abs(x), reverse=True):
                        for py in sorted(range(-padding, padding+1), key=lambda x:abs(x), reverse=True):
                            shader_draw.uniform_float("ofs", (px/float(src_w), py/float(src_h)) )
                            tri_batch.draw(shader_draw)
                            pt_batch.draw(shader_draw)
                            line_batch.draw(shader_draw)
                seam_data = offscreen.texture_color.read()
                seam_data.dimensions = src_w * src_h * 4
                
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
                                if island_group_mask[p2+3] < padding_threshold: # border point: search nearest non border point
                                    p2 = get_nearest_opaque_pos(island_group_mask, dx, dy, padding + 1, src_w, src_h)
                                if is_lightmap:
                                    s2 = p2
                                    if seam_data[s2+3] < seam_threshold: # outside of seam mask: search nearest seam mask point
                                        s2 = get_nearest_opaque_pos(seam_data, dx, dy, padding + 1, src_w, src_h)
                                    seam_fade = seam_data[s2] / 255.0
                                target_tex[p+0] = (island_render[p2+0] + rgb_offset) * rgb_scale * seam_fade
                                target_tex[p+1] = (island_render[p2+1] + rgb_offset) * rgb_scale * seam_fade
                                target_tex[p+2] = (island_render[p2+2] + rgb_offset) * rgb_scale * seam_fade
                                #target_tex[p+0] = target_tex[p+1] = target_tex[p+2] = seam_fade
                                target_tex[p+3] =  island_render[p2+3]
                                if island_render[p2+3] < 1: with_alpha = True
    render_data.clear()

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
        Image.open(path_png).save(path_webp, format = "WebP", lossless = True)
        print(f'. Texture #{i} has a size of {target_w}x{target_h} for a fill rate of {1.0 - (filled/(target_w*target_h)):>6.2%} (alpha: {with_alpha})')
    bpy.data.scenes.remove(scene)
    print(f'. Nest map generated and saved to {base_filepath}')


def prepare_nesting(obj, render_size, padding, uv_name):
    src_w, src_h = render_size
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv[uv_name]
    ftv, vtf = create_vert_face_db([f for f in bm.faces], uv_layer)
    islands = get_island(bm, ftv, vtf, uv_layer)
    islands = get_merged_overlapping_islands(islands, uv_layer)
    
    # Compute island masks by rendering masks then creating a simplified span view
    offscreen = gpu.types.GPUOffScreen(src_w, src_h)
    vertex_shader = 'in vec2 pos; uniform vec2 ofs; void main() { gl_Position = vec4(2.0 * (pos + ofs) - vec2(1.0), 0.0, 1.0); }'
    fragment_shader = 'out vec4 FragColor; void main() { FragColor = vec4(1.0); }'
    shader_draw = gpu.types.GPUShader(vertex_shader, fragment_shader)
    gpu.state.blend_set('NONE')
    total_pix_count = 0
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
            for px in range(-padding, padding+1):
                for py in range(-padding, padding+1):
                    shader_draw.uniform_float("ofs", (px/float(src_w), py/float(src_h)) )
                    tri_batch.draw(shader_draw)
                    pt_batch.draw(shader_draw)
                    line_batch.draw(shader_draw)
        buffer = offscreen.texture_color.read()
        buffer.dimensions = src_w * src_h * 4

        island_pix_count = 0
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
                        ymin = -1
            if ymin != -1:
                spans.append((ymin, island_h-1, island_h-1-ymin + 1))
                island_pix_count = island_pix_count + (island_h-1-ymin + 1)
            island_hor.append(spans)
        total_pix_count = total_pix_count + island_pix_count
        
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
        island_masks.append(island_hor) # 0 rotation
        island_masks.append([span for span in reversed(island_ver)]) # 90 rotation
        island_masks.append([span for span in reversed(island_hor)]) # 0 rotation, Flipped on X
        island_masks.append(island_ver) # 90 rotation, Flipped on X

        island['source'] = (obj, bm)
        island['masks'] = island_masks
        island['pixcount'] = island_pix_count
        
    offscreen.free()
    print(f'. Nesting prepared ({len(islands):>3} islands, {total_pix_count:>7}px, {src_w}x{src_h} renders) for {obj.name}')
    return NestBlock(obj, bm, islands, total_pix_count)


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
    
    
def perform_nesting(islands, uv_name, src_w, src_h, tex_w, tex_h, padding, only_one_page=False):
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
                            return NestMap(src_w, src_h, padding, islands, [], [])
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
        uv_layer = bm.loops.layers.uv[uv_name]
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
    
    return NestMap(src_w, src_h, padding, islands, targets, target_heights)



