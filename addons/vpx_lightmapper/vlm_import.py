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
import struct
import bmesh
import os
import io
import importlib
import math
import mathutils
import zlib
from math import radians
from bpy_extras.io_utils import axis_conversion
from . import biff_io
from . import vlm_utils
from . import vlm_collections

# Dependencies which need a custom install (not included in the Blender install)
from PIL import Image
import olefile

global_scale = vlm_utils.global_scale
curve_resolution = 3

# TODO
# - Implement surface positionning relative to a ramp
# - Add support for loading embedded LZW encoded bmp files (very seldom, just one identified in the full example table)
# - Place drop target in movable or indirect bake group
# - Evaluate elements that can need an active material (z<0, transparent material / alpha texture)


class VPX_Material(object):
    def __init__(self):
        self.name = ''
        self.base_color = (1,1,1,1)
        self.glossy_color = (0,0,0,1)
        self.clearcoat_color = (0,0,0,1)
        self.wrap_lighting = 0.0
        self.is_metal = False
        self.roughness = 0.0
        self.glossy_image_lerp = 1.0
        self.edge = 0.0
        self.thickness = 0.0
        self.opacity = 1.0
        self.opacity_active = False
        self.edge_alpha = 1.0
    
    def apply(self, group):
        group.inputs[3].default_value = self.base_color
        group.inputs[4].default_value = self.wrap_lighting
        group.inputs[5].default_value = self.glossy_color
        group.inputs[6].default_value = self.glossy_image_lerp
        group.inputs[7].default_value = self.roughness
        group.inputs[8].default_value = self.clearcoat_color
        group.inputs[9].default_value = self.edge
        if self.opacity_active:
            group.inputs[10].default_value = self.opacity
            group.inputs[11].default_value = self.edge_alpha
            group.inputs[12].default_value = self.thickness
        else:
            group.inputs[10].default_value = 1.0
            group.inputs[11].default_value = 1.0
            group.inputs[12].default_value = 0.05
        if self.is_metal:
            group.inputs[13].default_value = 1
        else:
            group.inputs[13].default_value = 0


def update_material(mesh, slot, materials, mat_name, image, translucency=-1):
    # Find/Create material (see https://docs.blender.org/api/current/bpy.types.ShaderNode.html)
    mat_name = f"{mat_name.casefold()}"
    if image == "":
        name = f"VPX.Mat.{mat_name}"
    else:
        name = f"VPX.Mat.{mat_name}.{image.casefold()}"
    if name in bpy.data.materials:
        mat = bpy.data.materials[name]    
    else:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        nodes.clear()
        links = mat.node_tree.links
        group = nodes.new("ShaderNodeGroup")
        group.name = f"{mat_name}.Mat"
        group.width = 300
        group.node_tree = bpy.data.node_groups['VPX.Material']
        node_output = nodes.new(type='ShaderNodeOutputMaterial')   
        node_output.location.x = 400
        links.new(group.outputs[0], node_output.inputs[0])
        node_tex = nodes.new(type='ShaderNodeTexImage')
        node_tex.name = f"{mat_name}.Tex"
        node_tex.location.x = -400
        links.new(node_tex.outputs[0], group.inputs[0])
        links.new(node_tex.outputs[1], group.inputs[1])
        
    # create material slots and assign material if empty
    while len(mesh.materials) <= slot:
        mesh.materials.append(None)
    if mesh.materials[slot] is None:
        if mat_name == "vpx.core.mat.light.bulb":
            mesh.materials[slot] = bpy.data.materials["VPX.Core.Mat.Light.Bulb"]
            return
        if mat_name == "vpx.core.mat.light.socket":
            mesh.materials[slot] = bpy.data.materials["VPX.Core.Mat.Light.Socket"]
            return
        mesh.materials[slot] = mat

    # update VPX material
    mat = mesh.materials[slot]
    use_image = 0
    if f"{mat_name}.Tex" in mat.node_tree.nodes:
        node_tex = mat.node_tree.nodes[f"{mat_name}.Tex"]
        if not image.startswith("VPX.Core."):
            image = f"VPX.Tex.{image.casefold()}"
        if image in bpy.data.images:
            node_tex.image = bpy.data.images[image]
            use_image = 1
        else:
            if image != "VPX.Tex.":
                print(f"Missing texture {image}")
            node_tex.image = None
    if f"{mat_name}.Mat" in mat.node_tree.nodes:
        group = mat.node_tree.nodes[f"{mat_name}.Mat"]
        if mat_name in materials:
            materials[mat_name].apply(group)
        elif mat_name != "":
            print(f"Missing material {mat_name}")
        if translucency >= 0.0:
            if mat_name in materials and materials[mat_name].is_metal:
                group.inputs[14].default_value = 0
            else:
                group.inputs[14].default_value = translucency
        group.inputs[2].default_value = use_image


def update_object(context, obj_name, data, visible, bake_col, hidden_col):
    if obj_name in context.scene.objects:
        obj = context.scene.objects[obj_name]
        if obj.vlmSettings.import_mesh:
            if obj.type=='MESH' or obj.type=='CURVE':
                while len(data.materials) < len(obj.data.materials):
                    data.materials.append(None)
                for i in range(len(obj.data.materials), len(data.materials)):
                    data.materials.pop()
                for i, m in enumerate(obj.data.materials):
                    data.materials[i] = m
            obj.data = data
    else:
        obj = bpy.data.objects.new(obj_name, data)
        if visible:
            bake_col.objects.link(obj)
        else:
            hidden_col.objects.link(obj)
    return obj


def update_location(obj, x, y, z):
    if obj.vlmSettings.import_transform:
        obj.location = (x, y, z)


def add_core_mesh(obj_list, obj_name, core_mesh, visible, bake_col, hidden_col, materials, material, image, x, y, z, x_size, y_size, z_size, rot_z, global_scale):
    mesh = bpy.data.objects[core_mesh].data.copy()
    obj = update_object(bpy.context, obj_name, mesh, visible, bake_col, hidden_col)
    update_material(obj.data, 0, materials, material, image)
    if obj.vlmSettings.import_transform:
        obj.location = (global_scale * x, -global_scale * y, global_scale * z)
        obj.scale = (global_scale * x_size, global_scale * y_size, global_scale * z_size)
        obj.rotation_euler = mathutils.Euler((0.0, 0.0, -radians(rot_z)), 'XYZ')
    obj_list.append(obj)
    return obj
    

def load_point(item_data):
    sub_data = item_data.child_reader()
    x = y = z = tex_coord = 0
    smooth = False
    point_skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'SLNG')
    while not sub_data.is_eof():
        sub_data.next()
        if sub_data.tag == 'VCEN':
            x = sub_data.get_float()
            y = sub_data.get_float()
        elif sub_data.tag == 'POSZ':
            z = sub_data.get_float()
        elif sub_data.tag == 'SMTH':
            smooth = sub_data.get_bool()
        elif sub_data.tag == 'ATEX':
            auto_tex = sub_data.get_bool()
        elif sub_data.tag == 'TEXC':
            tex_coord = sub_data.get_float()
        elif sub_data.tag in point_skipped:
            sub_data.skip_tag()
    item_data.skip(sub_data.pos)
    return [x, y, z, smooth, auto_tex, tex_coord]


def create_curve(curve_name, points, cyclic, flat, global_scale):
    # Create the curve object
    curve = bpy.data.curves.new(curve_name, type='CURVE')
    curve.render_resolution_u = curve_resolution
    curve.resolution_u = curve_resolution
    polyline = curve.splines.new('BEZIER')
    polyline.bezier_points.add(len(points) - 1)
    polyline.use_cyclic_u = cyclic
    if flat:
        curve.dimensions = '2D'
        curve.fill_mode = 'BOTH'
        for i, p in enumerate(points):
            polyline.bezier_points[i].co = (p[0] * global_scale, -p[1] * global_scale, 0)
    else:
        curve.dimensions = '3D'
        curve.fill_mode = 'FULL'
        curve.twist_mode = 'Z_UP'
        curve.use_fill_caps = True
        for i, p in enumerate(points):
            polyline.bezier_points[i].co = (p[0] * global_scale, -p[1] * global_scale, p[2] * global_scale)
    for i, p in enumerate(points):
        if p[3]:
            polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'AUTO'
        else:
            polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'VECTOR'
    # Update the points by computing the right U for points flagged as automatic 'texture coordinates'
    length = 0
    seg_lengths = []
    for i, p in enumerate(points):
        q = points[(i + 1) % len(points)]
        seg_lengths.append(length)
        length += math.sqrt((p[0]-q[0])*(p[0]-q[0]) + (p[1]-q[1])*(p[1]-q[1]))
    xpos = [x/length for x in seg_lengths]
    xpos.append(1.0)
    n_points = len(points)
    last_fixed_index = 0
    last_fixed_u = 0
    for i, p in enumerate(points):
        if not p[4]:
            for j in range(last_fixed_index, i):
                if xpos[last_fixed_index] == xpos[i]:
                    points[j][5] = last_fixed_u
                else:
                    points[j][5] = last_fixed_u + (p[5] - points[last_fixed_index][5]) * (xpos[j] - xpos[last_fixed_index]) / (xpos[i] - xpos[last_fixed_index])
            last_fixed_index = i
            last_fixed_u = p[5]
    for j in range(last_fixed_index, n_points):
        if xpos[last_fixed_index] == 1.0:
            points[j][5] = last_fixed_u
        else:
            points[j][5] = last_fixed_u + (1.0 - points[last_fixed_index][5]) * (xpos[j] - xpos[last_fixed_index]) / (1.0 - xpos[last_fixed_index])
    return curve


