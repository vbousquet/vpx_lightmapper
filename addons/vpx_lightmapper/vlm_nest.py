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
        island = {'faces': current_island, 'mat_index': current_island[0].material_index}
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
        other_islands = [i for i in merged_islands if island['mat_index'] == i['mat_index'] and island_bounds_overlaps(island, i)]
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
                        merged = True
                        other_island['faces'].extend(island['faces'])
                        update_island_bounds(other_island, uv_layer)
                        break
                if merged:
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

import collections

NestBlock = namedtuple("NestBlock", "obj bm islands pix_count")
NestMap = namedtuple("NestMap", "padding islands targets target_heights")

def nest(context, objects, uv_bake_name, uv_nest_name, tex_w, tex_h, nestmap_name, nestmap_offset):
    '''Perform nesting of a group of objects to a minimal (not optimal) set of nestmaps
    Eventually splitting objects that can't fit into a single nestmap.
    '''
    pack_threshold = int(tex_w * tex_h * 0.85)
    # adds one to the padding to take in account uv positionning inside pixel
    padding = context.scene.vlmSettings.padding + 1
    start_time = time.time()

    # Evaluate all islands with their masks
    tick_time = time.time()
    islands_to_pack = []
    render_sizes = {}
    
    to_prepare = [o for o in objects]
    while to_prepare:
        obj = to_prepare.pop()
        obj.vlmSettings.bake_nestmap = -1
        r, v = prepare_nesting(context, obj, padding, uv_bake_name, render_sizes, tex_w, tex_h)
        if r == 'FAILED':
            return None
        elif r == 'SPLITTED':
            to_prepare.extend(v)
        elif r == 'SUCCESS':
            islands_to_pack.append(v)
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
                uv_layer = bm.loops.layers.uv[uv_nest_name]
                for face in island['faces']:
                    for loop in face.loops:
                        uv_undo.append(Vector(loop[uv_layer].uv))

            nestmap = perform_nesting(selected_islands, uv_nest_name, tex_w, tex_h, padding, only_one_page=(len(selection) > 1))
            if len(nestmap.targets) == 1:
                print(f'. Nesting succeeded.')
                # Success: store result for later nestmap render
                tick_time = time.time()
                render_nestmap(context, selection, uv_bake_name, nestmap, nestmap_name, nestmap_offset + nestmap_index)
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
                        uv_layer = bm.loops.layers.uv[uv_nest_name]
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
                    padding, islands, targets, target_heights = nestmap
                    print(f'. Object {obj.name} did not fit on a single page. Splitting it.')
                    
                    # Gather faces that did not fit on the first page
                    remaining_faces = [] # The indices of faces that did not fit on the first page
                    nested_faces = [] # The indices of faces that did fit on the first page
                    processed_islands = []
                    remaining_islands = []
                    processed_pix_count = 0
                    bm.faces.ensure_lookup_table()
                    for island in block_islands:
                        if island['place'][0] == 0:
                            processed_islands.append(island)
                            for face in island['faces']:
                                nested_faces.append(face.index)
                            processed_pix_count = processed_pix_count + island['pixcount']
                        else:
                            remaining_islands.append(island)
                            for face in island['faces']:
                                remaining_faces.append(face.index)

                    # duplicate the packed object mesh
                    dup = obj.copy()
                    dup.data = obj.data.copy()
                    bm2 = bm.copy()
                    bm2.faces.ensure_lookup_table()
                    
                    # Reset uv of faces still to be nested
                    index = 0
                    uv_layer = bm2.loops.layers.uv[uv_nest_name]
                    for face in bm2.faces:
                        for loop in face.loops:
                            loop[uv_layer].uv = uv_undo[index]
                            index = index + 1

                    # Adjust data of remaining islands
                    for island in remaining_islands:
                        island['source'] = (dup, bm2)
                        island['faces'] = [bm2.faces[face.index] for face in island['faces']]

                    # Create new object to be nested
                    bmesh.ops.delete(bm2, geom=[bm2.faces[i] for i in nested_faces], context='FACES')
                    bm2.to_mesh(dup.data)
                    [col.objects.link(dup) for col in obj.users_collection]
                    dup.vlmSettings.bake_nestmap = -1

                    # Prepare nesting of the remaining islands
                    r, v = prepare_nesting(context, dup, padding, uv_bake_name, render_sizes, tex_w, tex_h)
                    if r == 'SUCCESS':
                        print(f'. {len(remaining_islands)} islands were splitted, and still need to be nested.')
                        islands_to_pack.append(v)
                    else:
                        print(f'. nesting the remaining island failed.')

                    # Create object with the nested faces
                    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in remaining_faces], context='FACES')
                    bm.to_mesh(obj.data)

                    # Render the resulting nestmap
                    nestmap = NestMap(padding, processed_islands, targets[0:1], target_heights[0:1])
                    tick_time = time.time()
                    render_nestmap(context, [NestBlock(obj, None, processed_islands, processed_pix_count)], uv_bake_name, nestmap, nestmap_name, nestmap_offset + nestmap_index)
                    render_length = time.time() - tick_time
                    nestmap_index = nestmap_index + 1
                    print(f'. {len(processed_islands)} islands were nested on the first page and kept.')
                    break

    # Free unprocessed data if any
    for block in islands_to_pack:
        block.bm.free()
    total_length = time.time() - start_time
    print(f'. Nestmapping finished ({n_failed} overflow were handled for {nestmap_index} generated nestmaps) in {str(datetime.timedelta(seconds=total_length))} (prepare={str(datetime.timedelta(seconds=prepare_length))}, nest={str(datetime.timedelta(seconds=total_length-prepare_length-render_length))}, render={str(datetime.timedelta(seconds=render_length))}).')
        
    return (nestmap_index, splitted_objects)


