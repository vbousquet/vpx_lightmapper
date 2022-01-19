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
import os
import zlib
import struct
from . import biff_io
from . import vlm_utils
from . import vlm_collections

# Dependencies which need a custom install (not included in the Blender install)
import olefile
import pythoncom
import win32crypt
import win32cryptcon
from win32com import storagecon


# TODO
# - Toggle visibility of baked elements
# - Update VBS script of the table for light synchronization
# - Name playfield bake as VLM.Playfield
# - Set table playfield image to the solid playfield bake (and material) ?
#
# - Try exporting bakes as HDR (or provide an evaluation of the benefit it would give)
# - Try computing bakemap histogram, select the right format depending on the intensity span (EXR / brightness adjusted PNG or WEBP)


# FIXME rewrite an export to obj operator
def export_obj(context):
    vlmProps = context.scene.vlmSettings
    result_col = vlm_collections.get_collection('BAKE RESULT')
    exportpath = vlm_utils.get_bakepath(context, type='EXPORT')
    vlm_utils.mkpath(exportpath)
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, result_col).exclude = False
    print(f"\nStarting export process for {len(result_col.all_objects)} bakes")
    
    for i, obj in enumerate(result_col.all_objects, start = 1):
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        # see https://docs.blender.org/api/current/bpy.ops.export_scene.html
        print(f". {i}/{len(result_col.all_objects)} Exporting bake mesh for '{obj.name}'")
        bpy.ops.export_scene.obj(
            filepath=os.path.join(bpy.path.abspath(exportpath), f"{obj.name}.obj"),
            use_selection=True, use_materials=False, use_triangles=True,
            global_scale=1000.0 / global_scale, axis_forward='-Y', axis_up='-Z')

    print(f"\nExport finished.")
    return {"FINISHED"}


