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

bl_info = {
    "name": "Visual Pinball X Light Mapper",
    "author": "Vincent Bousquet",
    "version": (0, 0, 1),
    "blender": (3, 0, 0),
    "description": "Import/Export Visual Pinball X tables and perform automated light baking",
    "warning": "Requires installation of dependencies",
    "wiki_url": "",
    "tracker_url": "",
    "support": "COMMUNITY",
    "category": "Import-Export"}

import bpy
import os
import sys
import glob
import time
import math
import mathutils
import importlib
import subprocess
from bpy_extras.io_utils import (ImportHelper, axis_conversion)
from bpy.props import (StringProperty, BoolProperty, IntProperty, FloatProperty, FloatVectorProperty, EnumProperty, PointerProperty)
from bpy.types import (Panel, Menu, Operator, PropertyGroup, AddonPreferences, Collection)
from rna_prop_ui import PropertyPanel

# TODO


# Use import.reload for all submodule to allow iterative development using bpy.ops.script.reload()
if "vlm_dependencies" in locals():
    importlib.reload(vlm_dependencies)
else:
    from . import vlm_dependencies
if "vlm_collections" in locals():
    importlib.reload(vlm_collections)
else:
    from . import vlm_collections
if "vlm_utils" in locals():
    importlib.reload(vlm_utils)
else:
    from . import vlm_utils
if "vlm_uvpacker" in locals():
    importlib.reload(vlm_uvpacker)
else:
    from . import vlm_uvpacker

# Only load submodules that have external dependencies if they are satisfied
dependencies = (
    # OLE lib: https://olefile.readthedocs.io/en/latest/Howto.html
    vlm_dependencies.Dependency(module="olefile", package=None, name=None),
    # Pillow image processing lib: https://pillow.readthedocs.io/en/stable/
    vlm_dependencies.Dependency(module="PIL", package="Pillow", name="Pillow"),
    # Win32 native lib: https://github.com/mhammond/pywin32
    vlm_dependencies.Dependency(module="win32crypt", package="pywin32", name=None),
)
dependencies_installed = vlm_dependencies.import_dependencies(dependencies)
if dependencies_installed:
    if "biff_io" in locals():
        importlib.reload(biff_io)
    else:
        from . import biff_io
    if "vlm_import" in locals():
        importlib.reload(vlm_import)
    else:
        from . import vlm_import
    if "vlm_export" in locals():
        importlib.reload(vlm_export)
    else:
        from . import vlm_export
    if "vlm_baker" in locals():
        importlib.reload(vlm_baker)
    else:
        from . import vlm_baker


class VLM_Scene_props(PropertyGroup):
    # Importer options
    light_size: FloatProperty(name="Light Size", description="Light size factor from VPX to Blender", default = 5.0)
    light_intensity: FloatProperty(name="Light Intensity", description="Light intensity factor from VPX to Blender", default = 250.0)
    process_inserts: BoolProperty(name="Convert inserts", description="Detect inserts and converts them", default = True)
    use_pf_translucency_map: BoolProperty(name="PF Translucency Map", description="Generate a translucency map for inserts", default = True)
    process_plastics: BoolProperty(name="Convert plastics", description="Detect plastics and converts them", default = True)
    bevel_plastics: FloatProperty(name="Bevel plastics", description="Bevel converted plastics", default = 0.0)
    # Baker options
    tex_size: EnumProperty(
        items=[
            ('256', '256', '256x256', '', 256),
            ('512', '512', '512x512', '', 512),
            ('1024', '1024', '1024x1024', '', 1024),
            ('2048', '2048', '2048x2048', '', 2048),
            ('4096', '4096', '4096x4096', '', 4096),
            ('8192', '8192', '8192x8192', '', 8192),
        ],
        default='256'
    )
    tex_size_old: IntProperty(name="Tex Size:", description="Texture size", default = 256, min = 8)
    padding: IntProperty(name="Padding:", description="Padding between bakes", default = 2, min = 0)
    remove_backface: FloatProperty(name="Backface Limit", description="Angle (degree) limit for backfacing geometry removal", default = 0.0)
    # Exporter options
    export_mode: EnumProperty(
        items=[
            ('default', 'Default', 'Add bakes and lightmap to the table', '', 0),
            ('hide', 'Hide', 'Hide items that have been baked', '', 1),
            ('remove', 'Remove', 'Delete items that have been baked', '', 2),
            ('remove_all', 'Remove All', 'Delete items and images that have been baked', '', 3),
        ],
        default='default'
    )
    # Active table informations
    table_file: StringProperty(name="Table", description="Table filename", default="")
    playfield_size: FloatVectorProperty(name="Playfield size:", description="Size of the playfield in VP unit", default=(0, 0, 0, 0), size=4)


