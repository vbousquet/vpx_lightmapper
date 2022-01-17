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
from . import biff_io
from . import vlm_utils
from . import vlm_collections

# Dependencies which need a custom install (not included in the Blender install)
import olefile
import pythoncom
import win32crypt
import win32cryptcon
from win32com import storagecon

# FIXME remove wincrypto dependency, pywin32 should do the job despite missing DeriveKey
#import wincrypto
#from wincrypto import CryptCreateHash, CryptHashData, CryptDeriveKey
#from wincrypto.constants import CALG_MD5


# TODO
# - The aim is to export by updating the VPX table (for the time being, we just support basic OBJ export)
# - Try exporting bakes as HDR (or provide an evaluation of the benefit it would give)
# - Try generating a default VBS script for eady integration
# - Try computing bakemap histogram, select the right format depending on the intensity span (EXR / brightness adjusted PNG or WEBP)


def export_packmap(bake_instance, name, is_light, tex_width, tex_height, opt_save_webp, opt_padding, opt_force_render, target_path):
    if opt_force_render or not os.path.exists(target_path):
        pack_image = bpy.data.images.new(f"PackMap.{name}", tex_width, tex_height, alpha=True)
        for mat in bake_instance.data.materials:
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 0.0 # Bake mode
            if is_light:
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 1.0
            else:
                mat.node_tree.nodes["PackMap"].inputs[2].default_value = 0.0
            mat.blend_method = 'OPAQUE'
            mat.node_tree.nodes["PackTex"].image = pack_image
            mat.node_tree.nodes.active = mat.node_tree.nodes["PackTex"]
        bpy.context.scene.render.bake.margin = opt_padding
        bpy.context.scene.render.bake.use_clear = True
    
        cg = vlm_utils.push_color_grading(True)
        bpy.ops.object.bake(type='EMIT', margin=opt_padding)
        pack_image.save_render(target_path)
        vlm_utils.pop_color_grading(cg)
        
        bpy.data.images.remove(pack_image)
        for mat in bake_instance.data.materials:
            mat.node_tree.nodes["PackMap"].inputs[3].default_value = 1.0 # Preview mode
            if is_light:
                mat.blend_method = 'BLEND'
    if opt_save_webp:
        webp_path = bpy.path.abspath(context.scene.render.filepath).removesuffix('.png') + '.webp'
        if opt_force_render or not os.path.exists(webp_path):
            im = Image.open(target_path)
            im.save(webp_path, 'WEBP')


def export_all(context):
    return export_vpx(context)
    
    vlmProps = context.scene.vlmSettings
    result_col = vlm_collections.get_collection('BAKE RESULT')
    exportpath = f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Export/"
    vlm_utils.mkpath(exportpath)
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, result_col).exclude = False
    print(f"\nStarting export process for {len(result_col.all_objects)} bakes")
    
    for i, obj in enumerate(result_col.all_objects, start = 1):
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        print(f". {i}/{len(result_col.all_objects)} Exporting packed bake maps for '{obj.name}'")
        export_packmap(obj, obj["vlm.name"], obj["vlm.is_light"] != 0, obj["vlm.tex_width"], obj["vlm.tex_height"], vlmProps.export_webp, vlmProps.padding, False, bpy.path.abspath(f"{exportpath}{obj.name}.png"))

        # see https://docs.blender.org/api/current/bpy.ops.export_scene.html
        print(f". {i}/{len(result_col.all_objects)} Exporting bake mesh for '{obj.name}'")
        bpy.ops.export_scene.obj(
            filepath=os.path.join(bpy.path.abspath(exportpath), f"{obj.name}.obj"),
            use_selection=True, use_materials=False, use_triangles=True,
            global_scale=1000.0 / global_scale, axis_forward='-Y', axis_up='-Z')

    print(f"\nExport finished.")
    return {"FINISHED"}

#FIXME export directly to an updated VPX file (it is not really manageable manually with hundreds of lightmap and grouped packmaps)
# olefile doesn't support create ole file. We will need anoter lib or create our own...
# For crypto, use: https://github.com/crappycrypto/wincrypto
# this needs to add the hash algo for MD2 (from pycryptodome or simply https://gist.github.com/CameronLonsdale/23772092aa4e0c75f2426eb418b156e6)
#
# Another way to go would be to go native with pywin32: https://github.com/mhammond/pywin32
# It includes wincrypt (missing DeriveKey...), and ole storage (in pythoncom, seems ok)
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
    
    input_path = bpy.path.abspath(vlmProps.table_file)
    if not os.path.isfile(input_path):
        self.report({'WARNING'},f"{input_path} does not exist")
        return {'CANCELLED'}
    
    output_path = bpy.path.abspath(f"//{os.path.splitext(bpy.path.basename(input_path))[0]} - VLM.vpx")

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
    # path, 0=unstructured bytes/1=BIFF, hashed ?
    file_structure = [
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
    append_structure('GameStg/GameItem', 1, False),
    append_structure('GameStg/Sound', 1, False),
    append_structure('GameStg/Image', 1, False),
    append_structure('GameStg/Font', 1, False),
    append_structure('GameStg/Collection', 1, True),

    game_item_index = 0
    image_index = 0
    for src_path, mode, hashed in file_structure:
        if not src_storage.exists(src_path):
            continue
        if src_path.startswith('GameStg'):
            dst_st = dst_gamestg
        else:
            dst_st = dst_tableinfo
        data = src_storage.openstream(src_path).read()
        # FIXME modify GameData to include VLM materials if not present
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
                        # FIXME update script with lightmap synchronization
                        data_hash.CryptHashData(br.get(code_length))
                    else: # Biff tags and data are hashed but not their size
                        data_hash.CryptHashData(br.get_record_data(True))
        remove_item = False
        if src_path.startswith('GameStg/GameItem') or src_path.startswith('GameStg/Image'):
            br = biff_io.BIFF_reader(data)
            name = 'unknown'
            # FIXME change visibility of backed elements
            while not br.is_eof():
                br.next()
                if br.tag == "NAME":
                    name = br.get_string()
                    break;
                br.skip_tag()
            if name.startswith('VLM.'):
                remove_item = True
            elif src_path.startswith('GameStg/GameItem'):
                src_path = f'GameStg/GameItem{game_item_index}'
                game_item_index += 1
            elif src_path.startswith('GameStg/Image'):
                src_path = f'GameStg/Image{image_index}'
                image_index += 1
        if not remove_item:
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
    