def export_vpx(context):
    """Export bakes by updating the reference VPX file
    . Remove all 'VLM.' prefixed objects from the source file
    . Disable rendering for all baked objects
    . Add all packmaps as texture with 'VLM.' prefixed name
    . Add base materials with 'VLM.' prefixed name
    . Add all bakes as primitives with 'VLM.' prefixed name
    . Update the table script with the needed light/lightmap sync code
    """
    vlmProps = context.scene.vlmSettings
    result_col = vlm_collections.get_collection('BAKE RESULT')
    bakepath = vlm_utils.get_bakepath(context)
    vlm_utils.mkpath(f"{bakepath}Export/")
    input_path = bpy.path.abspath(vlmProps.table_file)
    if not os.path.isfile(input_path):
        self.report({'WARNING'},f"{input_path} does not exist")
        return {'CANCELLED'}
    
    output_path = bpy.path.abspath(f"//{os.path.splitext(bpy.path.basename(input_path))[0]} - VLM.vpx")
    print(f'\nExporting bake results to {bpy.path.basename(output_path)}')

    src_storage = olefile.OleFileIO(input_path)
    dst_storage = pythoncom.StgCreateStorageEx(output_path, storagecon.STGM_TRANSACTED | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, storagecon.STGFMT_DOCFILE, 0, pythoncom.IID_IStorage, None, None)
    dst_gamestg = dst_storage.CreateStorage("GameStg", storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
    dst_tableinfo = dst_storage.CreateStorage("TableInfo", storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)

    crypt_context = win32crypt.CryptAcquireContext(None, None, win32cryptcon.PROV_RSA_FULL, win32cryptcon.CRYPT_VERIFYCONTEXT | win32cryptcon.CRYPT_NEWKEYSET)
    data_hash = crypt_context.CryptCreateHash(win32cryptcon.CALG_MD2)
    data_hash.CryptHashData(b'Visual Pinball')
    def append_structure(src_path, mode, hashed):
        index = 0
        while src_storage.exists(f'{src_path}{index}'):
            file_structure.append((f'{src_path}{index}', mode, hashed))
            index = index + 1
  
    file_structure = [ # path, 0=unstructured bytes/1=BIFF, hashed ?
        ('GameStg/Version', 0, True),
        ('TableInfo/TableName', 0, True),
        ('TableInfo/AuthorName', 0, True),
        ('TableInfo/TableVersion', 0, True),
        ('TableInfo/ReleaseDate', 0, True),
        ('TableInfo/AuthorEmail', 0, True),
        ('TableInfo/AuthorWebSite', 0, True),
        ('TableInfo/TableBlurb', 0, True),
        ('TableInfo/TableDescription', 0, True),
        ('TableInfo/TableRules', 0, True),
        ('TableInfo/TableSaveDate', 0, False),
        ('TableInfo/TableSaveRev', 0, False),
        ('TableInfo/Screenshot', 1, True),
        ('GameStg/CustomInfoTags', 1, True), # custom info tags must be hashed just after this stream
        ('GameStg/GameData', 1, True),]
    #append_structure('GameStg/GameItem', 1, False),
    append_structure('GameStg/Sound', 1, False),
    #append_structure('GameStg/Image', 1, False),
    append_structure('GameStg/Font', 1, False),
    append_structure('GameStg/Collection', 1, True),

    # Remove previous packmaps and append the new ones
    n_images = 0
    while src_storage.exists(f'GameStg/Image{n_images}'):
        data = src_storage.openstream(f'GameStg/Image{n_images}').read()
        br = biff_io.BIFF_reader(data)
        name = 'unknown'
        while not br.is_eof():
            br.next()
            if br.tag == "NAME":
                name = br.get_string()
                break;
            br.skip_tag()
        if not name.startswith('VLM.'):
            dst_stream = dst_gamestg.CreateStream(f'Image{n_images}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
            dst_stream.Write(data)
            n_images += 1
    packmap_index = 0
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break
        packmap_path = bpy.path.abspath(f"{bakepath}Export/Packmap {packmap_index}.png")
        img_writer = biff_io.BIFF_writer()
        img_writer.write_tagged_string(b'NAME', f'VLM.Packmap{packmap_index}')
        img_writer.write_tagged_string(b'PATH', packmap_path)
        with open(packmap_path, 'rb') as f:
            img_data = f.read()
            img_writer.write_tagged_u32(b'SIZE', len(img_data))
            img_writer.write_tagged_data(b'DATA', img_data)
        img_writer.close()
        writer = biff_io.BIFF_writer()
        writer.write_tagged_string(b'NAME', f'VLM.Packmap{packmap_index}')
        writer.write_tagged_string(b'PATH', packmap_path)
        writer.write_tagged_u32(b'WDTH', objects[0].vlmSettings.bake_packmap_width)
        writer.write_tagged_u32(b'HGHT', objects[0].vlmSettings.bake_packmap_height)
        writer.write_tagged_empty(b'JPEG') # Strangely, raw data are pushed outside of the JPEG tag (breaking the BIFF structure of the file)
        writer.write_data(img_writer.get_data())
        writer.write_tagged_float(b'ALTV', 1.0)
        writer.close()
        dst_stream = dst_gamestg.CreateStream(f'Image{n_images}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(writer.get_data())
        print(f'. Exporting Packmap #{packmap_index} as a {objects[0].vlmSettings.bake_packmap_width:>4} x {objects[0].vlmSettings.bake_packmap_height:>4} image')
        packmap_index += 1
        n_images += 1

    # Remove previous bake models and append the new ones
    n_game_items = 0
    while src_storage.exists(f'GameStg/GameItem{n_game_items}'):
        data = src_storage.openstream(f'GameStg/GameItem{n_game_items}').read()
        br = biff_io.BIFF_reader(data)
        name = 'unknown'
        while not br.is_eof():
            br.next()
            # FIXME toggle visibility of baked meshes (remove them in export release mode ?)
            if br.tag == "NAME":
                name = br.get_string()
                break;
            br.skip_tag()
        if not name.startswith('VLM.'):
            dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
            dst_stream.Write(data)
            n_game_items += 1
    for obj in [obj for obj in result_col.all_objects]:
        is_light = obj.vlmSettings.bake_type == 'lightmap'
        writer = biff_io.BIFF_writer()
        writer.write_u32(19)
        writer.write_tagged_padded_vector(b'VPOS', 0, 0, 0)
        writer.write_tagged_padded_vector(b'VSIZ', 100, 100, 100)
        writer.write_tagged_float(b'RTV0', 0)
        writer.write_tagged_float(b'RTV1', 0)
        writer.write_tagged_float(b'RTV2', 0)
        writer.write_tagged_float(b'RTV3', 0)
        writer.write_tagged_float(b'RTV4', 0)
        writer.write_tagged_float(b'RTV5', 0)
        writer.write_tagged_float(b'RTV6', 0)
        writer.write_tagged_float(b'RTV7', 0)
        writer.write_tagged_float(b'RTV8', 0)
        writer.write_tagged_string(b'IMAG', f'VLM.Packmap{obj.vlmSettings.bake_packmap}')
        writer.write_tagged_string(b'NRMA', '')
        writer.write_tagged_u32(b'SIDS', 4)
        writer.write_tagged_wide_string(b'NAME', obj.name) # FIXME naming should be better
        if is_light:
            writer.write_tagged_string(b'MATR', 'VLM.Lightmap')
        else:
            writer.write_tagged_string(b'MATR', 'VLM.Bake.Solid') # FIXME we should have 2 variants (active/not active)
        writer.write_tagged_u32(b'SCOL', 0xFFFFFF)
        writer.write_tagged_bool(b'TVIS', True)
        writer.write_tagged_bool(b'DTXI', False)
        writer.write_tagged_bool(b'HTEV', False)
        writer.write_tagged_float(b'THRS', 2.0)
        writer.write_tagged_float(b'ELAS', 0.3)
        writer.write_tagged_float(b'ELFO', 0.0)
        writer.write_tagged_float(b'RFCT', 0.0)
        writer.write_tagged_float(b'RSCT', 0.0)
        writer.write_tagged_float(b'EFUI', 0.0)
        writer.write_tagged_float(b'CORF', 0.0)
        writer.write_tagged_bool(b'CLDR', False)
        writer.write_tagged_bool(b'ISTO', True)
        writer.write_tagged_bool(b'U3DM', True)
        writer.write_tagged_bool(b'STRE', False) # FIXME static rendering should be true for solid map without transparency
        writer.write_tagged_u32(b'DILI', 255) # 255 if 1.0 for disable lighting
        writer.write_tagged_float(b'DILB', 1.0) # also disable lighting from below
        writer.write_tagged_bool(b'REEN', False)
        writer.write_tagged_bool(b'EBFC', False)
        writer.write_tagged_string(b'MAPH', '')
        writer.write_tagged_bool(b'OVPH', False)
        writer.write_tagged_bool(b'DIPT', False)
        writer.write_tagged_bool(b'OSNM', False)
        writer.write_tagged_string(b'M3DN', f'VLM.{obj.name}')
        indices = []
        vertices = []
        vert_dict = {}
        n_vertices = 0
        uv_layer_packed = obj.data.uv_layers["UVMap Packed"]
        for poly in obj.data.polygons:
            if len(poly.loop_indices) != 3:
                continue
            for loop_index in reversed(poly.loop_indices):
                loop = obj.data.loops[loop_index]
                x, y, z = obj.data.vertices[loop.vertex_index].co
                nx, ny, nz = loop.normal
                u, v = uv_layer_packed.data[loop_index].uv
                vertex = (x, -y, z, nx, -ny, nz, u, 1.0 - v)
                existing_index = vert_dict.get(vertex, None)
                if existing_index is None:
                    vert_dict[vertex] = n_vertices
                    vertices.extend(vertex)
                    indices.append(n_vertices)
                    n_vertices += 1
                else:
                    indices.append(existing_index)
        n_indices = len(indices)
        print(f'. Exporting model with {n_vertices:>6} vertices for {int(n_indices/3):>5} faces for {obj.name}')
        
        writer.write_tagged_u32(b'M3VN', n_vertices)
        #writer.write_tagged_data(b'M3DX', struct.pack(f'<{len(vertices)}f', *vertices))
        compressed_vertices = zlib.compress(struct.pack(f'<{len(vertices)}f', *vertices))
        writer.write_tagged_u32(b'M3CY', len(compressed_vertices))
        writer.write_tagged_data(b'M3CX', compressed_vertices)
        
        writer.write_tagged_u32(b'M3FN', n_indices)
        if n_vertices > 65535:
            #writer.write_tagged_data(b'M3DI', struct.pack(f'<{n_indices}I', *indices))
            compressed_indices = zlib.compress(struct.pack(f'<{n_indices}I', *indices))
        else:
            #writer.write_tagged_data(b'M3DI', struct.pack(f'<{n_indices}H', *indices))
            compressed_indices = zlib.compress(struct.pack(f'<{n_indices}H', *indices))
        writer.write_tagged_u32(b'M3CJ', len(compressed_indices))
        writer.write_tagged_data(b'M3CI', compressed_indices)
        
        writer.write_tagged_float(b'PIDB', 0.0)
        writer.write_tagged_bool(b'ADDB', is_light) # Additive blending VPX mod
        writer.write_tagged_u32(b'FALP', 100) # Additive blending VPX mod
        writer.write_tagged_bool(b'LOCK', True)
        writer.write_tagged_bool(b'LVIS', True)
        writer.write_tagged_u32(b'LAYR', 0)
        writer.write_tagged_string(b'LANR', 'VLM.Visuals')
        writer.close()
        dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(writer.get_data())
        n_game_items += 1

    # Copy reference file
    for src_path, mode, hashed in file_structure:
        if not src_storage.exists(src_path):
            continue
        if src_path.startswith('GameStg'):
            dst_st = dst_gamestg
        else:
            dst_st = dst_tableinfo
        data = src_storage.openstream(src_path).read()
        if src_path == 'GameStg/GameData':
            data = bytearray(data)
            br = biff_io.BIFF_reader(data)
            has_solid_bake_mat = has_active_bake_mat = has_light_mat = False
            while not br.is_eof():
                br.next()
                if br.tag == "SIMG":
                    br.put_u32(n_images)
                elif br.tag == "SEDT":
                    br.put_u32(n_game_items)
                elif br.tag == "MASI":
                    masi_pos = br.pos
                    n_materials = br.get_u32()
                elif br.tag == "MATE":
                    mate_pos = br.pos
                    for i in range(n_materials):
                        name = br.get_str(32).rstrip('\x00')
                        if name == 'VLM.Bake.Solid':
                            has_solid_bake_mat = True
                        elif name == 'VLM.Bake.Active':
                            has_active_bake_mat = True
                        elif name == 'VLM.Light':
                            has_light_mat = True
                        br.skip(11 * 4)
                elif br.tag == "PHMA":
                    phma_pos = br.pos
                if br.tag == "CODE":
                    code_length = br.get_u32() 
                    # FIXME update script with lightmap synchronization
                    br.get(code_length)
                else:
                    br.skip_tag()
            # modify existing data to add missing VLM materials
            n_material_to_add = 0
            wr = biff_io.BIFF_writer()
            pr = biff_io.BIFF_writer()
            if not has_solid_bake_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Bake.Solid\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Bake.Solid\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FE) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            if not has_active_bake_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Bake.Active\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Bake.Active\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FF) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            if not has_light_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Lightmap\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Lightmap\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FF) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            print(f'. Adding {n_material_to_add} materials ({len(wr.get_data())} bytes for graphics and {len(pr.get_data())} bytes for physics)')
            br.pos = masi_pos
            br.put_u32(n_materials + n_material_to_add)
            br.pos = mate_pos - 8
            br.put_u32((n_materials + n_material_to_add) * 76 + 4)
            for i, d in enumerate(wr.get_data()):
                br.data.insert(mate_pos + i, d)
            if phma_pos > mate_pos:
                phma_pos += len(wr.get_data())
            br.pos = phma_pos - 8
            br.put_u32((n_materials + n_material_to_add) * 48 + 4)
            for i, d in enumerate(pr.get_data()):
                br.data.insert(phma_pos + i, d)
            data = bytes(data)
        if hashed:
            if mode == 0:
                data_hash.CryptHashData(data)
            elif mode == 1:
                br = biff_io.BIFF_reader(data)
                while not br.is_eof():
                    br.next()
                    if br.tag == "CODE": # For some reason, the code length info is not hashed, just the tag and code string
                        data_hash.CryptHashData(b'CODE')
                        code_length = br.get_u32() 
                        data_hash.CryptHashData(br.get(code_length))
                    else: # Biff tags and data are hashed but not their size
                        data_hash.CryptHashData(br.get_record_data(True))
        dst_stream = dst_st.CreateStream(src_path.split('/')[-1], storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(data)
        if src_path == 'GameStg/CustomInfoTags': # process the custom info tags since they need to be hashed
            br = biff_io.BIFF_reader(data)
            while not br.is_eof():
                br.next()
                if br.tag == "CUST":
                    cust_name = br.get_string()
                    print(f'Hashing custom information block {cust_name}')
                    if src_storage.exists(f'TableInfo/f{cust_name}'):
                        data = src_storage.openstream(f'TableInfo/f{cust_name}').read()
                        data_hash.CryptHashData(data)
                        dst_stream = dst_tableinfo.CreateStream(cust_name, storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
                        dst_stream.Write(data)
                else:
                    br.skip_tag()

    # FIXME append models and packmaps
    hash_size = data_hash.CryptGetHashParam(win32cryptcon.HP_HASHSIZE)
    file_hash = data_hash.CryptGetHashParam(win32cryptcon.HP_HASHVAL)
    data_hash.CryptDestroyHash()
    crypt_context.CryptReleaseContext()
    dst_stream = dst_gamestg.CreateStream('MAC', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
    dst_stream.Write(file_hash)
    dst_storage.Commit(storagecon.STGC_DEFAULT)
    src_storage.close()
    
    print(f"\nExport finished.")
    return {"FINISHED"}
    