class VLM_Collection_props(PropertyGroup):
    bake_mode: EnumProperty(
        items=[
            ('default', 'Default', 'Default bake process', '', 0),
            ('movable', 'Movable', 'Bake to a splitted movable mesh', '', 1),
            ('playfield', 'Playfield', 'Bake to a dedicated orthographic playfield image', '', 2)
        ],
        default='default'
    )
    light_mode: BoolProperty(name="Group lights", description="Bake all lights as a group", default = True)


class VLM_Object_props(PropertyGroup):
    # Bake objects properties
    import_mesh: BoolProperty(name="Mesh", description="Update mesh on import", default = True)
    import_transform: BoolProperty(name="Transform", description="Update transform on import", default = True)
    render_group: IntProperty(name="Render Group", description="ID of group for batch rendering", default = -1)
    # Bake result properties
    bake_name: StringProperty(name="Bake Name", description="Lighting situation identifier", default="")
    bake_type: EnumProperty(
        items=[
            ('bake', 'Bake', 'Default bake process', '', 0),
            ('lightmap', 'Lightmap', 'Additive lightmap bake', '', 1),
            ('playfield', 'Playfield', 'Bake to a orthographic playfield sized image', '', 2)
        ],
        default='bake'
    )
    bake_tex_factor: FloatProperty(name="Bake Tex Ratio", description="Texture size factor", default=1)
    bake_packmap: IntProperty(name="Packmap", description="ID of output packmap (multiple bakes may share a packmap)", default = -1)
    bake_packmap_width: IntProperty(name="Packmap width", description="Packmap Texture width", default=1)
    bake_packmap_height: IntProperty(name="Packmap height", description="Packmap Texture height", default=1)


class VLM_OT_new(Operator):
    bl_idname = "vlm.new_operator"
    bl_label = "New"
    bl_description = "Start a new empty project"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 64
        context.scene.render.film_transparent = True
        context.scene.cycles.use_preview_denoising = True
        context.scene.vlmSettings.table_file = ""
        vlm_collections.delete_collection(vlm_collections.get_collection('ROOT'))
        vlm_collections.setup_collections()
        return {'FINISHED'}


class VLM_OT_new_from_vpx(Operator, ImportHelper):
    bl_idname = "vlm.new_from_vpx_operator"
    bl_label = "New from VPX"
    bl_description = "Start a new VPX lightmap project"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".vpx"
    filter_glob: StringProperty(default="*.vpx", options={'HIDDEN'}, maxlen=255,)
    
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 64
        context.scene.render.film_transparent = True
        context.scene.cycles.use_preview_denoising = True
        context.scene.vlmSettings.table_file = ""
        vlm_collections.delete_collection(vlm_collections.get_collection('ROOT'))
        return vlm_import.read_vpx(context, self.filepath)


class VLM_OT_update(Operator):
    bl_idname = "vlm.update_operator"
    bl_label = "Update"
    bl_description = "Update this project from the VPX file"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return os.path.exists(bpy.path.abspath(context.scene.vlmSettings.table_file))

    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        return vlm_import.read_vpx(context, bpy.path.abspath(context.scene.vlmSettings.table_file))


class VLM_OT_compute_render_groups(Operator):
    bl_idname = "vlm.compute_render_groups_operator"
    bl_label = "Groups"
    bl_description = "Evaluate render groups"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        vlm_baker.compute_render_groups(context)
        return {"FINISHED"}


class VLM_OT_render_all_groups(Operator):
    bl_idname = "vlm.render_all_groups_operator"
    bl_label = "Render"
    bl_description = "Render all groups for all lighting situation"
    bl_options = {"REGISTER"}
    
    def execute(self, context):
        vlm_baker.render_all_groups(context)
        return {"FINISHED"}


