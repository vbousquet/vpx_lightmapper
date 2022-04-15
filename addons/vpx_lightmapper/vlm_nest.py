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


def nest(context, objects, render_size, tex_w, tex_h):
    '''Perform nesting of a group of objects to a minimal (not optimal) set of nestmaps
    Eventually splitting objects that can't fit into a single nestmap.
    '''
    pack_threshold = int(tex_w*tex_h*0.9)
    src_w, src_h = render_size
    # adds one to the padding to take in account uv positionning inside pixel
    padding = context.scene.vlmSettings.padding + 1

    # Evaluate all islands with their masks
    islands_to_pack = []
    for obj in objects:
        islands_to_pack.append(prepare_nesting(obj, render_size, padding))

    # Nest groups of islands into nestmaps
    to_render = []
    while islands_to_pack:
        # Sort from biggest to smallest
        islands_to_pack.sort(key=lambda p: p[3], reverse=True)

        # Select island groups
        pixcount = 0
        selection = []
        for block in islands_to_pack:
            if pixcount == 0 or pixcount + block[3] <= pack_threshold:
                selection.append(block)
                pixcount += block[3]

        while True:
            pixcount = 0
            selected_islands = []
            for block in selection:
                pixcount += block[3]
                selected_islands.extend(block[2])
            selection_names = [block[0].name for block in selection]
            uv_undo = []
            for island in selected_islands:
                obj, bm = island['source']
                uv_layer = bm.loops.layers.uv.verify()
                for face in island['faces']:
                    for loop in face.loops:
                        uv_undo.append(Vector(loop[uv_layer].uv))

            print(f'Trying to nest in a single texture the {len(selected_islands)} islands ({pixcount/float(tex_w*tex_h):6.2%} fill with {pixcount} px / {tex_w*tex_h} content) of {selection_names}')
            nestmap = perform_nesting(selected_islands, src_w, src_h, tex_w, tex_h, padding, only_one_page=True)
            _, _, _, nested_islands, targets, _ = nestmap
            if len(targets) == 1:
                print(f'Nesting of {selection_names} succeeded.')
                # Success: apply UV and render packmap
                to_render.append( (selection, nestmap) )
                for block in selection:
                    islands_to_pack.remove(block)
                    obj, bm, _, _ = block
                    bm.to_mesh(obj.data)
                    bm.free()
                break
            else:
                print(f'Nesting of {selection_names} overflowed from a single texture page')
                # reset uv
                index = 0
                for island in selected_islands:
                    obj, bm = island['source']
                    uv_layer = bm.loops.layers.uv.verify()
                    for face in island['faces']:
                        for loop in face.loops:
                            loop[uv_layer].uv = uv_undo[index]
                            index = index + 1
                # remove last block and start again with a smaller group
                if len(selection) > 1:
                    selection.pop()
                else:
                    # it is the first block (the largest) of the selection then. We need to split the block's object between the parts on page 1 and the rest, keeping the page 1 (no big interest of re-nesting first page)
                    print(f'Object {selection[0].name} does not fit on a single page. It needs to be splitted.')
                    print('Not implemented yet')
                    return {'FINISHED'}

    # Free unprocessed data if any
    for (obj, bm, islands, obj_pixcount) in islands_to_pack:
        bm.free()
        
    # Render nestmaps
    n_render_groups = vlm_utils.get_n_render_groups(context)
    for nestmap_index, (selection, nestmap) in enumerate(to_render):
        print(f'Rendering nestmap #{nestmap_index+1}/{len(to_render)}')
        src_w, src_h, padding, islands, targets, target_heights = nestmap
        nestmaps = [np.zeros((len(target) * height * 4), 'f') for target, height in zip(targets, target_heights)]
        for obj_name in {obj.name for (obj, _, _, _) in selection}:
            print(f'. Copying renders for object {obj_name}')
            # Load the render 
            obj = bpy.data.objects[obj_name]
            render_data = []
            for i in range(n_render_groups):
                path = f"{vlm_utils.get_bakepath(context, type='RENDERS')}{obj.vlmSettings.bake_name} - Group {i}.exr"
                loaded, render = vlm_utils.get_image_or_black(path, black_is_none=True)
                if render:
                    pixel_data = np.zeros((src_w * src_h * 4), 'f') # using numpy is way faster
                    render.pixels.foreach_get(pixel_data)
                    render_data.append(pixel_data)
                    if loaded == 'loaded': bpy.data.images.remove(render)
                else:
                    render_data.append(None)
            # Render to the packed nest map
            with_alpha = False if obj.vlmSettings.bake_type == 'lightmap' else True
            for island in islands:
                n, x, y, rot = island['place']
                unpadded_mask = island['unpadded_mask']
                mask = island['masks'][rot]
                mask_w = len(mask)
                min_x, min_y = island['min_i']
                island_render_group = island['render_group']
                if island_render_group < 0 or island_render_group >= len(render_data) or render_data[island_render_group] is None:
                    print('. Missing render group, skipping island')
                    continue
                island_render = render_data[island_render_group]
                target_mask = targets[n]
                target_w = len(target_mask)
                target_h = target_heights[n]
                target_tex = nestmaps[n]

                # Identify opaque islands to process padding accordingly by fixing alpha on rendered borders
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
                                    for j in range(4):
                                        target_tex[p+j] = island_render[p2+j]
                                        # target_tex[p+j] = 1
                                    if is_opaque and island_render[p2+3] < 1:
                                        # TODO find nearest opaque color and replace (blended)
                                        target_tex[p+3] = 1

        # Save the rendered nestmaps
        scene = bpy.data.scenes.new('VLM.Tmp Scene')
        scene.view_settings.view_transform = 'Raw'
        scene.view_settings.look = 'None'
        base_filepath = f'{vlm_utils.get_bakepath(context, type="EXPORT")}Packmap {nestmap_index}'
        for i, target in enumerate(targets):
            target_w = len(target)
            target_h = target_heights[i]
            filled = 0
            #tex = np.ones((target_w * target_h * 4), 'f')
            for x in range(target_w):
                for span in target[x]:
                    filled += span[2]
                    # for y in range(span[0], span[1] + 1):
                        # for j in range(4):
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
            # Saving through save_render would save  a linear PNG, not an sRGB one which is needed by VPX
            pack_image.filepath_raw = path_png
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
            Image.open(path_png).save(path_webp, 'WEBP')

            print(f'. Texture #{i} has a size of {target_w}x{target_h} for a fill rate of {1.0 - (filled/(target_w*target_h)):>6.2%} (alpha: {with_alpha})')
        bpy.data.scenes.remove(scene)
        print(f'Nest map generated and saved to {base_filepath}')



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
        total_pix_count = total_pix_count + island_pix_count

        island['source'] = (obj, bm)

        print(f'. Island #{index:>3}/{len(islands)} placement mask computed (size: {island_w:>4}x{island_h:>4}, pixcount: {island_pix_count:>7}px, fill rate: {island_pix_count/(island_w*island_h):>6.1%})')
        
    offscreen.free()
    
    return (obj, bm, islands, total_pix_count)


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
                        if only_one_page: # Faset Fail if packing to a single page
                            island['place'] = (n, x, y, rot) # mark it to identify the first offender
                            print(f'. Island #{index:>3}/{len(islands)} could not be placed (single page mode) pixcount:{island["pixcount"]:>7}px  from {island["source"][0].name}')
                            return (src_w, src_h, padding, islands, [], [])
        island['place'] = (n, x, y, rot)
        print(f'. Island #{index:>3}/{len(islands)} placed on nestmap #{n} at {x:>4}, {y:>4} o:{rot} pixcount:{island["pixcount"]:>7}px  from {island["source"][0].name}')
        
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

    # Crop targets
    target_heights = []
    for target in targets:
        # Remove empty columns on the right
        while len(target)>0 and len(target[-1]) == 1 and target[-1][0][2] == tex_h:
            target.pop()
        # Evaluate upper bound
        ymax = 0
        for spans in target:
            if len(spans) > 0:
                ymax = max(ymax, spans[-1][0] - 1)
            else:
                ymax = tex_h
                break
        target_heights.append(ymax)
        
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
            face.material_index = n
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
                loop[uv_layer].uv = ((x + padding + u)/float(target_w) + n, (y + padding + v)/float(target_h))
    
    return (src_w, src_h, padding, islands, targets, target_heights)
