def cache_get(cache, image_path):
    cached = next((c for c in cache if c[0] == image_path), None)
    if cached is None:
        image = vlm_utils.get_image_or_black(image_path, black_is_none=True)
        cache.append((image_path, image))
        # Unload oldest bake if too much loaded at the same time
        if len(cache) > 32:
            path, (loaded, render) = cache.pop(0)
            if render and loaded == 'loaded':
                bpy.data.images.remove(render)
        loaded, render = image
        return render
    else:
        loaded, render = cached[1]
        return render
 

def cache_clear(cache):
    for path, (loaded, render) in cache:
        if render and loaded == 'loaded':
            bpy.data.images.remove(render)
    cache.clear()


def render_nestmap(context, selection, uv_bake_name, nestmap, nestmap_name, nestmap_index):
    padding, islands, targets, target_heights = nestmap
    n_render_groups = vlm_utils.get_n_render_groups(context)
    nestmaps = [np.zeros((len(target) * height * 4), 'f') for target, height in zip(targets, target_heights)]
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')

    # Offscreen surface where the nestmaps are rendered
    offscreen_renders = []
    has_alpha = []
    for target, height in zip(targets, target_heights):
        offscreen_render = gpu.types.GPUOffScreen(len(target), height, format='RGBA16F')
        with offscreen_render.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        offscreen_renders.append(offscreen_render)
        has_alpha.append(False)
    render_vs = '''
        in vec2 pos; 
        out vec2 uv;
        uniform vec2 src_size;
        uniform vec2 dst_size;
        uniform float ref_width;
        uniform vec2 pos_ref;
        uniform vec2 pos_dec;
        uniform int rot;
        void main() {
            gl_Position = vec4(2 * pos - vec2(1.0), 0.0, 1.0);
            vec2 p = pos * dst_size - pos_dec;
            if (rot == 1) {
                p = vec2(p.y, ref_width - p.x); // 90 rotation
            } else if (rot == 2) {
                p = vec2(ref_width - p.x, p.y); // Flipped on X
            } else if (rot == 3) {
                p = vec2(p.y, p.x); // 90 rotation, Flipped on X
            }
            uv = (pos_ref + p) / src_size;
        }
        '''
    render_fs = '''
        in vec2 uv;
        out vec4 FragColor;
        uniform sampler2D seam_mask;
        uniform sampler2D render_mask;
        uniform sampler2D render;
        uniform vec2 src_size;
        uniform int padding;
        void main() {
            if (uv.x < 0.0 || uv.x >= 1.0 || uv.y < 0.0 || uv.y >= 1.0)
                FragColor = vec4(0.0);
            else
            {
                vec2 min_uv = vec2(0.5/src_size);
                vec2 max_uv = vec2(1.0 - 0.5/src_size);
                float seam_sum = 0.0;
                vec4 seam = vec4(0.0); // RGB is lightmap seam fading, alpha is object mask including padding
                vec4 padding_accum = vec4(0.0); // masked weighted (by mask alpha and distance) average of the RGBA color
                float padding_sum = 0.0;
                float distance_to_outside = 2.0 * padding * padding;
                for (int i = -padding; i <= padding; i++)
                {
                    for (int j = -padding; j <= padding; j++)
                    {
                        vec2 uv_ofs = uv + vec2(i, j) / src_size;
                        uv_ofs = clamp(uv_ofs, min_uv, max_uv);
                        vec2 v = vec2(i, j);
                        float dist = dot(v, v);
                        float dist_factor = 1.0 / (1.0 + dist * dist);
                        // evaluate if this texel is part of the padded island as well as seam fading
                        seam += texture(seam_mask, uv_ofs) * dist_factor;
                        seam_sum += dist_factor;
                        if (texture(render_mask, uv_ofs).a < 1.0)
                        { // Outside of part or inside border area: update distance to outside
                            distance_to_outside = min(distance_to_outside, dist);
                        }
                        else
                        { // Only accumulate outside of border (mask < 1.0) since it would contain border fade as well mixed with part transparency
                            padding_accum += texture(render, uv_ofs) * dist_factor;
                            padding_sum += dist_factor;
                        }
                    }
                }
                if (padding_sum == 0.0)
                { // Fully outside, with not a single pixel of the island => discard texel
                    FragColor = vec4(0.0);
                }
                else
                { // Either inside (if inside == 1), or in the border area (if 0 < inside < 1) => lerp between inside color and padding color
                    float inside = smoothstep(0.0, 2.0, sqrt(distance_to_outside)); // Fixed 2 pixel border/interior fading
                    seam = seam / seam_sum;
                    seam.a = step(0.001, seam.a); // binary island mask
                    FragColor = seam * mix(padding_accum / padding_sum, texture(render, uv), inside);
               }
            }
        }'''
    render_shader = gpu.types.GPUShader(render_vs, render_fs)
    render_batch = batch_for_shader(render_shader, 'TRIS', { "pos": ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)) }, )
        
    # Offscreen surface where we render the seam fading mask for lightmaps
    offscreen_seams = None
    seams_vs = '''
        in vec2 pos; 
        in vec4 col; 
        uniform vec2 ofs; 
        out vec4 colInterp; 
        void main() 
        {        
            colInterp = col; 
            gl_Position = vec4(2.0 * (pos + ofs) - vec2(1.0), 0.0, 1.0); 
        }'''
    seams_fs = '''
        in vec4 colInterp; 
        out vec4 FragColor; 
        void main() 
        { 
            FragColor = colInterp; 
        }'''
    seams_shader = gpu.types.GPUShader(seams_vs, seams_fs)

    full_white_mask = bpy.data.images.new('Full White', 1, 1, alpha=False)
    full_white_mask.pixels = (1.0, 1.0, 1.0, 1.0)

    # Load the render masks
    image_cache = []
    for obj_name in sorted(list({obj.name for (obj, _, _, _) in selection}), key=lambda x:bpy.data.objects[x].vlmSettings.bake_lighting):
        obj = bpy.data.objects[obj_name]
        print(f'. Copying renders (HDR range={obj.vlmSettings.bake_hdr_range:>7.2f}) for object {obj.name} from {obj.vlmSettings.bake_lighting} renders')

        # Render to the packed nest map
        for island in islands:
            island_obj, bm = island['source']
            if island_obj != obj: continue
            n, x, y, rot = island['place']
            src_w = island['src_w']
            src_h = island['src_h']
            mask = island['masks'][rot]
            mask_w = len(mask)
            min_x, min_y = island['min_i']
            if n > 0: # Skip islands that were nested to secondary pages: they have been splitted to other objects
                continue
            if obj.vlmSettings.bake_nestmap != nestmap_index:
                if obj.vlmSettings.bake_nestmap != -1:
                    print(f'ERROR: object {obj.name} was not splitted but has parts on multiple nestmaps')
                obj.vlmSettings.bake_nestmap = nestmap_index
            
            # Loaded the bake and mask if not already loaded and cached
            render_path = vlm_utils.get_packmap_bakepath(context, island_obj.data.materials[island['mat_index']])
            island_render = cache_get(image_cache, render_path)
            if island_render is None:
                print('. No render (likely uninfluenced lightmap), skipping island')
                continue
            #FIXME for traditional bake, use the solid bake alpha channel ?
            render_id = island_obj.data.materials[island['mat_index']].get('VLM.Render')
            if isinstance(render_id, int):
                island_render_mask = cache_get(image_cache, f'{mask_path}Mask - Group {render_id}.png')
            else:
                island_render_mask = full_white_mask
            
            target_w = len(targets[n])
            target_h = target_heights[n]

            # Compute render mask, including lightmap's seam fading
            if island_obj.vlmSettings.bake_type != 'lightmap': has_alpha[n] = True # This could be improved to detect non opaque bakemap
            pts=[]
            pts_col=[]
            lines=[]
            lines_col=[]
            color_layer = bm.loops.layers.color.verify()
            uv_layer = bm.loops.layers.uv[uv_bake_name]
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
            gpu.state.blend_set('NONE')
            if offscreen_seams is None or offscreen_seams.width != src_w or offscreen_seams.height != src_h:
                if offscreen_seams is not None: offscreen_seams.free()
                offscreen_seams = gpu.types.GPUOffScreen(src_w, src_h, format='RGBA8')
            with offscreen_seams.bind():
                fb = gpu.state.active_framebuffer_get()
                fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                tri_batch = batch_for_shader(seams_shader, 'TRIS', {"pos": pts, "col": pts_col})
                pt_batch = batch_for_shader(seams_shader, 'POINTS', {"pos": pts, "col": pts_col})
                line_batch = batch_for_shader(seams_shader, 'LINES', {"pos": lines, "col": lines_col})
                seams_shader.bind()
                for px in sorted(range(-padding, padding+1), key=lambda x:abs(x), reverse=True):
                    for py in sorted(range(-padding, padding+1), key=lambda x:abs(x), reverse=True):
                        seams_shader.uniform_float("ofs", (px/float(src_w), py/float(src_h)) )
                        tri_batch.draw(seams_shader)
                        pt_batch.draw(seams_shader)
                        line_batch.draw(seams_shader)
                # image_data = fb.read_color(0, 0, src_w, src_h, 4, 0, 'UBYTE')
                # image_data.dimensions = src_w * src_h * 4
                # if 'Debug' not in bpy.data.images:
                    # bpy.data.images.new('Debug', src_w, src_h, alpha=True, float_buffer=False)
                # pack_image = bpy.data.images['Debug']
                # pack_image.scale(src_w, src_h)
                # pack_image.pixels = [v / 255 for v in image_data]

            # Copy the render, applying offset, rotation, flipping, masking, border/padding fading, and lightmap seam fading
            gpu.state.blend_set('ALPHA')
            with offscreen_renders[n].bind():
                render_shader.bind()
                render_shader.uniform_float("src_size", (src_w, src_h))
                render_shader.uniform_float("dst_size", (target_w, target_h))
                render_shader.uniform_float("ref_width", mask_w)
                render_shader.uniform_float("pos_ref", (min_x - padding, min_y - padding))
                render_shader.uniform_float("pos_dec", (x, y))
                render_shader.uniform_int("rot", rot)
                render_shader.uniform_int("padding", padding)
                render_shader.uniform_sampler("render_mask", gpu.texture.from_image(island_render_mask))
                render_shader.uniform_sampler("seam_mask", offscreen_seams.texture_color)
                render_shader.uniform_sampler("render", gpu.texture.from_image(island_render))
                render_batch.draw(render_shader)
                # fb = gpu.state.active_framebuffer_get()
                # image_data = fb.read_color(0, 0, target_w, target_h, 4, 0, 'UBYTE')
                # image_data.dimensions = target_w * target_h * 4
                # if 'Debug-Out' not in bpy.data.images:
                    # bpy.data.images.new('Debug-Out', target_w, target_h, alpha=True, float_buffer=True)
                # pack_image = bpy.data.images['Debug-Out']
                # pack_image.scale(target_w, target_h)
                # pack_image.pixels = [v / 255 for v in image_data]

    # Cleanup loaded images
    cache_clear(image_cache)
    bpy.data.images.remove(full_white_mask)

    # Save the rendered nestmaps
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    base_filepath = f'{vlm_utils.get_bakepath(context, type="EXPORT")}{nestmap_name} {nestmap_index}'
    for i, target in enumerate(targets):
        target_w = len(target)
        target_h = target_heights[i]

        image_data = offscreen_renders[i].texture_color.read()
        image_data.dimensions = target_w * target_h * 4
        pack_image = bpy.data.images.new(f'Nest {i}', target_w, target_h, alpha=has_alpha[i], float_buffer=True)
        pack_image.pixels = [v for v in image_data]
        
        if len(targets) > 1:
            path_exr = bpy.path.abspath(f'{base_filepath} {i}.exr')
            path_png = bpy.path.abspath(f'{base_filepath} {i}.png')
            path_webp = bpy.path.abspath(f'{base_filepath} {i}.webp')
        else:
            path_exr = bpy.path.abspath(f'{base_filepath}.exr')
            path_png = bpy.path.abspath(f'{base_filepath}.png')
            path_webp = bpy.path.abspath(f'{base_filepath}.webp')
        scene.render.image_settings.color_mode = 'RGBA' if has_alpha[i] else 'RGB'
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

        filled = 0
        for x in range(target_w):
            for span in target[x]:
                if span[0] < target_h:
                    filled += min(target_h - 1, span[1]) - span[0] + 1
        print(f'. Texture #{i} has a size of {target_w}x{target_h} for a fill rate of {1.0 - (filled/(target_w*target_h)):>6.2%} (alpha: {has_alpha[i]})')
    bpy.data.scenes.remove(scene)
    print(f'. Nest map generated and saved to {base_filepath}')