class VLM_OT_create_bake_meshes(Operator):
    bl_idname = "vlm.create_bake_meshes_operator"
    bl_label = "Bake Meshes"
    bl_description = "Create all bake meshes for all lighting situation"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        vlm_baker.create_bake_meshes(context)
        return {"FINISHED"}


class VLM_OT_render_packmaps(Operator):
    bl_idname = "vlm.render_packmaps_operator"
    bl_label = "Packmaps"
    bl_description = "Render all packmaps"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        vlm_baker.render_packmaps(context)
        return {"FINISHED"}


class VLM_OT_export_vpx(Operator):
    bl_idname = "vlm.export_vpx_operator"
    bl_label = "Export VPX"
    bl_description = "Export to an updated VPX table file"
    bl_options = {"REGISTER"}
    
    def execute(self, context):
        return vlm_export.export_vpx(context)


class VLM_OT_batch_bake(Operator):
    bl_idname = "vlm.batch_bake_operator"
    bl_label = "Batch Bake & Export"
    bl_description = "Performs all the bake steps in a batch, then export an updated VPX table (lengthy operation)"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        start_time = time.time()
        print(f"\nStarting complete bake batch...")
        vlm_baker.compute_render_groups(context)
        vlm_baker.render_all_groups(context)
        vlm_baker.create_bake_meshes(context)
        vlm_baker.render_packmaps(context)
        vlm_export.export_vpx(context)
        print(f"\nBatch baking performed in a total time of {int(time.time() - start_time)}s.")
        return {"FINISHED"}


class VLM_OT_state_hide(Operator):
    bl_idname = "vlm.state_hide_operator"
    bl_label = "Hide"
    bl_description = "Hide object from bake"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('HIDDEN', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects), None) is not None

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('HIDDEN', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects]:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_indirect(Operator):
    bl_idname = "vlm.state_indirect_operator"
    bl_label = "Indirect"
    bl_description = "Hide object from bake, but keep indirect interaction"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('INDIRECT', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects), None) is not None

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('INDIRECT', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects]:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_bake(Operator):
    bl_idname = "vlm.state_bake_operator"
    bl_label = "Bake"
    bl_description = "Enable objects for baking"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('BAKE', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects and o.type != 'LIGHT'), None) is not None
        return False

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('BAKE DEFAULT', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects and obj.type != 'LIGHT']:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_import_mesh(Operator):
    bl_idname = "vlm.state_import_mesh"
    bl_label = "Mesh"
    bl_description = "Update mesh on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_import: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects]:
                obj.vlmSettings.import_mesh = self.enable_import
        return {"FINISHED"}


class VLM_OT_state_import_transform(Operator):
    bl_idname = "vlm.state_import_transform"
    bl_label = "Transform"
    bl_description = "Update transform on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_transform: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects]:
                obj.vlmSettings.import_transform = self.enable_transform
        return {"FINISHED"}


class VLM_OT_clear_render_group_cache(Operator):
    bl_idname = "vlm.clear_render_group_cache"
    bl_label = "Clear Cache"
    bl_description = "Remove render group from cache"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('BAKE', create=False)
        if bake_col is not None:
            files = glob.glob(bpy.path.abspath(f"{vlm_utils.get_bakepath(context, type='RENDERS')}") + "* - Group *.exr")
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.render_group >= 0]:
                if next((f for f in files if f.endswith(f' {obj.vlmSettings.render_group}.exr')),None) != None:
                    return True
        return False

    def execute(self, context):
        bake_col = vlm_collections.get_collection('BAKE', create=False)
        delete_set = {}
        if bake_col is not None:
            files = glob.glob(bpy.path.abspath(f"{vlm_utils.get_bakepath(context, type='RENDERS')}") + "* - Group *.exr")
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.render_group >= 0]:
                for f in (f for f in files if f.endswith(f' {obj.vlmSettings.render_group}.exr')):
                    delete_set[f] = True
            for f in delete_set:
                os.remove(f)
        return {"FINISHED"}


class VLM_OT_select_render_group(Operator):
    bl_idname = "vlm.select_render_group"
    bl_label = "Select"
    bl_description = "Select all object from this render group"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('BAKE', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.render_group >= 0), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('BAKE', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.render_group >= 0]:
                for other in bake_col.all_objects:
                    if other.vlmSettings.render_group == obj.vlmSettings.render_group:
                        other.select_set(True)
        return {"FINISHED"}