def read_vpx(context, filepath):
    print("reading ", filepath)

    if not os.path.isfile(filepath):
        self.report({'WARNING'},f"{filepath} does not exist")
        return {'CANCELLED'}

    opt_light_size = context.scene.vlmSettings.light_size
    opt_light_intensity = context.scene.vlmSettings.light_intensity
    opt_insert_intensity = context.scene.vlmSettings.insert_intensity
    opt_process_inserts = context.scene.vlmSettings.process_inserts
    opt_process_plastics = context.scene.vlmSettings.process_plastics
    opt_use_pf_translucency_map = context.scene.vlmSettings.use_pf_translucency_map
    opt_plastic_translucency = 1.0
    opt_bevel_plastics = context.scene.vlmSettings.bevel_plastics
    opt_detect_insert_overlay = True # Place any flasher containing 'insert' in its name to the overlay collection
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    
    # Purge unlinked datas to avoid reusing them
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

    vlm_collections.setup_collections()
    col_initial_state = vlm_collections.push_state()
    root_col = vlm_collections.get_collection('ROOT')
    trash_col = vlm_collections.get_collection('TRASH')
    hidden_col = vlm_collections.get_collection('HIDDEN')
    indirect_col = vlm_collections.get_collection('INDIRECT')
    overlay_col = vlm_collections.get_collection('OVERLAY')
    gi_col = vlm_collections.get_collection('GI LIGHTS')
    lights_col = vlm_collections.get_collection('PLAYFIELD LIGHTS')
    bake_col = vlm_collections.get_collection('BAKE DEFAULT')
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, bake_col).exclude = False
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, hidden_col).exclude = False
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, indirect_col).exclude = False
    
    # Append core meshes (without linking them in order to dispose the unused ones after import)
    librarypath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VPXMeshes.blend")
    if not os.path.isfile(librarypath):
        self.report({'WARNING'},f"{librarypath} does not exist")
        return {'CANCELLED'}
    with bpy.data.libraries.load(librarypath, link=False) as (data_from, data_to):
        data_to.objects = [name for name in data_from.objects if name.startswith("VPX.Core.")]
        data_to.images = [name for name in data_from.images if name.startswith("VPX.Core.")]
        data_to.materials = [name for name in data_from.materials if name.startswith("VPX.Core.Mat.")]
        data_to.node_groups = data_from.node_groups
                
    with olefile.OleFileIO(filepath) as ole:
        version = biff_io.BIFF_reader(ole.openstream('GameStg/Version').read()).get_32()
        if version <= 30:
            return {"FAILED"}
        print(f"VPX file version: {version/100}")

        # Read the table informations
        game_data = biff_io.BIFF_reader(ole.openstream('GameStg/GameData').read())
        n_materials = 0
        env_image = ""
        n_items = 0
        n_sounds = 0
        n_images = 0
        n_collections = 0
        playfield_material = ""
        skipped = ()
        materials = {}
        ring_mat = VPX_Material()
        ring_mat.name = 'VPX.Mat.Ring'
        ring_mat.base_color = (1,1,1,1)
        ring_mat.glossy_color = (0,0,0,0)
        ring_mat.is_metal = True
        materials[ring_mat.name.casefold()] = ring_mat
        while not game_data.is_eof():
            game_data.next()
            if game_data.tag == 'MASI':
                n_materials = game_data.get_32()
            elif game_data.tag == 'MATE':
                for i in range(n_materials):
                    name = game_data.get_str(32).rstrip('\x00')
                    mat = VPX_Material()
                    mat.name = name
                    mat.base_color = game_data.get_color()
                    mat.glossy_color = game_data.get_color()
                    mat.clearcoat_color = game_data.get_color()
                    mat.wrap_lighting = game_data.get_float()
                    mat.is_metal = game_data.get_bool()
                    mat.roughness = game_data.get_float()
                    mat.glossy_image_lerp = game_data.get_u8() / 255.0
                    game_data.skip(3)
                    mat.edge = game_data.get_float()
                    mat.thickness = game_data.get_u8() / 255
                    game_data.skip(3)
                    mat.opacity = game_data.get_float()
                    opacity_active_edge_alpha = game_data.get_u8()
                    game_data.skip(3)
                    mat.opacity_active = (opacity_active_edge_alpha & 0x01) != 0
                    mat.edge_alpha = (opacity_active_edge_alpha & 0x7F) / 255.0
                    materials[name.casefold()] = mat
            elif game_data.tag == 'EIMG':
                env_image = game_data.get_string()
            elif game_data.tag == 'LEFT':
                playfield_left = game_data.get_float() * global_scale
            elif game_data.tag == 'TOPX':
                playfield_top = game_data.get_float() * global_scale
            elif game_data.tag == 'RGHT':
                playfield_right = game_data.get_float() * global_scale
            elif game_data.tag == 'BOTM':
                playfield_bottom = game_data.get_float() * global_scale
            elif game_data.tag == 'IMAG':
                playfield_image = game_data.get_string()
            elif game_data.tag == 'PLMA':
                playfield_material = game_data.get_string()
            elif game_data.tag == 'SEDT':
                n_items = game_data.get_u32()
            elif game_data.tag == 'SSND':
                n_sounds = game_data.get_u32()
            elif game_data.tag == 'SIMG':
                n_images = game_data.get_u32()
            elif game_data.tag == 'SCOL':
                n_collections = game_data.get_u32()
            elif game_data.tag == 'CODE':
                code_size = game_data.get_u32()
                game_data.skip(code_size)
            else:
                game_data.skip_tag()
        playfield_width = playfield_right - playfield_left
        playfield_height = playfield_bottom - playfield_top
        
        # Read the textures
        for index in range(n_images):
            image_data = biff_io.BIFF_reader(ole.openstream(f"GameStg/Image{index}").read())
            name = ""
            path = ""
            width = 0
            height = 0
            size = 0
            data = ""
            while not image_data.is_eof():
                image_data.next()
                if image_data.tag == 'NAME':
                    name = image_data.get_string()
                elif image_data.tag == 'PATH':
                    path = image_data.get_string()
                elif image_data.tag == 'WDTH':
                    width = image_data.get_u32()
                elif image_data.tag == 'HGHT':
                    height = image_data.get_u32()
                elif image_data.tag == 'ALTV':
                    image_data.skip_tag()
                elif image_data.tag == 'BITS':
                    print(f"GameStg/Image{index} {name}: Unsupported bmp image file")
                    #uncompressed = zlib.decompress(image_data.data[image_data.pos:]) #, wbits=9)
                    data = None
                    break
                elif image_data.tag == 'JPEG':
                    sub_data = image_data.child_reader()
                    while not sub_data.is_eof():
                        sub_data.next()
                        if sub_data.tag == 'SIZE':
                            size = sub_data.get_u32()
                        elif sub_data.tag == 'DATA':
                            data = sub_data.get(size)
                        elif sub_data.tag == 'NAME':
                            sub_data.skip_tag()
                        elif sub_data.tag == 'PATH':
                            path = sub_data.get_string()
                    image_data.skip(sub_data.pos)
            name = f"VPX.Tex.{name.casefold()}"
            if name in bpy.data.images:
                image = bpy.data.images[name]
                try:
                    image.unpack(method='REMOVE')
                except:
                    pass
            else:
                image = bpy.data.images.new(name, width, height, alpha=True)
            if data is not None:
                try:
                    im = Image.open(io.BytesIO(data))
                    if im.format == 'WEBP':
                        byte_io = io.BytesIO()
                        im.save(byte_io, 'PNG')
                        data = byte_io.getvalue()
                        size = len(data)
                except OSError:
                    print(f"cannot load {name} initially imported from {path}")
                image.pack(data=data, data_len=size)
            image.source = 'FILE'
            
        # Setup environment lighting
        env_image = f"VPX.Tex.{env_image.casefold()}" 
        if "VPX.Env.Black" not in bpy.data.worlds:
            mat = bpy.data.worlds.new("VPX.Env.Black")
            mat.use_nodes = True
            mat.use_fake_user = True
            nodes = mat.node_tree.nodes
            nodes.clear()
            if env_image in bpy.data.images:
                node_dif = nodes.new(type='ShaderNodeBackground')
                node_output = nodes.new(type='ShaderNodeOutputWorld')   
                node_output.location.x = 400
                links = mat.node_tree.links
                node_dif.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
                links.new(node_dif.outputs[0], node_output.inputs[0])
        if "VPX.Env.IBL" in bpy.data.worlds:
            mat = bpy.data.worlds["VPX.Env.IBL"]
        else:
            mat = bpy.data.worlds.new("VPX.Env.IBL")
            mat.use_nodes = True
            mat.use_fake_user = True
            nodes = mat.node_tree.nodes
            nodes.clear()
            if env_image in bpy.data.images:
                node_tex = nodes.new(type='ShaderNodeTexEnvironment')
                node_tex.name = 'VPX.Mat.Tex.IBL'
                node_dif = nodes.new(type='ShaderNodeBackground')
                node_output = nodes.new(type='ShaderNodeOutputWorld')   
                node_output.location.x = 400
                node_tex.location.x = -400
                links = mat.node_tree.links
                links.new(node_tex.outputs[0], node_dif.inputs[0])
                links.new(node_dif.outputs[0], node_output.inputs[0])
            context.scene.world = mat
        if 'VPX.Mat.Tex.IBL' in mat.node_tree.nodes:
            node_tex = mat.node_tree.nodes['VPX.Mat.Tex.IBL']
            if env_image in bpy.data.images:
                node_tex.image = bpy.data.images[env_image]
            else:
                node_tex.image = None
        
        # Read the game items
        created_objects = []
        surface_offsets = {}
        shifted_objects = []
        insert_cups = []
        for index in range(n_items):
            name = ""
            item_data = biff_io.BIFF_reader(ole.openstream(f"GameStg/GameItem{index}").read())
            item_type = item_data.get_32()

            if item_type == 0: # Surface (wall)
                top_material = ""
                side_material = ""
                top_image = ""
                side_image = ""
                top_visible = False
                side_visible = False
                height_bottom = 0.0
                height_top = 0.0
                points = []
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'PIID', 'HTEV', 'DROP', 'FLIP', 'ISBS', 'CLDW', 'TMON', 'TMIN', 'THRS', 'MAPH', 'SLMA', 'INNR', 'DSPT', 'SLGF', 'SLTH', 'ELAS', 'ELFO', 'WFCT', 'WSCT', 'OVPH', 'SLGA', 'DILI', 'DILB', 'REEN')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'TOMA':
                        top_material = item_data.get_string().casefold()
                    elif item_data.tag == 'SIMA':
                        side_material = item_data.get_string().casefold()
                    elif item_data.tag == 'IMAG':
                        top_image = item_data.get_string()
                    elif item_data.tag == 'SIMG':
                        side_image = item_data.get_string()
                    elif item_data.tag == 'VSBL':
                        top_visible = item_data.get_bool()
                    elif item_data.tag == 'SVBL':
                        side_visible = item_data.get_bool()
                    elif item_data.tag == 'HTBT':
                        height_bottom = item_data.get_float()
                    elif item_data.tag == 'HTTP':
                        height_top = item_data.get_float()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                surface_offsets[name] = height_top
                obj_name = f"VPX.Wall.{name}"
                extrude_height = global_scale * 0.5 * (height_top - height_bottom)
                curve = create_curve(f"VPX.Curve.{name}", points, True, True, global_scale)
                curve.extrude = extrude_height
                obj = bpy.data.objects.new(f"VPX.Temp", curve)
                bake_col.objects.link(obj)
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                bpy.ops.object.convert(target='MESH')
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.remove_doubles(threshold=global_scale*0.5)
                bpy.ops.mesh.dissolve_limited(angle_limit=radians(2.0), use_dissolve_boundaries=True)
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.shade_smooth()
                obj = context.view_layer.objects.active
                mesh = obj.data
                # Set bevelling on top and bottom edges
                mesh.use_customdata_edge_bevel = True
                for edge in mesh.edges:
                    if abs(mesh.vertices[edge.vertices[0]].co.z - mesh.vertices[edge.vertices[1]].co.z) < 0.01 * global_scale:
                         edge.bevel_weight = 1.0
                    else:
                         edge.bevel_weight = 0.0
                # Compute split normals, trying to get the right smoothing
                mesh.calc_normals_split()
                normals = [(0,0,0) for i in mesh.loops]
                epsilon = 0.00001 * global_scale
                uv_layer = mesh.uv_layers.active.data
                uv_pt1 = [100000, -1, -1]
                uv_pt2 = [100000, -1, -1]
                for poly in mesh.polygons:
                    for loop_index in poly.loop_indices:
                        loop = mesh.loops[loop_index]
                        pt = mesh.vertices[loop.vertex_index].co
                        n = loop.normal
                        if abs(poly.normal.z) > 0.5: # Top/Bottom sides
                            n = mathutils.Vector((0, 0, n[2])).normalized()
                        else: # Sides
                            # Identify 2 different points from the original curve and store there uv unwrapping
                            u,v = uv_layer[loop_index].uv # default u is a coordinate in the index coordinate system (point index / nb points)
                            if u != 0 and u != 1.0:
                                for i, p in enumerate(points):
                                    d = (p[0] * global_scale - pt.x)*(p[0] * global_scale - pt.x) + (-p[1] * global_scale - pt.y)*(-p[1] * global_scale - pt.y)
                                    if d < uv_pt1[0]:
                                        if i == uv_pt1[1]:
                                            uv_pt1[0] = d
                                            uv_pt1[2] = u
                                        elif i != uv_pt2[1] and u != uv_pt1[2] and u != uv_pt2[2]:
                                            if uv_pt1[0] < uv_pt2[0]:
                                                uv_pt2 = uv_pt1
                                            uv_pt1 = [d, i, u]
                                    if d < uv_pt2[0]:
                                        if i == uv_pt2[1]:
                                            uv_pt2[0] = d
                                            uv_pt2[2] = u
                                        elif i != uv_pt1[1] and u != uv_pt1[2] and u != uv_pt2[2]:
                                            if uv_pt2[0] < uv_pt1[0]:
                                                uv_pt1 = uv_pt2
                                            uv_pt2 = [d, i, u]
                            # Compute split normal for side
                            n = mathutils.Vector((n[0], n[1], 0)).normalized()
                            for i, p in enumerate(points): # Sharp edges
                                d = (p[0] * global_scale - pt.x)*(p[0] * global_scale - pt.x) + (-p[1] * global_scale - pt.y)*(-p[1] * global_scale - pt.y)
                                if d <= epsilon and not p[3]:
                                    scale = math.sqrt(1.0 - n[2]*n[2])
                                    n = mathutils.Vector((scale * poly.normal[0], scale * poly.normal[1], n[2])).normalized()
                                    break
                        normals[loop_index] = n
                mesh.use_auto_smooth = True
                mesh.normals_split_custom_set(normals)
                n_points = len(points)
                # For wall height of 0, uv_pt2[1] == uv_pt1[1], resulting in degenerate UV generation
                if uv_pt2[1] == uv_pt1[1]: 
                    u_winding = u0 = 1
                else:
                    if uv_pt1[1] > uv_pt2[1]:
                        tmp = uv_pt2
                        uv_pt2 = uv_pt1
                        uv_pt1 = tmp
                    # check if going forward from first to second points, match with increasing u accordingly, if not then we need to go backward
                    tu = (uv_pt1[2] + (uv_pt2[1] - uv_pt1[1]) / n_points) % 1.0
                    if abs(tu - uv_pt2[2]) < epsilon: 
                        u_winding = 1
                    else:
                        u_winding = -1
                    u0 = uv_pt1[2] - u_winding * uv_pt1[1] / n_points # u for first point
                def calc_u(points, pos):
                    p = (pos - u0) * n_points * u_winding
                    while p < 0:
                        p += n_points
                    i_a = int(p) % n_points
                    i_b = (i_a + 1) % n_points
                    rel = p - int(p)
                    return points[i_a][5] + rel * (points[i_b][5] - points[i_a][5])
                
                uv_layer = mesh.uv_layers.active.data
                for poly in mesh.polygons:
                    if abs(poly.normal.z) > 0.5: # Top/Bottom sides
                        if poly.normal.z > 0.5:
                            poly.material_index  = 0 # Top
                        else:
                            poly.material_index  = 2 # Bottom
                        for loop_index in poly.loop_indices:
                            pt = mesh.vertices[mesh.loops[loop_index].vertex_index]
                            uv_layer[loop_index].uv = ((pt.co.x - playfield_left) / playfield_width, (playfield_bottom + pt.co.y) / playfield_height)
                    else:
                        poly.material_index  = 1 # Side
                        for loop_index in poly.loop_indices:
                            u,v = uv_layer[loop_index].uv # default u is a coordinate in the index coordinate system (0 = first point,  u = point index / nb points, 1 = cyclicly first point)
                            uv_layer[loop_index].uv = (calc_u(points, u), v)

                bake_col.objects.unlink(obj)
                existing = obj_name in context.scene.objects
                obj = update_object(context, obj_name, obj.data, top_visible or side_visible, bake_col, hidden_col)
                update_location(obj, 0, 0, global_scale * 0.5 * (height_top + height_bottom))

                is_plastic = 2.5 < (height_top - height_bottom) < 3.5 # and 45 < height_bottom < 55

                bevel_size = min(extrude_height, global_scale * opt_bevel_plastics)
                if is_plastic and bevel_size > 0:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
                    if "Bevel" not in obj.modifiers:
                        bpy.ops.object.modifier_add(type='BEVEL')
                    obj.modifiers["Bevel"].offset_type = 'OFFSET'
                    obj.modifiers["Bevel"].width = bevel_size
                    obj.modifiers["Bevel"].segments = 5
                    obj.modifiers["Bevel"].limit_method = 'WEIGHT'

                created_objects.append(obj)
                obj.vlmSettings.vpx_object = name

                while len(mesh.materials) < 3:
                    mesh.materials.append(None)
                if opt_process_plastics and is_plastic:
                    obj.data.materials[0] = bpy.data.materials["VPX.Core.Mat.Plastic"]
                    obj.data.materials[1] = bpy.data.materials["VPX.Core.Mat.Plastic"]
                    update_material(obj.data, 2, materials, top_material, top_image, opt_plastic_translucency)
                else:
                    update_material(obj.data, 0, materials, top_material, top_image)
                    update_material(obj.data, 1, materials, side_material, side_image)
                    obj.data.materials[2] = bpy.data.materials["VPX.Core.Mat.Invisible"]
                if not top_visible:
                    obj.data.materials[0] = bpy.data.materials["VPX.Core.Mat.Invisible"]
                    obj.data.materials[2] = bpy.data.materials["VPX.Core.Mat.Invisible"]
                if not side_visible:
                    obj.data.materials[1] = bpy.data.materials["VPX.Core.Mat.Invisible"]
                             
            elif item_type == 1: # Flipper
                # FIXME add an option to create a cylinder in the indirect baking to get the base projected shadow
                pass
            
            elif item_type == 2: # Timer
                pass
            
            elif item_type == 3: # Plunger
                pass
            
            elif item_type == 4: # Text box
                pass
            
            elif item_type == 5: # Bumper
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'THRS', 'FORC', 'BSCT', 'RISP', 'RDLI', 'BVIS', 'HAHE', 'COLI', 'REEN')
                ring_material = ring_mat.name
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'RADI':
                        radius = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        cap_material = item_data.get_string()
                    elif item_data.tag == 'RIMA':
                        ring_material = item_data.get_string()
                        if ring_material == '':
                            ring_material = ring_mat.name
                    elif item_data.tag == 'BAMA':
                        base_material = item_data.get_string()
                    elif item_data.tag == 'SKMA':
                        skirt_material = item_data.get_string()
                    elif item_data.tag == 'HISC':
                        height_scale = item_data.get_float()
                    elif item_data.tag == 'ORIN':
                        orientation = item_data.get_float()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag == 'CAVI':
                        cap_visible = item_data.get_bool()
                    elif item_data.tag == 'BSVS':
                        base_visible = item_data.get_bool()
                        ring_visible = base_visible
                        skirt_visible = base_visible
                    elif item_data.tag == 'RIVS':
                        ring_visible = item_data.get_bool()
                    elif item_data.tag == 'SKVS':
                        skirt_visible = item_data.get_bool()
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj = add_core_mesh(created_objects, f"VPX.Bumper.Base.{name}", "VPX.Core.Bumperbase", base_visible, bake_col, hidden_col, materials, base_material, "", x, y, 0.0, radius, radius, height_scale, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                obj = add_core_mesh(created_objects, f"VPX.Bumper.Socket.{name}", "VPX.Core.Bumpersocket", skirt_visible, bake_col, hidden_col, materials, skirt_material, "", x, y, 0.0, radius, radius, height_scale, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                obj = add_core_mesh(created_objects, f"VPX.Bumper.Ring.{name}", "VPX.Core.Bumperring", ring_visible, indirect_col, hidden_col, materials, ring_material, "", x, y, 0.0, radius, radius, height_scale, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                obj = add_core_mesh(created_objects, f"VPX.Bumper.Cap.{name}", "VPX.Core.Bumpercap", cap_visible, bake_col, hidden_col, materials, cap_material, "", x, y, 0.0, radius, radius, height_scale, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
            
            elif item_type == 6: # Trigger
                points = []
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'REEN', 'ANSP', 'THOT', 'EBLD')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'SHAP':
                        shape = item_data.get_u32()
                    elif item_data.tag == 'RADI':
                        radius = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'SCAX':
                        scale_x = item_data.get_float()
                    elif item_data.tag == 'SCAY':
                        scale_y = item_data.get_float()
                    elif item_data.tag == 'WITI':
                        wire_thickness = item_data.get_float()
                    elif item_data.tag == 'ROTA':
                        orientation = item_data.get_float()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag == 'VSBL':
                        visible = item_data.get_bool()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                scale_z = 1.0
                if shape == 0:
                    continue
                elif shape == 2 or shape == 4:
                    scale_x = radius
                    scale_y = radius
                    scale_z = radius
                # TriggerNone, TriggerWireA, TriggerStar, TriggerWireB, TriggerButton, TriggerWireC, TriggerWireD, TriggerInder
                meshes = ["", "VPX.Core.Triggersimple", "VPX.Core.Triggerstar", "VPX.Core.Triggersimple", "VPX.Core.Triggerbutton", "VPX.Core.Triggersimple", "VPX.Core.Triggerwired", "VPX.Core.Triggerinder"]
                obj = add_core_mesh(created_objects, f"VPX.Trigger.{name}", meshes[shape], visible, indirect_col, hidden_col, materials, material, "", x, y, 0.0, scale_x, scale_y, scale_z, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                if shape == 1 or shape == 3 or shape == 5 or shape == 6 and wire_thickness > 0 and obj.vlmSettings.import_mesh:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
                    bpy.ops.object.mode_set(mode='EDIT')
                    bm = bmesh.from_edit_mesh(obj.data)
                    for v in bm.verts:
                        v.co += wire_thickness * v.normal
                    bmesh.update_edit_mesh(obj.data)
                    bpy.ops.object.mode_set(mode='OBJECT')
            
            elif item_type == 7: # Light
                x = y = halo_height = intensity = 0
                color = color2 = (0,0,0,0)
                bulb = False
                image = ''
                points = []
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'STAT', 'TMON', 'TMIN', 'SHAP', 'BPAT', 'BINT', 'TRMS', 'BGLS', 'LIDB', 'FASP', 'FASD', 'STBM', 'SHRB', 'BMVA')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'COLR':
                        color = item_data.get_color()
                    elif item_data.tag == 'COL2':
                        color2 = item_data.get_color()
                    elif item_data.tag == 'BWTH':
                        intensity = item_data.get_float()
                    elif item_data.tag == 'SHBM':
                        show_bulb = item_data.get_bool()
                    elif item_data.tag == 'BGLS':
                        is_backglass = item_data.get_bool()
                    elif item_data.tag == 'IMMO':
                        is_passthrough = item_data.get_bool()
                    elif item_data.tag == 'BMSC':
                        bulb_mesh_radius = item_data.get_float()
                    elif item_data.tag == 'BULT':
                        bulb = item_data.get_bool()
                    elif item_data.tag == 'BHHI':
                        halo_height = item_data.get_float()
                    elif item_data.tag == 'RADI':
                        falloff = item_data.get_float()
                    elif item_data.tag == 'FAPO':
                        falloff_power = item_data.get_float()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag == 'IMG1':
                        image = item_data.get_string()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                is_gi = name.casefold().startswith("gi")
                if is_gi:
                    default_light = gi_col
                else:
                    default_light = lights_col
                obj_name = f"VPX.Light.{name}"
                curve = create_curve(f"VPX.LightData.{name}", points, True, True, global_scale)
                obj = bpy.data.objects.new(f"VPX.Temp", curve)
                bake_col.objects.link(obj)
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                
                # Some tables expect the bulb halo to be cut by the light mesh (like a mask for insrets for example) but others use the mesh to create fake shadows...
                is_insert = not is_gi and (bulb or image == playfield_image or image == '') and (not bulb or halo_height == 0) and surface == ''
                if opt_process_inserts and is_insert:
                    if not bulb:
                        halo_height = 0
                    obj.data.fill_mode = 'BACK'
                    obj.data.extrude = max(opt_light_size + 1, 5) * global_scale
                    obj.data.transform(mathutils.Matrix.Translation((-x * global_scale, y * global_scale, 0.0)))
                    obj.data.materials.append(bpy.data.materials["VPX.Core.Mat.Inserts.Back"])
                    obj.vlmSettings.vpx_object = name
                    update_location(obj, x * global_scale, -y * global_scale, halo_height * global_scale - obj.data.extrude)
                    created_objects.append(obj)
                    indirect_col.objects.link(obj)
                    insert_cups.append(obj)
                    bake_col.objects.unlink(obj)
                    obj.name = f"VPX.Light.Shape.{name}"
                    mesh = bpy.data.lights.new(name=name, type='POINT')
                    mesh.color = (color[0], color[1], color[2])
                    mesh.energy = opt_insert_intensity * intensity * global_scale
                    mesh.shadow_soft_size = opt_light_size * global_scale
                    obj = update_object(context, obj_name, mesh, True, default_light, hidden_col)
                    # Move below playfield to light through the translucency of the playfield material
                    update_location(obj, x * global_scale, -y * global_scale, -(opt_light_size + 1) * global_scale)
                    obj.vlmSettings.vpx_object = name
                    created_objects.append(obj)
                elif bulb:
                    z = halo_height
                    bake_col.objects.unlink(obj)
                    obj.name = f"VPX.Light.Shape.{name}"
                    mesh = bpy.data.lights.new(name=name, type='POINT')
                    mesh.color = (color[0], color[1], color[2])
                    mesh.energy = opt_light_intensity * intensity * global_scale
                    mesh.shadow_soft_size = opt_light_size * global_scale
                    obj = update_object(context, obj_name, mesh, True, default_light, hidden_col)
                    obj.vlmSettings.vpx_object = name
                    update_location(obj, x * global_scale, -y * global_scale, z * global_scale)
                    created_objects.append(obj)
                else:
                    bpy.ops.object.convert(target='MESH')
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.mesh.remove_doubles(threshold = global_scale * 1)
                    bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.5))
                    bpy.ops.object.mode_set(mode='OBJECT')
                    bpy.ops.object.shade_flat()
                    obj = context.view_layer.objects.active
                    mesh = obj.data
                    mesh.use_auto_smooth = True
                    mesh.normals_split_custom_set([(0,0,1) for i in mesh.loops])
                    uv_layer = mesh.uv_layers.active.data
                    for poly in mesh.polygons:
                        for loop_index in poly.loop_indices:
                            pt = mesh.vertices[mesh.loops[loop_index].vertex_index]
                            uv_layer[loop_index].uv = ((pt.co.x - playfield_left) / playfield_width, (playfield_bottom + pt.co.y) / playfield_height)
                    bake_col.objects.unlink(obj)
                    obj = update_object(context, obj_name, mesh, True, default_light, hidden_col)
                    obj.vlmSettings.vpx_object = name
                    if obj.vlmSettings.import_transform:
                        z = 0.01 * global_scale # Slightly above playfield
                        if bulb:
                            z += halo_height * global_scale
                        obj.data.transform(mathutils.Matrix.Translation((-x * global_scale, y * global_scale, 0.0)))
                        obj.location = (x * global_scale, -y * global_scale, z)
                    created_objects.append(obj)
                    # Create/Update emitter material
                    mat_name = f"VPX.Emitter.{name}"
                    if name in bpy.data.materials:
                        mat = bpy.data.materials[name]    
                    else:
                        mat = bpy.data.materials.new(name)
                        mat.use_nodes = True
                        nodes = mat.node_tree.nodes
                        nodes.clear()
                        links = mat.node_tree.links
                        group = nodes.new("ShaderNodeGroup")
                        group.name = f"{mat_name}.Mat"
                        group.width = 300
                        group.node_tree = bpy.data.node_groups['VPX.Light']
                        node_output = nodes.new(type='ShaderNodeOutputMaterial')   
                        node_output.location.x = 400
                        links.new(group.outputs[0], node_output.inputs[0])
                        node_tex = nodes.new(type='ShaderNodeTexImage')
                        node_tex.name = f"{mat_name}.Tex"
                        node_tex.location.x = -400
                        links.new(node_tex.outputs[0], group.inputs[0])
                        links.new(node_tex.outputs[1], group.inputs[1])
                    while len(mesh.materials) <= 0:
                        mesh.materials.append(None)
                    if mesh.materials[0] is None:
                        mesh.materials[0] = mat
                    mat = mesh.materials[0]
                    use_image = 0
                    if f"{mat_name}.Tex" in mat.node_tree.nodes:
                        node_tex = mat.node_tree.nodes[f"{mat_name}.Tex"]
                        if not image.startswith("VPX.Core."):
                            image = f"VPX.Tex.{image.casefold()}"
                        if image in bpy.data.images:
                            node_tex.image = bpy.data.images[image]
                            use_image = 1
                        else:
                            if image != "VPX.Tex.":
                                print(f"Missing texture {image}")
                            node_tex.image = None
                    if f"{mat_name}.Mat" in mat.node_tree.nodes:
                        group = mat.node_tree.nodes[f"{mat_name}.Mat"]
                        group.inputs[2].default_value = use_image
                        group.inputs[3].default_value = color
                        group.inputs[4].default_value = color2
                        group.inputs[5].default_value = falloff * global_scale
                        group.inputs[6].default_value = max(0.1, falloff_power)
                        group.inputs[7].default_value = intensity
                shifted_objects.append((obj, surface))
                if show_bulb:
                    obj = add_core_mesh(created_objects, f"VPX.Light.Bulb.{name}", "VPX.Core.Bulblight", True, bake_col, hidden_col, materials, "VPX.Core.Mat.Light.Bulb", "", x, y, 0, bulb_mesh_radius, bulb_mesh_radius, bulb_mesh_radius, 0, global_scale)
                    shifted_objects.append((obj, surface))
                    obj = add_core_mesh(created_objects, f"VPX.Light.Socket.{name}", "VPX.Core.Bulbsocket", True, bake_col, hidden_col, materials, "VPX.Core.Mat.Light.Socket", "", x, y, 0, bulb_mesh_radius, bulb_mesh_radius, bulb_mesh_radius, 0, global_scale)
                    shifted_objects.append((obj, surface))
                
            elif item_type == 8: # Kicker
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'KSCT', 'KHAC', 'KHHI', 'EBLD', 'FATH', 'LEMO')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'RADI':
                        radius = item_data.get_float()
                    elif item_data.tag == 'KORI':
                        orientation = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'TYPE':
                        type = item_data.get_u32()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                if type != 0:
                    z = 0
                    if type == 1 or type == 3:
                        orientation = 0.0
                    elif type == 2:
                        z = -0.18
                    elif type == 4:
                        orientation += 90.0
                    meshes = ["", "VPX.Core.Kickerhole", "VPX.Core.Kickercup", "VPX.Core.Kickersimplehole", "VPX.Core.Kickerwilliams", "VPX.Core.Kickergottlieb", "VPX.Core.Kickert1"]
                    images = ["", "VPX.Core.kickerHoleWood", "VPX.Core.kickerCup", "VPX.Core.kickerHoleWood", "VPX.Core.kickerWilliams", "VPX.Core.kickerGottlieb", "VPX.Core.kickerT1"]
                    obj = add_core_mesh(created_objects, f"VPX.Kicker.{name}", meshes[type], True, bake_col, hidden_col, materials, material, images[type], x, y, z, radius, radius, radius, orientation, global_scale)
                    obj.vlmSettings.vpx_object = name
                    shifted_objects.append((obj, surface))
                
            elif item_type == 9: # Decal
                pass
            
            elif item_type == 10: # Gate
                type = 1
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'GGFC', 'AFRC', 'GFRC', 'GAMI', 'GAMA', 'ELAS', 'TWWA', 'GCOL', 'REEN')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'GATY':
                        type = item_data.get_u32()
                    elif item_data.tag == 'LGTH':
                        length = item_data.get_float()
                    elif item_data.tag == 'HGTH':
                        height = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'ROTA':
                        orientation = item_data.get_float()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag == 'GSUP':
                        show_bracket = item_data.get_bool()
                    elif item_data.tag == 'GVSB':
                        visible = item_data.get_bool()
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                meshes = ["", "VPX.Core.Gatewire", "VPX.Core.Gatewirerectangle", "VPX.Core.Gateplate", "VPX.Core.Gatelongplate"]
                obj = add_core_mesh(created_objects, f"VPX.Gate.Bracket.{name}", "VPX.Core.Gatebracket", visible and show_bracket, bake_col, hidden_col, materials, material, "", x, y, height, length, length, length, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                obj = add_core_mesh(created_objects, f"VPX.Gate.Wire.{name}", meshes[type], visible, indirect_col, hidden_col, materials, material, "", x, y, height, length, length, length, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
            
            elif item_type == 11: # Spinner
                type = 1
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'AFRC', 'SVIS', 'SELA', 'SMIN', 'SMAX', 'AFRC', 'REEN')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VCEN':
                        x = item_data.get_float()
                        y = item_data.get_float()
                    elif item_data.tag == 'LGTH':
                        length = item_data.get_float()
                    elif item_data.tag == 'HIGH':
                        height = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'IMGF':
                        image = item_data.get_string()
                    elif item_data.tag == 'ROTA':
                        orientation = item_data.get_float()
                    elif item_data.tag == 'SURF':
                        surface = item_data.get_string()
                    elif item_data.tag == 'SSUP':
                        show_bracket = item_data.get_bool()
                    elif item_data.tag == 'SVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj = add_core_mesh(created_objects, f"VPX.Spinner.Bracket.{name}", "VPX.Core.Spinnerbracket", visible and show_bracket, bake_col, hidden_col, materials, material, "", x, y, height, length, length, length, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
                obj = add_core_mesh(created_objects, f"VPX.Spinner.Wire.{name}", "VPX.Core.Spinnerplate", visible, indirect_col, hidden_col, materials, material, image, x, y, height, length, length, length, orientation, global_scale)
                obj.vlmSettings.vpx_object = name
                shifted_objects.append((obj, surface))
            
            elif item_type == 12: # Ramp
                material = ""
                image = ""
                visible = False
                height_bottom = 0.0
                height_top = 0.0
                width_bottom = 0.0
                width_top = 0.0
                wire_diameter = 0.0
                wire_distance_x = 0.0
                wire_distance_y = 0.0
                right_wall_height = 0.0
                left_wall_height = 0.0
                image_on_walls = False
                ramp_type = 0
                image_alignment = 0
                points = []
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'OVPH', 'MAPH', 'REEN', 'RADB', 'CLDR', 'RSCT', 'RFCT', 'ELAS', 'THRS', 'HTEV', 'WLHR', 'WLHL', 'TMIN', 'TMON')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'HTBT':
                        height_bottom = item_data.get_float()
                    elif item_data.tag == 'HTTP':
                        height_top = item_data.get_float()
                    elif item_data.tag == 'WDBT':
                        width_bottom = item_data.get_float()
                    elif item_data.tag == 'WDTP':
                        width_top = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'IMAG':
                        image = item_data.get_string()
                    elif item_data.tag == 'IMGW':
                        image_on_walls = item_data.get_bool()
                    elif item_data.tag == 'ALGN':
                        image_alignment = item_data.get_u32()
                    elif item_data.tag == 'TYPE':
                        ramp_type = item_data.get_u32()
                    elif item_data.tag == 'RVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag == 'RADI':
                        wire_diameter = item_data.get_float()
                    elif item_data.tag == 'RADX':
                        wire_distance_x = item_data.get_float()
                    elif item_data.tag == 'RADY':
                        wire_distance_y = item_data.get_float()
                    elif item_data.tag == 'WVHR':
                        right_wall_height = item_data.get_float()
                    elif item_data.tag == 'WVHL':
                        left_wall_height = item_data.get_float()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                z_axis = mathutils.Vector((0,0,1))
                obj_name = f"VPX.Ramp.{name}"
                curve_name = f"VPX.Curve.{name}"
                if ramp_type == 0:
                    # Flat ramp, with texture coordinates, RampTypeFlat = 0
                    curve = create_curve(f"VPX.Curve.{name}", points, False, False, global_scale)
                    bzp = curve.splines[0].bezier_points
                    ratios = []
                    length = 0
                    ratios.append(length)
                    for i in range(len(bzp) - 1):
                        length += (bzp[i].co-bzp[i+1].co).length
                        ratios.append(length)
                    for i, p in enumerate(points):
                        bzp[i].co.z = (p[2] + height_bottom + (height_top - height_bottom) * ratios[i] / length) * global_scale
                    object = bpy.data.objects.new("VPX.Temp", curve)
                    bake_col.objects.link(object)
                    bpy.ops.object.select_all(action='DESELECT')
                    object.select_set(True)
                    context.view_layer.objects.active = object
                    bpy.ops.object.convert(target='MESH')
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.mesh.remove_doubles(threshold = global_scale * 1)
                    bpy.ops.mesh.dissolve_limited(angle_limit = radians(0.5))
                    bpy.ops.object.mode_set(mode='OBJECT')
                    n_verts = len(object.data.vertices)
                    normals = []
                    length = 0
                    ratios.clear()
                    ratios.append(length)
                    for i in range(n_verts):
                        if i < n_verts - 1:
                            length += (object.data.vertices[i].co-object.data.vertices[i+1].co).length
                            ratios.append(length)
                        v = object.data.vertices[i].co
                        if i == 0:
                            n = mathutils.Vector(object.data.vertices[i + 1].co - v).cross(z_axis)
                        elif i == n_verts-1:
                            n = mathutils.Vector(v - object.data.vertices[i - 1].co).cross(z_axis)
                        else:
                            n = (mathutils.Vector(v - object.data.vertices[i - 1].co) + mathutils.Vector(object.data.vertices[i + 1].co - v)).cross(z_axis)
                        n.normalize()
                        normals.append(n)
                    verts = []
                    faces = []
                    # Plastic ramps need to have some thickness for transparent material to render correctly, so we create both sides, slightly separated
                    for i in range(n_verts):
                        w = global_scale * (0.5 * (width_bottom + (width_top - width_bottom) * ratios[i] / length) - 0.5)
                        verts.append((object.data.vertices[i].co.x-w*normals[i].x, object.data.vertices[i].co.y-w*normals[i].y, object.data.vertices[i].co.z-w*normals[i].z))
                        verts.append((object.data.vertices[i].co.x+w*normals[i].x, object.data.vertices[i].co.y+w*normals[i].y, object.data.vertices[i].co.z+w*normals[i].z))
                        verts.append((object.data.vertices[i].co.x-w*normals[i].x, object.data.vertices[i].co.y-w*normals[i].y, object.data.vertices[i].co.z-w*normals[i].z + right_wall_height * global_scale))
                        verts.append((object.data.vertices[i].co.x+w*normals[i].x, object.data.vertices[i].co.y+w*normals[i].y, object.data.vertices[i].co.z+w*normals[i].z + left_wall_height * global_scale))
                        if i > 0:
                            faces.append((i*4-4 + 0, i*4-4 + 1, i*4 + 1, i*4 + 0))
                            if left_wall_height != 0:
                                faces.append((i*4 + 0, i*4 + 2, i*4-4 + 2, i*4-4 + 0)) # Normal pointing inside
                                #faces.append((i*4-4 + 0, i*4-4 + 2, i*4 + 2, i*4 + 0)) # Normal pointing outside
                            if right_wall_height != 0:
                                faces.append((i*4-4 + 1, i*4-4 + 3, i*4 + 3, i*4 + 1)) # Normal pointing inside
                                #faces.append((i*4 + 1, i*4 + 3, i*4-4 + 3, i*4-4 + 1)) # Normal pointing outside
                    dec = n_verts * 4
                    for i in range(n_verts):
                        w = global_scale * (0.5 * (width_bottom + (width_top - width_bottom) * ratios[i] / length) + 0.5)
                        verts.append((object.data.vertices[i].co.x-w*normals[i].x, object.data.vertices[i].co.y-w*normals[i].y, object.data.vertices[i].co.z-w*normals[i].z))
                        verts.append((object.data.vertices[i].co.x+w*normals[i].x, object.data.vertices[i].co.y+w*normals[i].y, object.data.vertices[i].co.z+w*normals[i].z))
                        verts.append((object.data.vertices[i].co.x-w*normals[i].x, object.data.vertices[i].co.y-w*normals[i].y, object.data.vertices[i].co.z-w*normals[i].z + right_wall_height * global_scale))
                        verts.append((object.data.vertices[i].co.x+w*normals[i].x, object.data.vertices[i].co.y+w*normals[i].y, object.data.vertices[i].co.z+w*normals[i].z + left_wall_height * global_scale))
                        if i > 0:
                            #faces.append((i*4-4 + 0, i*4-4 + 1, i*4 + 1, i*4 + 0))
                            if left_wall_height != 0:
                                #faces.append((i*4 + 0, i*4 + 2, i*4-4 + 2, i*4-4 + 0)) # Normal pointing inside
                                faces.append((dec+i*4-4 + 0, dec+i*4-4 + 2, dec+i*4 + 2, dec+i*4 + 0)) # Normal pointing outside
                            if right_wall_height != 0:
                                #faces.append((i*4-4 + 1, i*4-4 + 3, i*4 + 3, i*4 + 1)) # Normal pointing inside
                                faces.append((dec+i*4 + 1, dec+i*4 + 3, dec+i*4-4 + 3, dec+i*4-4 + 1)) # Normal pointing outside
                    mesh = bpy.data.meshes.new(f"VPX.RMesh.{name}")
                    mesh.from_pydata(verts, [], faces)
                    uv_layer = mesh.uv_layers.new().data
                    for poly in mesh.polygons:
                        for loop_index in poly.loop_indices:
                            idx = mesh.loops[loop_index].vertex_index
                            if idx >= dec:
                                idx -= dec
                            if image_alignment == 0:
                                pt = mesh.vertices[idx]
                                uv_layer[loop_index].uv = ((pt.co.x - playfield_left) / playfield_width, (playfield_bottom + pt.co.y) / playfield_height)
                            else:
                                uv_layer[loop_index].uv = (idx & 1, ratios[idx >> 2] / length)
                    tmp_obj = obj = bpy.data.objects.new('VLM.Tmp', mesh)
                    bake_col.objects.link(tmp_obj)
                    bpy.ops.object.select_all(action='DESELECT')
                    tmp_obj.select_set(True)
                    context.view_layer.objects.active = tmp_obj
                    bpy.ops.object.shade_smooth()
                    bpy.ops.object.modifier_add(type='EDGE_SPLIT')
                    bpy.ops.object.modifier_apply(modifier="EdgeSplit")
                    bake_col.objects.unlink(tmp_obj)
                    vlm_utils.apply_split_normals(mesh)
                    object.data = mesh
                    bake_col.objects.unlink(object)
                    obj = update_object(context, obj_name, object.data, visible, bake_col, hidden_col)
                else:
                    # Wire ramp (no texture coordinate)
                    # RampType4Wire = 1, RampType2Wire = 2, RampType3WireLeft = 3, RampType3WireRight = 4, RampType1Wire = 5
                    curve = bpy.data.curves.new(curve_name, type='CURVE')
                    curve.dimensions = '3D'
                    curve.render_resolution_u = curve_resolution
                    curve.resolution_u = curve_resolution
                    curve.fill_mode = 'FULL'
                    curve.use_fill_caps = True
                    curve.bevel_depth = wire_diameter * 0.5 * global_scale
                    pos = [[(-wire_distance_x/2, 0), (wire_distance_x/2, 0), (-wire_distance_x/2, wire_distance_y), (wire_distance_x/2, wire_distance_y)], [(-wire_distance_x/2, 0), (wire_distance_x/2, 0)], [(-wire_distance_x/2, 0), (wire_distance_x/2, 0), (-wire_distance_x/2, wire_distance_y)], [(-wire_distance_x/2, 0), (wire_distance_x/2, 0), (wire_distance_x/2, wire_distance_y)], [(0, 0)]]
                    for w in pos[ramp_type - 1]:
                        polyline = curve.splines.new('BEZIER')
                        polyline.bezier_points.add(len(points) - 1)
                        for i, p in enumerate(points):
                            polyline.bezier_points[i].co = (p[0] * global_scale, -p[1] * global_scale, 0.0)
                        bzp = polyline.bezier_points
                        ratios = []
                        normals = []
                        length = 0
                        ratios.clear()
                        ratios.append(length)
                        n_verts = len(bzp)
                        for i in range(n_verts):
                            if i < n_verts - 1:
                                length += (bzp[i].co-bzp[i+1].co).length
                                ratios.append(length)
                            v = bzp[i].co
                            if i == 0:
                                n = mathutils.Vector(bzp[i + 1].co - v).cross(z_axis)
                            elif i == n_verts-1:
                                n = mathutils.Vector(v - bzp[i - 1].co).cross(z_axis)
                            else:
                                n = (mathutils.Vector(v - bzp[i - 1].co) + mathutils.Vector(bzp[i + 1].co - v)).cross(z_axis)
                            n.normalize()
                            normals.append(n)
                        for i, p in enumerate(points):
                            dx = w[0] * global_scale * normals[i].x
                            dy = w[0] * global_scale * normals[i].y
                            polyline.bezier_points[i].co.x += dx
                            polyline.bezier_points[i].co.y += dy
                            polyline.bezier_points[i].co.z = (p[2] + w[1] + height_bottom + (height_top - height_bottom) * ratios[i] / length) * global_scale
                            if p[3]:
                                polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'AUTO'
                            else:
                                polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'VECTOR'
                    obj = update_object(context, obj_name, curve, visible, bake_col, hidden_col)
                obj.vlmSettings.vpx_object = name
                update_location(obj, 0, 0, 0)
                update_material(obj.data, 0, materials, material, image)
                created_objects.append(obj)
            
            elif item_type == 13: # Table
                pass
            
            elif item_type == 14: # Light Center
                pass
            
            elif item_type == 15: # Drag Point
                pass
            
            elif item_type == 16: # Collection
                pass
            
            elif item_type == 17: # Reel
                pass
            
            elif item_type == 18: # Light sequencer
                pass
            
            elif item_type == 19: # Primitive
                n_vertices = 0
                n_indices = 0
                material = ""
                image = ""
                compressed_indices_size = 0
                compressed_vertices_size = 0
                vertices = []
                normals = []
                faces = []
                uvs = []
                visible = True
                rot_tra = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                position = (0.0, 0.0, 0.0)
                size = (1.0, 1.0, 1.0)
                n_sides = 8
                skipped = ('PIID', 'LOCK', 'LAYR', 'LANR', 'LVIS', 'FALP', 'ADDB', 'PIDB', 'M3DN', 'OSNM', 'DIPT', 'OVPH', 'MAPH', 'EBFC', 'NRMA', 'SCOL', 'TVIS', 'DTXI', 'HTEV', 'THRS', 'ELAS', 'ELFO', 'RFCT', 'RSCT', 'EFUI', 'CORF', 'CLDR', 'ISTO', 'STRE', 'DILI', 'DILB', 'REEN')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'IMAG':
                        image = item_data.get_string()
                    elif item_data.tag == 'TVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag == 'VPOS':
                        position = (item_data.get_float(), item_data.get_float(), item_data.get_float())
                        item_data.skip(4)
                    elif item_data.tag == 'VSIZ':
                        size = (item_data.get_float(), item_data.get_float(), item_data.get_float())
                        item_data.skip(4)
                    elif item_data.tag == 'U3DM':
                        use_3d_mesh = item_data.get_bool()
                    elif item_data.tag == 'SIDS':
                        n_sides = item_data.get_u32()
                    elif item_data.tag == 'RTV0':
                        rot_tra[0] = item_data.get_float()
                    elif item_data.tag == 'RTV1':
                        rot_tra[1] = item_data.get_float()
                    elif item_data.tag == 'RTV2':
                        rot_tra[2] = item_data.get_float()
                    elif item_data.tag == 'RTV3':
                        rot_tra[3] = item_data.get_float()
                    elif item_data.tag == 'RTV4':
                        rot_tra[4] = item_data.get_float()
                    elif item_data.tag == 'RTV5':
                        rot_tra[5] = item_data.get_float()
                    elif item_data.tag == 'RTV6':
                        rot_tra[6] = item_data.get_float()
                    elif item_data.tag == 'RTV7':
                        rot_tra[7] = item_data.get_float()
                    elif item_data.tag == 'RTV8':
                        rot_tra[8] = item_data.get_float()
                    elif item_data.tag == 'M3VN':
                        n_vertices = item_data.get_u32()
                    elif item_data.tag == 'M3CJ':
                        compressed_indices_size = item_data.get_u32()
                    elif item_data.tag == 'M3CI':
                        uncompressed = zlib.decompress(item_data.get(compressed_indices_size))
                        p = 0
                        if n_vertices > 65535:
                            while p < len(uncompressed):
                                faces.append(struct.unpack("<3I", uncompressed[p:p + 3*4]))
                                p = p + 3 * 4
                        else:
                            while p < len(uncompressed):
                                faces.append(struct.unpack("<3H", uncompressed[p:p + 3*2]))
                                p = p + 3 * 2
                    elif item_data.tag == 'M3DI':
                        if n_vertices > 65535:
                            for i in range(int(n_indices / 3)):
                                faces.append((item_data.get_u32(), item_data.get_u32(), item_data.get_u32()))
                        else:
                            for i in range(int(n_indices / 3)):
                                faces.append((item_data.get_u16(), item_data.get_u16(), item_data.get_u16()))
                    elif item_data.tag == 'M3FN':
                        n_indices = item_data.get_u32()
                    elif item_data.tag == 'M3CY':
                        compressed_vertices_size = item_data.get_u32()
                    elif item_data.tag == "M3CX":
                        uncompressed = zlib.decompress(item_data.get(compressed_vertices_size))
                        p = 0
                        while p < len(uncompressed):
                            vertex = struct.unpack("<3f", uncompressed[p:p + 3*4])
                            vertex = (-vertex[0], -vertex[1], -vertex[2])
                            vertices.append(vertex)
                            p = p + 4 * 3
                            normal = struct.unpack("<3f", uncompressed[p:p + 3*4])
                            normal = (-normal[0], -normal[1], -normal[2])
                            normals.append(normal)
                            p = p + 4 * 3
                            uv = struct.unpack("<2f", uncompressed[p:p + 2*4])
                            uvs.append((uv[0], 1.0 - uv[1]))
                            p = p + 4 * 2
                    elif item_data.tag == "M3DX":
                        d = struct.unpack(f'<{n_vertices * 8}f', item_data.get(n_vertices * 8 * 4))
                        for i in range(n_vertices):
                            p = i * 8
                            vertices.append( (-d[p+0], -d[p+1], -d[p+2]) )
                            normals.append( (-d[p+3], -d[p+4], -d[p+5]) )
                            uvs.append( (d[p+6], 1.0 - d[p+7]) )
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj_name = f"VPX.Prim.{name}"
                mesh_name = f"VPX.Mesh.{name}"
                mesh = bpy.data.meshes.new(mesh_name)
                if use_3d_mesh:
                    mesh.from_pydata(vertices, [], faces)
                    mesh.flip_normals()
                    mesh.use_auto_smooth = True
                    mesh.normals_split_custom_set_from_vertices(normals)
                    uv_layer = mesh.uv_layers.new()
                    for i in range(len(mesh.loops)):
                        uv_layer.data[i].uv = uvs[mesh.loops[i].vertex_index]
                else:
                    bm = bmesh.new()
                    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=n_sides, radius1=0.5, radius2=0.5, depth=1, matrix=mathutils.Matrix(), calc_uvs=True)
                    bm.to_mesh(mesh)
                    bm.free()
                    for p in mesh.polygons:
                        p.use_smooth = True
                    mesh.use_auto_smooth = True
                    mesh.calc_normals()
                    vlm_utils.apply_split_normals(mesh)
                    uv_layer = mesh.uv_layers.new().data
                    for poly in mesh.polygons:
                        for loop_index in poly.loop_indices:
                            vi = mesh.loops[loop_index].vertex_index
                            pt = mesh.vertices[vi].co
                            if poly.normal.z > 0.5: # Top/Bottom sides
                                uv_layer[loop_index].uv = (0.25 + 0.5 * pt.x, 0.75 + 0.5 * pt.y)
                            elif poly.normal.z < -0.5: # Top/Bottom sides
                                uv_layer[loop_index].uv = (0.75 + 0.5 * pt.x, 0.75 + 0.5 * pt.y)
                            else:
                                uv_layer[loop_index].uv = ((vi >> 1) / (n_sides - 1), 0.5 * (0.5 - pt.z))
                    
                existing = obj_name in context.scene.objects
                obj = update_object(context, obj_name, mesh, visible, bake_col, hidden_col)
                if obj.vlmSettings.import_transform:
                    axis_matrix = mathutils.Matrix.Scale(-1, 4, (1,0,0)) @ axis_conversion('-Y', 'Z', 'Y', 'Z').to_4x4()
                    pos = mathutils.Vector(((position[0] + rot_tra[3])* global_scale, (position[1] + rot_tra[4])* global_scale, (position[2] + rot_tra[5])* global_scale))
                    scale = mathutils.Vector((-size[0] * global_scale, -size[1] * global_scale, -size[2] * global_scale))
                    eul1 = mathutils.Euler((radians(rot_tra[0]), radians(rot_tra[1]), radians(rot_tra[2])), 'ZYX')
                    eul2 = mathutils.Euler((radians(rot_tra[6]), radians(rot_tra[7]), radians(rot_tra[8])), 'ZYX')
                    obj.matrix_world = axis_matrix @ mathutils.Matrix.LocRotScale(pos, eul2, None) @ mathutils.Matrix.LocRotScale(None, eul1, scale)
                if obj_name == "VPX.Prim.playfield_mesh":
                    update_material(obj.data, 0, materials, playfield_material, playfield_image)
                    if not existing:
                        vlm_collections.get_collection('BAKE PLAYFIELD').objects.link(obj)
                else:
                    update_material(obj.data, 0, materials, material, image)
                obj.vlmSettings.vpx_object = name
                created_objects.append(obj)
                    
            elif item_type == 20: # Flasher
                height = 0
                x = 0
                y = 0
                material = ""
                image = ""
                visible = True
                points = []
                skipped = ('PIID', 'LOCK', 'LAYR', 'LANR', 'LVIS', 'TMON', 'TMIN', 'IDMD', 'DSPT', 'FLDB', 'FILT', 'FIAM')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'FHEI':
                        height = item_data.get_float()
                    elif item_data.tag == 'FLAX':
                        x = item_data.get_float()
                    elif item_data.tag == 'FLAY':
                        y = item_data.get_float()
                    elif item_data.tag == 'FROX':
                        rot_x = item_data.get_float()
                    elif item_data.tag == 'FROY':
                        rot_y = item_data.get_float()
                    elif item_data.tag == 'FROZ':
                        rot_z = item_data.get_float()
                    elif item_data.tag == 'COLR':
                        color = item_data.get_color()
                    elif item_data.tag == 'IMAG':
                        image_a = item_data.get_string()
                    elif item_data.tag == 'IMAB':
                        image_b = item_data.get_string()
                    elif item_data.tag == 'FALP':
                        alpha = item_data.get_32()
                    elif item_data.tag == 'MOVA':
                        modulate_vs_add = item_data.get_float()
                    elif item_data.tag == 'FVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag == 'ADDB':
                        additive_blend = item_data.get_bool()
                    elif item_data.tag == 'ALGN':
                        image_alignment = item_data.get_u32()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj_name = f"VPX.Flasher.{name}"
                mesh_name = f"VPX.Quad.{name}"
                mesh = bpy.data.meshes.new(mesh_name)
                minx = miny = 100000000
                maxx = maxy = -100000000
                for pt in points:
                    minx = min( pt[0] * global_scale, minx)
                    miny = min(-pt[1] * global_scale, miny)
                    maxx = max( pt[0] * global_scale, maxx)
                    maxy = max(-pt[1] * global_scale, maxy)
                half_x = 0.5 * (maxx + minx)
                half_y = 0.5 * (maxy + miny)
                verts = [(pt[0] * global_scale - half_x, -pt[1] * global_scale - half_y, 0.0) for pt in points]
                faces = [tuple([idx for idx in range(len(points))])]
                mesh.from_pydata(verts, [], faces)
                mesh.use_auto_smooth = True
                mesh.normals_split_custom_set([(0,0,1) for i in mesh.loops])
                uv_layer = mesh.uv_layers.new().data
                for poly in mesh.polygons:
                    for loop_index in poly.loop_indices:
                        pt = mesh.vertices[mesh.loops[loop_index].vertex_index]
                        if image_alignment == 0: # World
                            uv_layer[loop_index].uv = ((pt.co.x + half_x - playfield_left) / playfield_width, (playfield_bottom + pt.co.y + half_y) / playfield_height)
                        else: # Wrap
                            uv_layer[loop_index].uv = (0.5 + pt.co.x / (maxx - minx), 0.5 + pt.co.y / (maxy - miny))
                existing = obj_name in context.scene.objects
                is_insert_overlay = opt_detect_insert_overlay and 'insert' in name.casefold()
                obj = update_object(context, obj_name, mesh, is_insert_overlay, overlay_col, hidden_col)
                obj.vlmSettings.vpx_object = name
                if obj.vlmSettings.import_transform:
                    obj.rotation_euler = mathutils.Euler((-radians(rot_x), -radians(rot_y), -radians(rot_z)), 'ZYX')
                    #obj.location = (global_scale * x, -global_scale * y, global_scale * height)
                    obj.location = (half_x, half_y, global_scale * height)
                created_objects.append(obj)
                mat_name = f"VPX.Flasher.{name.casefold()}"
                image_a = f"VPX.Tex.{image_a.casefold()}"
                image_b = f"VPX.Tex.{image_b.casefold()}"
                # Create material if needed
                if mat_name in bpy.data.materials:
                    mat = bpy.data.materials[mat_name]
                else:
                    mat = bpy.data.materials.new(mat_name)
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    nodes.clear()
                    links = mat.node_tree.links
                    group = nodes.new("ShaderNodeGroup")
                    group.name = f"{mat_name}.Mat"
                    group.width = 300
                    group.node_tree = bpy.data.node_groups['VPX.Flasher']
                    node_output = nodes.new(type='ShaderNodeOutputMaterial')   
                    node_output.location.x = 400
                    links.new(group.outputs[0], node_output.inputs[0])
                    node_texA = nodes.new(type='ShaderNodeTexImage')
                    node_texA.name = f"{mat_name}.TexA"
                    node_texA.extension = 'CLIP'
                    node_texA.location.x = -400
                    links.new(node_texA.outputs[0], group.inputs[0])
                    links.new(node_texA.outputs[1], group.inputs[1])
                    node_texB = nodes.new(type='ShaderNodeTexImage')
                    node_texB.name = f"{mat_name}.TexB"
                    node_texB.extension = 'CLIP'
                    node_texB.location.x = -400
                    node_texB.location.y = -300
                    links.new(node_texB.outputs[0], group.inputs[3])
                    links.new(node_texB.outputs[1], group.inputs[4])
                # Create material slots and assign material if empty
                while len(mesh.materials) < 1:
                    mesh.materials.append(None)
                if mesh.materials[0] is None:
                    mesh.materials[0] = mat
                # update VPX material
                mat = mesh.materials[0]
                use_imageA = 0
                if f"{mat_name}.TexA" in mat.node_tree.nodes:
                    node_texA = mat.node_tree.nodes[f"{mat_name}.TexA"]
                    if image_a in bpy.data.images:
                        node_texA.image = bpy.data.images[image_a]
                        use_imageA = 1
                    else:
                        if image_a != "VPX.Tex.":
                            print(f"Missing texture {image_a}")
                        node_texA.image = None
                use_imageB = 0
                if f"{mat_name}.TexB" in mat.node_tree.nodes:
                    node_texB = mat.node_tree.nodes[f"{mat_name}.TexB"]
                    if image_b in bpy.data.images:
                        node_texB.image = bpy.data.images[image_b]
                        use_imageB = 1
                    else:
                        if image_b != "VPX.Tex.":
                            print(f"Missing texture {image_b}")
                        node_texB.image = None
                if f"{mat_name}.Mat" in mat.node_tree.nodes:
                    group = mat.node_tree.nodes[f"{mat_name}.Mat"]
                    group.inputs[2].default_value = use_imageA
                    group.inputs[5].default_value = use_imageB
                    group.inputs[6].default_value = 0 # filter type
                    group.inputs[7].default_value = 0 # filter amount
                    group.inputs[8].default_value = color
                    group.inputs[9].default_value = additive_blend
                    group.inputs[10].default_value = alpha
                    group.inputs[11].default_value = modulate_vs_add
                    if not existing and is_insert_overlay:
                        group.inputs[12].default_value = 1.0 # Insert overlays are diffuse shaded (not emissive)

            elif item_type == 21: # Rubber
                material = ""
                image = ""
                visible = False
                height = 0.0
                thickness = 0.0
                rotate_x = 0.0
                rotate_y = 0.0
                rotate_z = 0.0
                points = []
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'OVPH', 'MAPH', 'REEN', 'ESIE', 'ESTR', 'CLDR', 'RSCT', 'RFCT', 'ELAS', 'ELFO', 'TMIN', 'TMON', 'HTHI', 'HTEV')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'HTTP':
                        height = item_data.get_float()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'IMAG':
                        image = item_data.get_string()
                    elif item_data.tag == 'RVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag == 'WDTP':
                        thickness = item_data.get_u32()
                    elif item_data.tag == 'ROTX':
                        rotate_x = item_data.get_float()
                    elif item_data.tag == 'ROTY':
                        rotate_y = item_data.get_float()
                    elif item_data.tag == 'ROTZ':
                        rotate_z = item_data.get_float()
                    elif item_data.tag == 'DPNT':
                        points.append(load_point(item_data))
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj_name = f"VPX.Rubber.{name}"
                curve_name = f"VPX.Curve.{name}"
                curve = bpy.data.curves.new(curve_name, type='CURVE')
                curve.dimensions = '3D'
                curve.render_resolution_u = curve_resolution
                curve.resolution_u = curve_resolution
                curve.fill_mode = 'FULL'
                curve.bevel_depth = thickness * 0.5 * global_scale
                polyline = curve.splines.new('BEZIER')
                polyline.bezier_points.add(len(points) - 1)
                polyline.use_cyclic_u = True
                for i, p in enumerate(points):
                    polyline.bezier_points[i].co = (p[0] * global_scale, -p[1] * global_scale, p[2] * global_scale)
                bzp = polyline.bezier_points
                ratios = []
                length = 0
                ratios.append(length)
                for i in range(len(bzp) - 1):
                    length += (bzp[i].co-bzp[i+1].co).length
                    ratios.append(length)
                for i, p in enumerate(points):
                    if p[3]:
                        polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'AUTO'
                    else:
                        polyline.bezier_points[i].handle_right_type = polyline.bezier_points[i].handle_left_type = 'VECTOR'
                obj = update_object(context, obj_name, curve, visible, bake_col, hidden_col)
                obj.vlmSettings.vpx_object = name
                update_location(obj, 0, 0, global_scale * height)
                update_material(obj.data, 0, materials, material, image)
                created_objects.append(obj)
        
            elif item_type == 22: # Hit Target
                skipped = ('LOCK', 'LAYR', 'LANR', 'LVIS', 'SLNG', 'OVPH', 'MAPH', 'RADE', 'TMIN', 'TMON', 'DRSP', 'ISDR', 'PIDB', 'REEN', 'DILI', 'DILB', 'CLDR', 'ELAS', 'ELFO', 'RSCT', 'RFCT', 'THRS', 'LEMO', 'HTEV')
                while not item_data.is_eof():
                    item_data.next()
                    if item_data.tag == 'NAME':
                        name = item_data.get_wide_string()
                    elif item_data.tag == 'VPOS':
                        x = item_data.get_float()
                        y = item_data.get_float()
                        z = item_data.get_float()
                        item_data.skip(4)
                    elif item_data.tag == 'VSIZ':
                        x_size = item_data.get_float()
                        y_size = item_data.get_float()
                        z_size = item_data.get_float()
                        item_data.skip(4)
                    elif item_data.tag == 'ROTZ':
                        rot_z = item_data.get_float()
                    elif item_data.tag == 'IMAG':
                        image = item_data.get_string()
                    elif item_data.tag == 'MATR':
                        material = item_data.get_string()
                    elif item_data.tag == 'TRTY':
                        type = item_data.get_u32()
                    elif item_data.tag == 'TVIS':
                        visible = item_data.get_bool()
                    elif item_data.tag in skipped:
                        item_data.skip_tag()
                obj_name = f"VPX.Target.{name}"
                #DropTargetBeveled, DropTargetSimple, HitTargetRound, HitTargetRectangle, HitFatTargetRectangle, HitFatTargetSquare, DropTargetFlatSimple, HitFatTargetSlim, HitTargetSlim
                meshes = ["", "VPX.Core.Droptargett2", "VPX.Core.Droptargett3", 
                    "VPX.Core.Hittargetround", "VPX.Core.Hittargetrectangle", "VPX.Core.Hittargetfatrectangle",
                    "VPX.Core.Hittargetfatsquare", "VPX.Core.Droptargett4", "VPX.Core.Hittargett2slim", "VPX.Core.Hittargett1slim"]
                obj = add_core_mesh(created_objects, obj_name, meshes[type], visible, bake_col, hidden_col, materials, material, image, x, y, z, x_size, y_size, z_size, rot_z, global_scale)
                obj.vlmSettings.vpx_object = name

            else:
                print(f"GameStg/GameItem{index}: unsupported type #{item_type}")
    
    # Shift object that are positionned on a surface
    for obj, surface in shifted_objects:
        if surface in surface_offsets:
            obj.location.z += surface_offsets[surface] * global_scale
            
    # Create the playfield
    if "VPX.Prim.playfield_mesh" in bpy.data.objects:
        playfield_obj = bpy.data.objects["VPX.Prim.playfield_mesh"]
        [col.objects.unlink(obj) for col in playfield_obj.users_collection]
        pfmesh = playfield_obj.data
    else:
        vert = [(playfield_left, -playfield_bottom, 0.0), (playfield_right, -playfield_bottom, 0.0), (playfield_left, -playfield_top, 0.0), (playfield_right, -playfield_top, 0.0)]
        pfmesh = bpy.data.meshes.new("VPX.Mesh.Playfield")
        pfmesh.from_pydata(vert, [], [(0, 1, 3, 2)])
        pfmesh.use_auto_smooth = True
        pfmesh.normals_split_custom_set([(0,0,1) for i in pfmesh.loops])
        uv_layer = pfmesh.uv_layers.new()
    playfield_obj = update_object(context, "VPX.Playfield", pfmesh, True, vlm_collections.get_collection('BAKE PLAYFIELD'), None)
    update_material(pfmesh, 0, materials, playfield_material, playfield_image, 0)
    created_objects.append(playfield_obj)
    if pfmesh.materials[0].name.startswith('VPX.Mat.'):
        print('Creating playfield material')
        mat = pfmesh.materials[0].copy()
        mat.name = 'VPX.Playfield'
        pfmesh.materials[0] = mat
        node_tex = mat.node_tree.nodes.new(type='ShaderNodeTexImage')
        node_tex.name = 'TranslucencyMap'
        node_tex.location.x = -400
        node_tex.location.y = -400
        node_math = mat.node_tree.nodes.new(type='ShaderNodeMath')
        node_math.operation = 'MULTIPLY'
        node_math.location.x = 100
        node_math.location.y = -400
        group_name = f"{playfield_material.casefold()}.Mat"
        if group_name in mat.node_tree.nodes:
            mat.node_tree.links.new(node_tex.outputs[1], node_math.inputs[0])
            mat.node_tree.links.new(node_math.outputs[0], mat.node_tree.nodes[group_name].inputs[14])
        else:
            print(f"Missing group '{group_name}' in playfield material")

    # Move to trash all managed objects that were not processed
    created_object_names = [obj.name for obj in created_objects]
    for obj in [obj for obj in root_col.all_objects if obj.name.startswith('VPX.') and obj.name not in created_object_names]:
        [col.objects.unlink(obj) for col in obj.users_collection]
        trash_col.objects.link(obj)
        
    # Output warnings for split normals
    for obj in created_objects:
        if obj.type == 'MESH' and not obj.data.has_custom_normals:
            print(f". Warning '{obj.name}' does not have split normals. This will break normals on the final bake mesh.")

    # Create and move camera to keep it centered on the table (at least on x axis)
    if not context.scene.camera is None and context.scene.camera.type == 'CAMERA':
        camera_object = context.scene.camera
    else:
        camera_data = bpy.data.cameras.new(name='Camera')
        camera_object = bpy.data.objects.new('Camera', camera_data)
        camera_object.rotation_euler = mathutils.Euler((radians(25.0), 0.0, 0.0), 'XYZ')
        context.scene.collection.objects.link(camera_object)
        camera_object.location.y = -2.41824
        camera_object.location.z =  2.6735
    camera_object.location.x = 0.5 * (playfield_left + playfield_right)
    camera_object.lock_location[0] = True

    # Create a translucency map for the playfield (translucent for inserts, diffuse otherwise)
    if len(pfmesh.materials) > 0 and pfmesh.materials[0] is not None and 'TranslucencyMap' in pfmesh.materials[0].node_tree.nodes:
        translucency_image = bpy.data.images.new('PFTranslucency', int(opt_tex_size/2), opt_tex_size, alpha=True)
        translucency_image.source = 'GENERATED' # Defaults to a full translucent playfield
        translucency_image.generated_type = 'BLANK'
        translucency_image.generated_color = (0.0, 0.0, 0.0, 1.0)
        mat = pfmesh.materials[0]
        mat.node_tree.nodes["TranslucencyMap"].image = translucency_image
        if opt_use_pf_translucency_map: # Render the translucency map (which can be entirely empty if there is no inserts cups)
            print(f"Computing translucency map for the playfield inserts.")
            mat.node_tree.nodes.active = mat.node_tree.nodes["TranslucencyMap"]
            tmp_col = vlm_collections.get_collection('BAKETMP')
            cups_initial_collection = vlm_collections.move_all_to_col(insert_cups, tmp_col)
            vlm_collections.exclude_all(context, root_col)
            vlm_collections.exclude_all(context, tmp_col, False)
            # Force a wiexport update (I did not find any better way....)
            bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
            view_matrix = mathutils.Matrix.LocRotScale(mathutils.Vector((-1.0, 1.0, 0)), None, mathutils.Vector((2.0 / playfield_width, 2.0 / playfield_height, 0.1)))
            projection_matrix = mathutils.Matrix.OrthoProjection('XY', 4)
            vlm_utils.render_mask(context, int(opt_tex_size / 2), opt_tex_size, translucency_image.name, view_matrix, projection_matrix)
            vlm_collections.restore_all_col_links(cups_initial_collection)
            vlm_collections.delete_collection(tmp_col)

    # Purge unlinked datas
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

    vlm_collections.pop_state(col_initial_state)

    context.scene.vlmSettings.table_file = bpy.path.relpath(filepath)
    context.scene.vlmSettings.playfield_size = (playfield_left, playfield_top, playfield_width, playfield_height)
    print(f"\nImport finished.")
    return {"FINISHED"}