def nest_(context, obj, render_size, tex_w, tex_h):
    '''Perform UV nesting af the given object inside textures of a size equal or smaller to the given size.
    '''
    padding = context.scene.vlmSettings.padding + 1 # adds one to the padding to take in account uv positionning inside pixel
    src_w, src_h = render_size
    start_time = time.time()
    print(f'Starting nesting of {obj.name} to texture (max size: {tex_w}x{tex_h}, padding: {padding}) from {src_w}x{src_h} renders')

    print('. Identifying and sorting all UV islands with render group splitting')
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    ftv, vtf = create_vert_face_db([f for f in bm.faces], uv_layer)
    islands = get_island(bm, ftv, vtf, uv_layer)
    islands = sorted(islands, key=lambda p:p['bb_area'], reverse=True)

    # Compute island masks (by rendering masks)
    offscreen = gpu.types.GPUOffScreen(src_w, src_h)
    offscreen2 = gpu.types.GPUOffScreen(src_w, src_h)
    vertex_shader = 'in vec2 pos; void main() { gl_Position = vec4(2.0 * pos - vec2(1.0), 0.0, 1.0); }'
    fragment_shader = 'out vec4 FragColor; void main() { FragColor = vec4(1.0); }'
    shader_draw = gpu.types.GPUShader(vertex_shader, fragment_shader)
    vertex_shader = 'in vec2 position; in vec2 uv; out vec2 uvInterp; void main() { uvInterp = uv; gl_Position = vec4(position, 0.0, 1.0); }'
    fragment_shader = '''
        uniform sampler2D image;
        uniform float deltaU;
        uniform float deltaV;
        in vec2 uvInterp;
        out vec4 FragColor;
        void main() {
            vec4 t = vec4(0.0);
            for (int y=-1; y<=1; y++) {
                for (int x=-1; x<=1; x++) {
                    t = t + texture(image, uvInterp + vec2(x, y) * vec2(deltaU, deltaV)).rgba;
                }
            }
            FragColor = t;
        }
    '''
    shader_pad = gpu.types.GPUShader(vertex_shader, fragment_shader)
    for index, island in enumerate(islands, start=1):
        island_min = island['min']
        island_max = island['max']
        island_size = island['size']
        island_render_group = island['render_group']
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
        
        if island_w > tex_w or island_h > tex_h:
            print(f'. Island #{index:>3}/{len(islands)} size is {island_w}x{island_h} and cannot be placed in a {tex_w}x{tex_h} texture, skipping island')
            continue
        print(f'. Island #{index:>3}/{len(islands)} size is {island_w}x{island_h} including padding - generating placement mask')
        
        # Render the island and create a discrete tuple model (vertical opaque spans) and a list of its column order
        pts=[]
        lines=[]
        for face in island['faces']:
            prev = first = None
            for loop in face.loops:
                uv = loop[uv_layer].uv
                pts.append(((uv[0]-ofs_u) + padding/float(src_w), (uv[1]-ofs_v) + padding/float(src_h)))
                if prev:
                    lines.append(prev)
                    lines.append(uv)
                else:
                    first = uv
                prev = uv
                lines.append(prev)
                lines.append(first)
        gpu.state.blend_set('NONE')
        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
            shader_draw.bind()
            batch_for_shader(shader_draw, 'TRIS', {"pos": pts}).draw(shader_draw)
        
        buffer = offscreen.texture_color.read()
        buffer.dimensions = src_w * src_h * 4
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
            shader_draw.bind()
            batch_for_shader(shader_draw, 'POINTS', {"pos": pts}).draw(shader_draw) 
            batch_for_shader(shader_draw, 'LINES', {"pos": lines}).draw(shader_draw) # needed for 0 width triangles
        
        layers = (offscreen, offscreen2)
        for i in range(padding):
            layers = (layers[1], layers[0])
            with layers[0].bind():
                fb = gpu.state.active_framebuffer_get()
                shader_pad.bind()
                shader_pad.uniform_sampler("image", layers[1].texture_color)
                shader_pad.uniform_float("deltaU", 1.0 / src_w)
                shader_pad.uniform_float("deltaV", 1.0 / src_h)
                batch_for_shader(shader_pad, 'TRI_FAN',
                        {
                            "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                            "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
                        }).draw(shader_pad)
        buffer = layers[0].texture_color.read()
        buffer.dimensions = src_w * src_h * 4

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
        island_ver = []
        for y in range(island_h):
            xmin = -1
            spans = []
            for x in range(island_w):
                if buffer[4 * (x + y * src_w)] > 0:
                    if xmin == -1: xmin = x
                else:
                    if xmin != -1:
                        spans.append((xmin, x-1, x-1-xmin + 1))
                        xmin = -1
            if xmin != -1:
                spans.append((xmin, island_w-1, island_w-1-xmin + 1))
            island_ver.append(spans)
        island_mask = island_hor

        if False:
            print('. Island mask saved')
            pack_image = bpy.data.images.new(f"Island {index}", src_w, src_h, alpha=True)
            pack_image.pixels = [v / 255 for v in buffer]
            pack_image.filepath_raw = bpy.path.abspath(f"//Island {index}.png")
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
        
        island_masks = []
        island['masks'] = island_masks
        island_masks.append(island_hor) # 0 rotation
        island_masks.append([span for span in reversed(island_ver)]) # 90 rotation
        island_masks.append([span for span in reversed(island_hor)]) # 0 rotation, Flipped on X
        island_masks.append(island_ver) # 90 rotation, Flipped on X
    offscreen.free()
    offscreen2.free()

    # Placement algorithm (simple discret bottom left direct placement)
    targets = []
    for index, island in enumerate(islands, start=1):
        island_masks = island['masks']
        if not island_masks: continue
        x = y = n = 0
        n_succeeded = 0
        col_index = 0
        rot_index = 0
        rot_order = [0, 2, 1, 3] if island_w <= island_h else [1, 3, 0, 2]
        #rot_order = [2, 0, 1, 3] if island_w <= island_h else [3, 1, 0, 2]
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
        island['place'] = (n, x, y, rot)
        print(f'. Island #{index:>3}/{len(islands)} placed on nestmap #{n} at {x:>4}, {y:>4} o:{rot} ')
        
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

    # Crop targets
    target_heights = []
    for target in targets:
        # Remove empty columns on the right
        while len(target)>0 and len(target[-1]) == 1 and target[-1][0][2] == tex_h:
            target.pop()
        # Evaluate upper bound
        ymax = 0
        for spans in target:
            if len(spans) > 0:
                ymax = max(ymax, spans[-1][0] - 1)
            else:
                ymax = tex_h
                break
        target_heights.append(ymax)
        
    # Update UV to the new placement
    for island in islands:
        n, x, y, rot = island['place']
        min_x, min_y = island['min_i']
        max_x, max_y = island['max_i']
        target_w = len(targets[n])
        target_h = target_heights[n]
        for face in island['faces']:
            face.material_index = n
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
                loop[uv_layer].uv = ((x + padding + u)/float(target_w) + n, (y + padding + v)/float(target_h))
    
    bm.to_mesh(obj.data)
    bm.free()

    print(f'Nesting of {len(islands)} islands into {len(targets)} textures performed in {str(datetime.timedelta(seconds=time.time() - start_time))}')
    return (src_w, src_h, padding, islands, targets, target_heights)