class VLM_OT_select_packmap_group(Operator):
    bl_idname = "vlm.select_packmap_group"
    bl_label = "Select"
    bl_description = "Select all object from this packmap"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.bake_packmap >= 0), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.bake_packmap >= 0]:
                for other in bake_col.all_objects:
                    if other.vlmSettings.bake_packmap == obj.vlmSettings.bake_packmap:
                        other.select_set(True)
        return {"FINISHED"}


class VLM_OT_export_packmap(Operator):
    bl_idname = "vlm.export_packmap_operator"
    bl_label = "Bake PackMap"
    bl_description = "Compute and save the packed bake map for the selected bake meshes"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        object_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        if object_col is not None:
            for obj in context.selected_objects:
                if obj.name in object_col.all_objects and "vlm.name" in obj and "vlm.is_light" in obj and "vlm.tex_width" in obj and "vlm.tex_height" in obj:
                    return True
        return False

    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        result_col = vlm_collections.get_collection('BAKE RESULT')
        bakepath = f"//{bpy.path.basename(context.blend_data.filepath)} - Bakes/"
        for obj in context.selected_objects:
            if obj.name in result_col.all_objects:
                vlm_export.export_packmap(obj, obj["vlm.name"], obj["vlm.is_light"] != 0, obj["vlm.tex_width"], obj["vlm.tex_height"], vlmProps.export_webp, vlmProps.padding, False, f"{bakepath}{obj['vlm.name']}.png")
        return {"FINISHED"}


class VLM_OT_load_render_images(Operator):
    bl_idname = "vlm.load_render_images_operator"
    bl_label = "Load/Unload Renders"
    bl_description = "Load/Unload render images for preview"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        object_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        if object_col is not None:
            for obj in context.selected_objects:
                if obj.name in object_col.all_objects:
                    return True
        return False

    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        result_col = vlm_collections.get_collection('BAKE RESULT')
        bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
        for obj in context.selected_objects:
            if obj.name in result_col.all_objects:
                paths = [f"{bakepath}{obj.vlmSettings.bake_name} - Group {i}.exr" for i,_ in enumerate(obj.data.materials)]
                all_loaded = all((vlm_utils.image_by_path(path) is not None for path in paths))
                if all_loaded:
                    for path in paths:
                        bpy.data.images.remove(vlm_utils.image_by_path(path))
                else:
                    for path, mat in zip(paths, obj.data.materials):
                        mat.node_tree.nodes["BakeTex"].image = bpy.data.images.load(path, check_existing=True)
        return {"FINISHED"}


class VLM_PT_Properties(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        vlmProps = scene.vlmSettings

        layout.label(text="VPX Importer", icon='IMPORT') 
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_new.bl_idname)
        row.operator(VLM_OT_new_from_vpx.bl_idname)
        row.operator(VLM_OT_update.bl_idname)
        layout.prop(vlmProps, "table_file")
        row = layout.row()
        row.prop(vlmProps, "process_plastics")
        row.prop(vlmProps, "bevel_plastics")
        row = layout.row()
        row.prop(vlmProps, "process_inserts")
        row.prop(vlmProps, "use_pf_translucency_map")
        row = layout.row()
        row.prop(vlmProps, "light_size")
        row.prop(vlmProps, "light_intensity")

        layout.separator()

        layout.label(text="LightMap Baker", icon='RENDERLAYERS') 
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.prop(vlmProps, "tex_size", expand=True)
        row = layout.row()
        row.prop(vlmProps, "padding")
        row.prop(vlmProps, "remove_backface", text='Backface')

        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_compute_render_groups.bl_idname)
        row.operator(VLM_OT_render_all_groups.bl_idname)
        row.operator(VLM_OT_create_bake_meshes.bl_idname)
        row.operator(VLM_OT_render_packmaps.bl_idname)

        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_batch_bake.bl_idname)
        
        layout.separator()

        layout.label(text="Baked Model Exporter", icon='EXPORT') 
        row = layout.row(align=True)
        row.prop(vlmProps, "export_mode", expand=True)
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_export_vpx.bl_idname)
        
        layout.separator()

        versionStr = "VPX Light Mapper Version: %d.%d.%d" % bl_info["version"]
        layout.label(text=versionStr, icon="SETTINGS") 
        row = layout.row()
        row.scale_y = 1.5
        row.operator("wm.url_open", text="Light Mapper" , icon="QUESTION").url = "https://github.com/vbousquet/vlm"
        row.operator("wm.url_open", text="Visual Pinball X", icon="HOME").url = "https://github.com/vpinball/vpinball"


