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
from mathutils import Vector
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from . import vlm_utils



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
        uv_island_lists.append({'faces': current_island, 'max': max_uv, 'min': min_uv, 'size': (max_uv - min_uv), 'bb_area': (max_uv.x-min_uv.x)*(max_uv.y-min_uv.y)})
    return uv_island_lists


def create_vert_face_db(faces, uv_layer):
    face_to_verts = defaultdict(set)
    vert_to_faces = defaultdict(set)
    for f in faces:
        for l in f.loops:
            id_ = l[uv_layer].uv.to_tuple(5), l.vert.index
            face_to_verts[f.index].add(id_)
            vert_to_faces[id_].add(f.index)
    return (face_to_verts, vert_to_faces)


## Code for 2D nesting algorithm

def nest_(context):
    print("Starting nesting...")

    tex_w = 1024
    tex_h = 1024
    padding = 3
    scale = tex_h / context.scene.render.resolution_y
    src_w = int(context.scene.render.resolution_x * scale)
    src_h = int(context.scene.render.resolution_y * scale)
    scale_x = src_w / tex_w
    obj = context.object

    print(".Identifying and sorting all UV islands")
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    uv_layer = bm.loops.layers.uv.verify()
    ftv, vtf = create_vert_face_db([f for f in bm.faces], uv_layer)
    islands = get_island(bm, ftv, vtf, uv_layer)
    islands = sorted(islands, key=lambda p:p['bb_area'], reverse=True)

    vertex_shader = 'in vec2 pos; void main() { gl_Position = vec4(2.0 * pos - vec2(1.0), 0.0, 1.0); }'
    fragment_shader = 'out vec4 FragColor; void main() { FragColor = vec4(1.0); }'
    shader = gpu.types.GPUShader(vertex_shader, fragment_shader)
    offscreen = gpu.types.GPUOffScreen(tex_w, tex_h)

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
    shader2 = gpu.types.GPUShader(vertex_shader, fragment_shader)
    offscreen2 = gpu.types.GPUOffScreen(tex_w, tex_h)

    targets = []
    targets.append([[(0, tex_h-1, tex_h-1)] for i in range(tex_w)])
    for index, island in enumerate(islands, start=1):
        print(f'. Processing island #{index:>3}/{len(islands)} (BB area: {scale_x * island["bb_area"]})')
        island_min = island['min']
        island_max = island['max']
        island_size = island['size']
        
        # Render the island and create a discrete tuple model (vertical opaque spans) and a list of its column order
        pts=[]
        lines=[]
        for face in island['faces']:
            prev = first = None
            for loop in face.loops:
                uv = loop[uv_layer].uv
                uv = ((uv[0]-island_min[0])*(src_w-1)/(tex_w-1) + 0.5/tex_w + 3/tex_w, (uv[1]-island_min[1]) + 0.5/tex_h + 3/tex_h)
                pts.append(uv)
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
            shader.bind()
            batch_for_shader(shader, 'TRIS', {"pos": pts}).draw(shader)
            batch_for_shader(shader, 'POINTS', {"pos": pts}).draw(shader)
            batch_for_shader(shader, 'LINES', {"pos": lines}).draw(shader)
        layers = (offscreen, offscreen2)
        for i in range(padding):
            layers = (layers[1], layers[0])
            with layers[0].bind():
                fb = gpu.state.active_framebuffer_get()
                shader2.bind()
                shader2.uniform_sampler("image", layers[1].texture_color)
                shader2.uniform_float("deltaU", 1.0 / tex_w)
                shader2.uniform_float("deltaV", 1.0 / tex_h)
                batch_for_shader(shader2, 'TRI_FAN',
                        {
                            "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                            "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
                        }).draw(shader2)
        buffer = layers[0].texture_color.read()
        buffer.dimensions = tex_w * tex_h * 4

        island_w = min(tex_w, max(1, int(island_size[0]*src_w)) + 2*padding)
        island_h = min(tex_h, max(1, int(island_size[1]*src_h)) + 2*padding)
        island_hor = []
        for x in range(island_w):
            ymin = -1
            spans = []
            for y in range(island_h):
                if buffer[4 * (x + y * tex_w)] > 0:
                    if ymin == -1: ymin = y
                else:
                    if ymin != -1:
                        spans.append((ymin, y-1, y-1-ymin))
                        ymin = -1
            if ymin != -1:
                spans.append((ymin, island_h-1, island_h-1-ymin))
            island_hor.append(spans)
        island_ver = []
        for y in range(island_h):
            xmin = -1
            spans = []
            for x in range(island_w):
                if buffer[4 * (x + y * tex_w)] > 0:
                    if xmin == -1: xmin = x
                else:
                    if xmin != -1:
                        spans.append((xmin, x-1, x-1-xmin))
                        xmin = -1
            if xmin != -1:
                spans.append((xmin, island_w-1, island_w-1-xmin))
            island_ver.append(spans)
        island_mask = island_hor

        if False and scale_x * island["bb_area"] < 1e-07:
            print('. Island mask saved')
            pack_image = bpy.data.images.new(f"Island {index}", tex_w, tex_h, alpha=True)
            pack_image.pixels = [v / 255 for v in buffer]
            pack_image.filepath_raw = bpy.path.abspath(f"//Island {index}.png")
            pack_image.file_format = 'PNG'
            pack_image.save()
            bpy.data.images.remove(pack_image)
        
        island_masks = []
        island_masks.append({'w':island_w, 'h':island_h, 'mask':island_hor}) # 0 rotation
        island_masks.append({'w':island_h, 'h':island_w, 'mask':island_ver}) # 90 rotation
        island_masks.append({'w':island_w, 'h':island_h, 'mask':[span for span in reversed(island_hor)]}) # 0 rotation, Flipped on X
        island_masks.append({'w':island_h, 'h':island_w, 'mask':[span for span in reversed(island_ver)]}) # 90 rotation, Flipped on X
        
        x = y = n = 0
        rot = 0
        n_succeeded = 0
        col_index = 0
        while True:
            col = col_index
            x_start = x
            y_start = y
            mask = island_masks[rot]['mask']
            w = island_masks[rot]['w']
            h = island_masks[rot]['h']
            if n >= len(targets): targets.append([[(0, tex_h-1, tex_h-1)] for i in range(tex_w)])
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
                    rot = rot + 1
                    col_index = 0
                    if rot >= len(island_masks):
                        n = n + 1
                        rot = 0
        # n = rot = 0
        # x = int(tex_w * island_min[0] * (src_w-1)/(tex_w-1) + 3)
        # y = int(tex_h * island_min[1]+ 3)
        
        print(f'  Island placed on page #{n} at {x}, {y} with orientation: {rot} ')
        island['place'] = (n, x, y, rot)
        mask = island_masks[rot]['mask']
        w = island_masks[rot]['w']
        h = island_masks[rot]['h']
        target = targets[n]
        for col in range(w):
            for span in mask[col]:
                for i, check in enumerate(target[x + col]):
                    if span[2] <= check[2] and y+span[1] <= check[1]:
                        target[x + col].pop(i)
                        if y+span[1] < check[1]:
                            target[x + col].insert(i, (y+span[1], check[1], check[1] - (y+span[1])))
                        if check[0] < y+span[0]:
                            target[x + col].insert(i, (check[0], y+span[0], y+span[0] - check[0]))
                        break
    offscreen.free()
    offscreen2.free()

    print(f'. {len(islands)} islands packed into {len(targets)} textures')
    scale_u = (src_w - 1) / (tex_w - 1)
    for island in islands:
        (n, x, y, rot) = island['place']
        island_min = island['min']
        island_max = island['max']
        island_size = island['size']
        du = x/tex_w+ 3/tex_w + n * 1.0
        dv = y/tex_h+ 3/tex_h
        for face in island['faces']:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                u0 = (uv[0]-island_min[0]) * scale_u
                v0 = uv[1]-island_min[1]
                if rot == 0: # Original position
                    u = u0 + du
                    v = v0 + dv
                elif rot == 1: # 90 rotation
                    u = v0 + du
                    v = u0 + dv
                elif rot == 2: # Flipped on X
                    u = island_size[0]*scale_u - u0 + du
                    v = v0 + dv
                elif rot == 3: # 90 rotation, Flipped on x
                    u = island_size[1] - v0 + du
                    v = u0 + dv
                loop[uv_layer].uv = (u, v)
    bm.to_mesh(obj.data)
    bm.free()
    
    # Save a debug view of the texture
    for i, target in enumerate(targets):
        filled = 0
        tex = [1.0 for v in range(tex_w*tex_h*4)]
        for x in range(tex_w):
            for span in target[x]:
                filled += span[2]
                for y in range(span[0], span[1] + 1):
                    tex[4*(x + y*tex_w) + 3] = 0.0
        pack_image = bpy.data.images.new(f'Nest {i}', tex_w, tex_h, alpha=True)
        pack_image.pixels = tex
        pack_image.filepath_raw = bpy.path.abspath(f'//Nest {i}.png')
        pack_image.file_format = 'PNG'
        pack_image.save()
        bpy.data.images.remove(pack_image)
        print(f'. Texture #{i} has a fill rate of {1.0 - (filled/(tex_w*tex_h))}')

    print("Packing done...")