def render_nestmap_(nestmap, renders, base_filepath, with_alpha=True):
    start_time = time.time()
    src_w, src_h, padding, islands, targets, target_heights = nestmap
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'

    print('. Loading renders')
    render_data = []
    for render in renders:
        if render:
            pixel_data = np.zeros((src_w * src_h * 4), 'f') # using numpy is way faster
            render.pixels.foreach_get(pixel_data)
            render_data.append(pixel_data)
        else:
            render_data.append(None)

    print('. Creating nest maps')
    nestmaps = [np.zeros((len(target) * height * 4), 'f') for target, height in zip(targets, target_heights)]
    for island in islands:
        n, x, y, rot = island['place']
        unpadded_mask = island['unpadded_mask']
        mask = island['masks'][rot]
        mask_w = len(mask)
        min_x, min_y = island['min_i']
        island_render_group = island['render_group']
        if island_render_group < 0 or island_render_group >= len(renders) or render_data[island_render_group] is None:
            print('. Missing render group, skipping island')
            continue
        island_render = render_data[island_render_group]
        target_mask = targets[n]
        target_w = len(target_mask)
        target_h = target_heights[n]
        target_tex = nestmaps[n]

        # Identify opaque islands to process padding accordingly by fixing alpha on rendered borders
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
                            for j in range(4):
                                target_tex[p+j] = island_render[p2+j]
                                # target_tex[p+j] = 1
                            if is_opaque and island_render[p2+3] < 1:
                                # TODO find nearest opaque color and replace (blended)
                                target_tex[p+3] = 1

    # Save the rendered nestmaps
    for i, target in enumerate(targets):
        target_w = len(target)
        target_h = target_heights[i]
        filled = 0
        #tex = np.ones((target_w * target_h * 4), 'f')
        for x in range(target_w):
            for span in target[x]:
                filled += span[2]
                # for y in range(span[0], span[1] + 1):
                    # for j in range(4):
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
        # Saving through save_render would save  a linear PNG, not an sRGB one which is needed by VPX
        pack_image.filepath_raw = path_png
        pack_image.file_format = 'PNG'
        pack_image.save()
        bpy.data.images.remove(pack_image)
        Image.open(path_png).save(path_webp, 'WEBP')

        print(f'. Texture #{i} has a size of {target_w}x{target_h} for a fill rate of {1.0 - (filled/(target_w*target_h)):>6.2%} (alpha: {with_alpha})')
    
    bpy.data.scenes.remove(scene)
    print(f'Nest map generated in {str(datetime.timedelta(seconds=time.time() - start_time))} and saved to {base_filepath}')