class VLM_PT_Col_Props(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "collection"

    def draw(self, context):
        layout = self.layout
        col = context.collection
        bake_col = vlm_collections.get_collection('BAKE')
        light_col = vlm_collections.get_collection('LIGHTS')
        if col.name in bake_col.children:
            layout.label(text="Bake mode:") 
            layout.prop(col.vlmSettings, 'bake_mode', expand=True)
        elif col.name in light_col.children:
            layout.prop(col.vlmSettings, 'light_mode', expand=True)
        else:
            layout.label(text="Select a bake or light group") 


class VLM_PT_3D(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        show_info = True
        root_col = vlm_collections.get_collection('ROOT', create=False)
        result_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        
        bake_objects = [obj for obj in context.selected_objects if (root_col is not None and obj.name in root_col.all_objects) and (result_col is None or obj.name not in result_col.all_objects)]
        if bake_objects:
            show_info = False
            layout.label(text="Import options:")
            row = layout.row(align=True)
            row.scale_y = 1.5
            if all((x.vlmSettings.import_mesh for x in bake_objects)):
                row.operator(VLM_OT_state_import_mesh.bl_idname, text='On', icon='MESH_DATA').enable_import = False
            elif all((not x.vlmSettings.import_mesh for x in bake_objects)):
                row.operator(VLM_OT_state_import_mesh.bl_idname, text='Off', icon='MESH_DATA').enable_import = True
            else:
                row.operator(VLM_OT_state_import_mesh.bl_idname, text='-', icon='MESH_DATA').enable_import = True
            if all((x.vlmSettings.import_transform for x in bake_objects)):
                row.operator(VLM_OT_state_import_transform.bl_idname, text='On', icon='OBJECT_ORIGIN').enable_transform = False
            elif all((not x.vlmSettings.import_transform for x in bake_objects)):
                row.operator(VLM_OT_state_import_transform.bl_idname, text='Off', icon='OBJECT_ORIGIN').enable_transform = True
            else:
                row.operator(VLM_OT_state_import_transform.bl_idname, text='-', icon='MATERIAL').enable_transform = True
            layout.separator()
            layout.label(text="Bake visibility:")
            row = layout.row(align=True)
            row.scale_y = 1.5
            row.operator(VLM_OT_state_hide.bl_idname)
            row.operator(VLM_OT_state_indirect.bl_idname)
            row.operator(VLM_OT_state_bake.bl_idname)
            
            single_group = -1
            for obj in bake_objects:
                if single_group == -1:
                    single_group = obj.vlmSettings.render_group
                elif single_group != obj.vlmSettings.render_group:
                    single_group = -2
            if single_group == -2:
                layout.label(text="Multiple render groups")
            elif single_group == -1:
                layout.label(text="Undefined render groups")
            else:
                layout.label(text=f"Render group #{single_group}")
            row = layout.row(align=True)
            row.operator(VLM_OT_clear_render_group_cache.bl_idname)
            row.operator(VLM_OT_select_render_group.bl_idname)

        result_objects = [obj for obj in context.selected_objects if result_col is not None and obj.name in result_col.all_objects]
        if result_objects:
            show_info = False
            if len(result_objects) == 1:
                layout.prop(result_objects[0].vlmSettings, 'bake_name', text='Name', expand=True)
                layout.prop(result_objects[0].vlmSettings, 'bake_type', expand=True)
                layout.prop(result_objects[0].vlmSettings, 'bake_tex_factor', expand=True)
                row = layout.row(align=True)
                row.prop(result_objects[0].vlmSettings, 'bake_packmap', expand=True, text="Pack:")
                row.operator(VLM_OT_select_packmap_group.bl_idname)
                row = layout.row(align=True)
                row.prop(result_objects[0].vlmSettings, 'bake_packmap_width', expand=True, text="W:")
                row.prop(result_objects[0].vlmSettings, 'bake_packmap_height', expand=True, text="H:")
            layout.separator()
            layout.operator(VLM_OT_export_packmap.bl_idname)
            layout.separator()
            layout.operator(VLM_OT_load_render_images.bl_idname)
            
        if show_info:
            layout.label(text="Select a baked object or a bake result") 


class VLM_PT_3D_warning_panel(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def draw(self, context):
        layout = self.layout
        lines = [f"Please install the missing dependencies",
                 f"for the \"{bl_info.get('name')}\" add-on.",
                 f"1. Open the preferences (Edit > Preferences > Add-ons).",
                 f"2. Search for the \"{bl_info.get('name')}\" add-on.",
                 f"3. Open the details section of the add-on.",
                 f"4. Click on the \"{VLM_OT_install_dependencies.bl_label}\" button.",
                 f"   This will download and install the missing",
                 f"   Python packages, if Blender has the required",
                 f"   permissions."]
        for line in lines:
            layout.label(text=line)


class VLM_PT_Props_warning_panel(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def draw(self, context):
        layout = self.layout
        lines = [f"Please install the missing dependencies",
                 f"for the \"{bl_info.get('name')}\" add-on.",
                 f"1. Open the preferences (Edit > Preferences > Add-ons).",
                 f"2. Search for the \"{bl_info.get('name')}\" add-on.",
                 f"3. Open the details section of the add-on.",
                 f"4. Click on the \"{VLM_OT_install_dependencies.bl_label}\" button.",
                 f"   This will download and install the missing",
                 f"   Python packages, if Blender has the required",
                 f"   permissions."]
        for line in lines:
            layout.label(text=line)


class VLM_OT_install_dependencies(bpy.types.Operator):
    bl_idname = "vlm.install_dependencies"
    bl_label = "Install dependencies"
    bl_description = ("Downloads and installs the required python packages for this add-on. "
                      "Internet connection is required. Blender may have to be started with "
                      "elevated permissions in order to install the package")
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def execute(self, context):
        try:
            vlm_dependencies.install_dependencies(dependencies)
        except (subprocess.CalledProcessError, ImportError) as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        global dependencies_installed
        dependencies_installed = True
        for cls in classes:
            bpy.utils.register_class(cls)
        return {"FINISHED"}


class VLM_preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        layout.operator(VLM_OT_install_dependencies.bl_idname, icon="CONSOLE")


classes = (
    VLM_Scene_props,
    VLM_Collection_props,
    VLM_Object_props,
    VLM_PT_Col_Props,
    VLM_PT_3D,
    VLM_PT_Properties,
    VLM_OT_new,
    VLM_OT_new_from_vpx,
    VLM_OT_update,
    VLM_OT_compute_render_groups,
    VLM_OT_render_all_groups,
    VLM_OT_create_bake_meshes,
    VLM_OT_render_packmaps,
    VLM_OT_batch_bake,
    VLM_OT_state_hide,
    VLM_OT_state_indirect,
    VLM_OT_state_bake,
    VLM_OT_state_import_mesh,
    VLM_OT_state_import_transform,
    VLM_OT_clear_render_group_cache,
    VLM_OT_select_render_group,
    VLM_OT_select_packmap_group,
    VLM_OT_export_packmap,
    VLM_OT_load_render_images,
    VLM_OT_export_vpx,
    )
preference_classes = (VLM_PT_3D_warning_panel, VLM_PT_Props_warning_panel, VLM_OT_install_dependencies, VLM_preferences)
registered_classes = []


def register():
    global dependencies_installed
    dependencies_installed = False
    for cls in preference_classes:
        bpy.utils.register_class(cls)
        registered_classes.append(cls)
    dependencies_installed = vlm_dependencies.import_dependencies(dependencies)
    if dependencies_installed:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered_classes.append(cls)
        bpy.types.Scene.vlmSettings = PointerProperty(type=VLM_Scene_props)
        bpy.types.Collection.vlmSettings = PointerProperty(type=VLM_Collection_props)
        bpy.types.Object.vlmSettings = PointerProperty(type=VLM_Object_props)
    else:
        print(f"VPX light mapper was not installed due to missing dependencies")


def unregister():
    for cls in registered_classes:
        bpy.utils.unregister_class(cls)
    if dependencies_installed:
        del bpy.types.Scene.vlmSettings
        del bpy.types.Collection.vlmSettings
        del bpy.types.Object.vlmSettings


if __name__ == "__main__":
    register()