def prepare_nesting(context, obj, padding, uv_nest_name, render_sizes, tex_w, tex_h):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv[uv_nest_name]

    # Identify islands (faces sharing the same render id linked with respect to uv)
    ftv, vtf = create_vert_face_db([f for f in bm.faces], uv_layer)
    islands = get_island(bm, ftv, vtf, uv_layer)
    islands = get_merged_overlapping_islands(islands, uv_layer)

    # Compute island masks by rendering masks then creating a simplified span view
    offscreen = None
    vertex_shader = 'in vec2 pos; uniform vec2 ofs; void main() { gl_Position = vec4(2.0 * (pos + ofs) - vec2(1.0), 0.0, 1.0); }'
    fragment_shader = 'out vec4 FragColor; void main() { FragColor = vec4(1.0); }'
    shader_draw = gpu.types.GPUShader(vertex_shader, fragment_shader)
    gpu.state.blend_set('NONE')
    total_pix_count = 0
    for index, island in enumerate(islands, start=1):
        render_path = vlm_utils.get_packmap_bakepath(context, obj.data.materials[island['faces'][0].material_index])
        render_size = render_sizes.get(render_path)
        if render_size is None:
            im = bpy.data.images.load(render_path, check_existing=False)
            render_size = (im.size[0], im.size[1])
            render_sizes[render_path] = render_size
            bpy.data.images.remove(im)
        src_w, src_h = render_size
        island_min = island['min']
        island_max = island['max']
        island_size = island['size']
        island_render_id = island['mat_index']
        island['src_w'] = src_w
        island['src_h'] = src_h
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
        
        # Check if the island exceed a single texture page and if so, split it
        if island_w > tex_w or island_h > tex_h:
            sel_max_uv = None
            sel_min_uv = None
            max_uv = Vector((-10000000.0, -10000000.0))
            min_uv = Vector((10000000.0, 10000000.0))
            selected_faces = []
            unselected_faces = [f.index for f in bm.faces]
            for face in island['faces']:
                for l in face.loops:
                    uv = l[uv_layer].uv
                    max_uv.x = max(uv.x, max_uv.x)
                    max_uv.y = max(uv.y, max_uv.y)
                    min_uv.x = min(uv.x, min_uv.x)
                    min_uv.y = min(uv.y, min_uv.y)
                min_x = math.floor(min_uv.x * src_w)
                min_y = math.floor(min_uv.y * src_h)
                max_x = math.ceil(max_uv.x * src_w)
                max_y = math.ceil(max_uv.y * src_h)
                island_w = min(src_w, max(1, max_x - min_x + 2*padding))
                island_h = min(src_h, max(1, max_y - min_y + 2*padding))
                if sel_max_uv is None or (island_w <= tex_w and island_h <= tex_h):
                    selected_faces.append(face.index)
                    unselected_faces.remove(face.index)
                    sel_min_uv = min_uv.copy()
                    sel_max_uv = max_uv.copy()
                max_uv = sel_max_uv.copy()
                min_uv = sel_min_uv.copy()
            if selected_faces:
                print(f'. Object {obj.name} has parts that do not fit in the target texture. It has been splitted according to the texture settings.')
                bm2 = bm.copy()
                bm2.faces.ensure_lookup_table()
                
                bmesh.ops.delete(bm, geom=[bm.faces[i] for i in selected_faces], context='FACES')
                bm.to_mesh(obj.data)
                bm.free()

                dup = obj.copy()
                dup.data = obj.data.copy()
                bmesh.ops.delete(bm2, geom=[bm2.faces[i] for i in unselected_faces], context='FACES')
                bm2.to_mesh(dup.data)
                bm2.free()
                [col.objects.link(dup) for col in obj.users_collection]
                return ('SPLITTED', (obj, dup))
            else:
                # We did not find a face that fits in the texture. No splitting is possible, just fail
                print(f'. Object {obj} has a face that do not fit in the target texture. Nestmapping can not be achieved.')
                return ('FAILED', None)
        
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

        if offscreen is None or offscreen.width != src_w or offscreen.height != src_h:
            if offscreen is not None: offscreen.free()
            offscreen = gpu.types.GPUOffScreen(src_w, src_h)
            
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
    return ('SUCCESS', NestBlock(obj, bm, islands, total_pix_count))


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
    
    
def perform_nesting(islands, uv_nest_name, tex_w, tex_h, padding, only_one_page=False):
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
                            return NestMap(padding, islands, [], [])
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
        uv_layer = bm.loops.layers.uv[uv_nest_name]
        n, x, y, rot = island['place']
        min_x, min_y = island['min_i']
        max_x, max_y = island['max_i']
        src_w = island['src_w']
        src_h = island['src_h']
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
    
    return NestMap(padding, islands, targets, target_heights)



