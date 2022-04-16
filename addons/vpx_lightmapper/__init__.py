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
if "vlm_occlusion" in locals():
    importlib.reload(vlm_occlusion)
else:
    from . import vlm_occlusion
if "vlm_camera" in locals():
    importlib.reload(vlm_camera)
else:
    from . import vlm_camera

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
    if "vlm_export_obj" in locals():
        importlib.reload(vlm_export_obj)
    else:
        from . import vlm_export_obj
    if "vlm_group_baker" in locals():
        importlib.reload(vlm_group_baker)
    else:
        from . import vlm_group_baker
    if "vlm_render_baker" in locals():
        importlib.reload(vlm_render_baker)
    else:
        from . import vlm_render_baker
    if "vlm_meshes_baker" in locals():
        importlib.reload(vlm_meshes_baker)
    else:
        from . import vlm_meshes_baker
    if "vlm_nestmap_baker" in locals():
        importlib.reload(vlm_nestmap_baker)
    else:
        from . import vlm_nestmap_baker
    if "vlm_nest" in locals():
        importlib.reload(vlm_nest)
    else:
        from . import vlm_nest


def unit_update(self, context):
    if context.scene.vlmSettings.units_mode == 'inch':
        context.scene.unit_settings.system = 'IMPERIAL'
        context.scene.unit_settings.length_unit = 'INCHES'
    elif context.scene.vlmSettings.units_mode == 'cm':
        context.scene.unit_settings.system = 'METRIC'
        context.scene.unit_settings.length_unit = 'CENTIMETERS'
    elif context.scene.vlmSettings.units_mode == 'vpx':
        context.scene.unit_settings.system = 'NONE'
    new_scale = vlm_utils.get_global_scale(context)
    if context.scene.vlmSettings.active_scale != new_scale:
        print(f'old scale:{context.scene.vlmSettings.active_scale} => new scale:{new_scale}')
        scaling = new_scale / context.scene.vlmSettings.active_scale
        # FIXME scale all objects relative to origin
        for i in range(4):
            context.scene.vlmSettings.playfield_size[i] = scaling * context.scene.vlmSettings.playfield_size[i]
        context.scene.vlmSettings.active_scale = new_scale
        vlm_camera.camera_inclination_update


def select_render_group(self, context):
    for obj in context.scene.collection.all_objects:
        obj.select_set(obj.name in context.view_layer.objects and obj.vlmSettings.render_group == context.scene.vlmSettings.render_group_select)


class VLM_Scene_props(PropertyGroup):
    # Importer options
    units_mode: EnumProperty(
        items=[
            ('vpx', 'VPX', 'VPX units', '', 0),
            ('inch', 'Inch', 'Inches (50 VPX = 1.0625")', '', 1),
            ('cm', 'cm', 'Centimeters (50 VPX = 2.69875cm)', '', 2)
        ],
        name='Units',
        default='inch', 
        update=unit_update
    )
    light_size: FloatProperty(name="Light Size", description="Light size factor from VPX to Blender", default = 5.0)
    light_intensity: FloatProperty(name="Light Intensity", description="Light intensity factor from VPX to Blender", default = 250.0)
    insert_size: FloatProperty(name="Insert Size", description="Inserts light size factor from VPX to Blender", default = 0.0)
    insert_intensity: FloatProperty(name="Insert Intensity", description="Insert intensity factor from VPX to Blender", default = 25.0)
    process_inserts: BoolProperty(name="Convert inserts", description="Detect inserts and converts them", default = True)
    use_pf_translucency_map: BoolProperty(name="Translucency Map", description="Generate a translucency map for inserts", default = True)
    process_plastics: BoolProperty(name="Convert plastics", description="Detect plastics and converts them", default = True)
    bevel_plastics: FloatProperty(name="Bevel plastics", description="Bevel converted plastics", default = 1.0)
    camera_inclination: FloatProperty(name="Inclination", description="Camera inclination", default = 15.0, update=vlm_camera.camera_inclination_update)
    camera_layback: FloatProperty(name="Layback", description="Camera layback", default = 35.0, update=vlm_camera.camera_inclination_update)
    layback_mode: EnumProperty(
        items=[
            ('disable', 'Disable', 'Disable layback', '', 0),
            ('deform', 'Deform', 'Apply layback to geometry. This breaks reflection/refraction', '', 1),
            ('camera', 'Camera', 'Apply layback to camera.', '', 2)
        ],
        name='Layback mode',
        default='camera', 
        update=vlm_camera.camera_inclination_update
    )
    # Baker options
    last_bake_step: EnumProperty(
        items=[
            ('unstarted', 'Unstarted', '', '', 0),
            ('groups', 'Groups', '', '', 1),
            ('renders', 'Rendered', '', '', 2),
            ('meshes', 'Meshes', '', '', 3),
            ('nestmaps', 'Nestmaps', '', '', 4),
        ],
        name='Last Bake Step',
        default='unstarted'
    )
    tex_size: EnumProperty(
        items=[
            ('256', '256', '256x256', '', 256),
            ('512', '512', '512x512', '', 512),
            ('1024', '1024', '1024x1024', '', 1024),
            ('2048', '2048', '2048x2048', '', 2048),
            ('4096', '4096', '4096x4096', '', 4096),
            ('8192', '8192', '8192x8192', '', 8192),
        ],
        name='Render size',
        default='256', update=vlm_camera.camera_inclination_update
    )
    render_aspect_ratio: FloatProperty(name="Render AR", description="Aspect ratio of render bakes", default = 1.0)
    padding: IntProperty(name="Padding", description="Padding between bakes", default = 2, min = 0)
    remove_backface: FloatProperty(name="Backface Limit", description="Angle (degree) limit for backfacing geometry removal", default = 0.0)
    keep_pf_reflection_faces: BoolProperty(name="Keep playfield reflection", description="Keep faces only visible through playfield reflection", default = False)
    # Exporter options
    enable_vpx_reflection: BoolProperty(name="Enable VPX reflection", description="Enable VPX playfield reflection for exported models and lightmaps", default = True)
    export_mode: EnumProperty(
        items=[
            ('default', 'Default', 'Add bakes and lightmap to the table', '', 0),
            ('hide', 'Hide', 'Hide items that have been baked', '', 1),
            ('remove', 'Remove', 'Delete items that have been baked', '', 2),
            ('remove_all', 'Remove All', 'Delete items and images that have been baked', '', 3),
        ],
        name='Export mode',
        default='remove_all'
    )
    playfield_col: PointerProperty(name="Playfield", type=bpy.types.Collection, description="Bake collection used for VPX playfield (object rendered with table reflections)")
    # Active table informations
    table_file: StringProperty(name="Table", description="Table filename", default="")
    playfield_size: FloatVectorProperty(name="Playfield size:", description="Size of the playfield in VP unit", default=(0, 0, 0, 0), size=4)
    active_scale: FloatProperty(name="Active scale", description="Scale of the active table", default = 1.0)
    # Tools
    render_group_select: IntProperty(name="Select Group", description="Select all objects from a render group", default = 0, min = 0, update=select_render_group)


class VLM_Collection_props(PropertyGroup):
    bake_mode: EnumProperty(
        items=[
            ('default', 'Default', 'Default bake process', '', 0),
            ('movable', 'Movable', 'Bake to a splitted movable mesh', '', 1),
        ],
        name='Bake Mode',
        description='Bake mode for the selected collection',
        default='default'
    )
    is_active_mat: BoolProperty(name="Active Material", description="True if this bake group need an 'Active' material (non opaque, under playfield,...)", default = False)
    light_mode: EnumProperty(
        items=[
            ('solid', 'Solid', 'Base solid bake', '', 0),
            ('group', 'Group', 'Bake all lights as a single lightmap group', '', 1),
            ('split', 'Split', 'Bake each light as a separate lightmap', '', 2)
        ],
        name='Light Mode',
        description='Light mode for the selected collection',
        default='group'
    )
    world: PointerProperty(name="World", type=bpy.types.World, description="World lighting to be used (should be empty for playfield lights)")


class VLM_Object_props(PropertyGroup):
    # Bake objects properties
    vpx_object: StringProperty(name="VPX", description="Identifier of reference VPX object", default = '')
    vpx_subpart: StringProperty(name="Part", description="Sub part identifier for multi part object like bumpers,...", default = '')
    layback_offset: FloatProperty(name="Layback offset", description="Y offset caused by current layback", default = 0.0)
    import_mesh: BoolProperty(name="Mesh", description="Update mesh on import", default = True)
    import_transform: BoolProperty(name="Transform", description="Update transform on import", default = True)
    indirect_only: BoolProperty(name="Indirect", description="Do not bake this object but consider its influence on the scene", default = False)
    render_group: IntProperty(name="Render Group", description="ID of group for batch rendering", default = -1)
    is_rgb_led: BoolProperty(name="RGB Led", description="RGB Led (lightmapped to white then colored in VPX for dynamic colors)", default = False)
    enable_aoi: BoolProperty(name="Enable AOI", description="Area Of Influence rendering optimization", default = True)
    enable_glow: BoolProperty(name="Enable Glow", description="Enable light glow", default = False)
    bake_to: PointerProperty(name="Bake To", type=bpy.types.Object, description="Target object used as bake mesh target")
    # Movable objects bake settings
    movable_influence: EnumProperty(
        items=[
            ('indirect', 'Indirect', 'Allow indirect contribution of this object to other bakes', '', 0),
            ('hide', 'Hide', 'Hide this object from the other bakes. WARNING: this feature has limited support. see doc.', '', 1),
        ],
        default='indirect'
    )
    # Bake result properties
    bake_lighting: StringProperty(name="Lighting", description="Lighting scenario", default="")
    bake_objects: StringProperty(name="Source", description="Object or collection of objects included in this bake/lightmap", default="")
    bake_sync_light: StringProperty(name="Sync Light", description="Object to sync light state on", default="")
    bake_sync_trans: StringProperty(name="Sync Trans", description="Object to sync transform on", default="")
    bake_type: EnumProperty(
        items=[
            ('default', 'Default', 'Default opaque bake', '', 0),
            ('static', 'Static', 'Static opaque bake', '', 1),
            ('active', 'Active', "'Active', i.e. non opaque, bake", '', 2),
            ('lightmap', 'Lightmap', 'Additive lightmap bake', '', 3),
        ],
        name="Type",
        default='default'
    )
    bake_hdr_range: FloatProperty(name="HDR Range", description="HDR range of this bake", default=1)
    bake_nestmap: IntProperty(name="Nestmap", description="ID of output nestmap (multiple bakes may share a nestmap)", default = -1)


class VLM_OT_new_from_vpx(Operator, ImportHelper):
    bl_idname = "vlm.new_from_vpx_operator"
    bl_label = "Import"
    bl_description = "Start a new VPX lightmap project"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".vpx"
    filter_glob: StringProperty(default="*.vpx", options={'HIDDEN'}, maxlen=255,)
    
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.render.film_transparent = True
        context.scene.cycles.film_transparent_glass = True
        context.scene.vlmSettings.table_file = ""
        context.scene.vlmSettings.last_bake_step = "unstarted"
        unit_update(self, context)
        return vlm_import.read_vpx(self, context, self.filepath)


class VLM_OT_update(Operator):
    bl_idname = "vlm.update_operator"
    bl_label = "Update"
    bl_description = "Update this project from the VPX file"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return os.path.exists(bpy.path.abspath(context.scene.vlmSettings.table_file))

    def execute(self, context):
        unit_update(self, context)
        return vlm_import.read_vpx(self, context, bpy.path.abspath(context.scene.vlmSettings.table_file))


class VLM_OT_select_occluded(Operator):
    bl_idname = "vlm.select_occluded_operator"
    bl_label = "Select Occluded"
    bl_description = "Select occluded objects"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        return vlm_occlusion.select_occluded(self, context)


class VLM_OT_select_indirect(Operator):
    bl_idname = "vlm.select_indirect_operator"
    bl_label = "Select Indirect"
    bl_description = "Select objects that indirectly affect rendering"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        for obj in context.scene.collection.all_objects:
            obj.select_set(obj.name in context.view_layer.objects and obj.vlmSettings.indirect_only)
        return {'FINISHED'}


class VLM_OT_compute_render_groups(Operator):
    bl_idname = "vlm.compute_render_groups_operator"
    bl_label = "1. Groups"
    bl_description = "Evaluate render groups"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        return vlm_group_baker.compute_render_groups(self, context)


class VLM_OT_render_all_groups(Operator):
    bl_idname = "vlm.render_all_groups_operator"
    bl_label = "2. Render"
    bl_description = "Render all groups for all lighting situation"
    bl_options = {"REGISTER"}
    
    def execute(self, context):
        return vlm_render_baker.render_all_groups(self, context)


class VLM_OT_create_bake_meshes(Operator):
    bl_idname = "vlm.create_bake_meshes_operator"
    bl_label = "3. Bake Meshes"
    bl_description = "Create all bake meshes for all lighting situation"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        return vlm_meshes_baker.create_bake_meshes(self, context)


class VLM_OT_render_nestmaps(Operator):
    bl_idname = "vlm.render_nestmaps_operator"
    bl_label = "4. Nestmaps"
    bl_description = "Compute and render all nestmaps"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        return result_col is not None and len(result_col.all_objects) > 0

    def execute(self, context):
        return vlm_nestmap_baker.render_nestmaps(self, context)


class VLM_OT_export_vpx(Operator):
    bl_idname = "vlm.export_vpx_operator"
    bl_label = "5. Export VPX"
    bl_description = "Export to an updated VPX table file"
    bl_options = {"REGISTER"}
    
    def execute(self, context):
        return vlm_export.export_vpx(self, context)


class VLM_OT_export_bake(Operator):
    bl_idname = "vlm.export_bake_operator"
    bl_label = "Export OBJ"
    bl_description = "Export bake to a Wavefront OBJ file with its texture"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return next((obj for obj in context.selected_objects if obj.vlmSettings.bake_lighting != ''), None) is not None

    def execute(self, context):
        return vlm_export_obj.export_obj(self, context)


class VLM_OT_export_pov(Operator):
    bl_idname = "vlm.export_pov_operator"
    bl_label = "Export POV"
    bl_description = "Export a POV file for easy VPX/Blender comparison. Needs manual adjustment in VPX from console informations."
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) == 1 and context.selected_objects[0].type == 'CAMERA'

    def execute(self, context):
        camera_object = context.selected_objects[0]
        path = bpy.path.abspath(f'{vlm_utils.get_bakepath(context, type="EXPORT")}{camera_object.name}.pov')
        sections = ['desktop', 'fullscreen', 'fullsinglescreen']
        with open(path, 'w') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<POV>\n')
            for section in sections:
                f.write(f'\t<{section}>\n')
                f.write(f'\t\t<inclination>{math.degrees(camera_object.rotation_euler[0])}</inclination>\n')
                f.write(f'\t\t<fov>{math.degrees(camera_object.data.angle)}</fov>\n')
                f.write(f'\t\t<layback>0.000000</layback>\n')
                f.write(f'\t\t<rotation>270.000000</rotation>\n')
                #f.write(f'\t\t<rotation>0.000000</rotation>\n')
                f.write(f'\t\t<xscale>1.000000</xscale>\n')
                f.write(f'\t\t<yscale>1.000000</yscale>\n')
                f.write(f'\t\t<zscale>1.000000</zscale>\n')
                f.write(f'\t\t<xoffset>0.000000</xoffset>\n')
                f.write(f'\t\t<yoffset>0.000000</yoffset>\n')
                f.write(f'\t\t<zoffset>0.000000</zoffset>\n')
                f.write(f'\t</{section}>\n')
            f.write('</POV>\n')
        scale = 1.0 / vlm_utils.get_global_scale(context)
        print(f"\nCamera POV exported to '{path}'")
        if camera_object.data.shift_x != 0 or camera_object.data.shift_y != 0:
            print(f"WARNING: Blender's camera is shifted by {camera_object.data.shift_x}, {camera_object.data.shift_y}")
        if context.scene.render.pixel_aspect_x != 1 or context.scene.render.pixel_aspect_y != 1:
            print(f"WARNING: Blender's pixel aspect is not squared: {context.scene.render.pixel_aspect_x}, {context.scene.render.pixel_aspect_y}")
        print(f"Use VPX 'F6' mode to move camera to ({round(-scale*camera_object.location[1])}, {round(scale*camera_object.location[0])}, {round(scale*camera_object.location[2])})\n")
        return {'FINISHED'}


class VLM_OT_batch_bake(Operator):
    bl_idname = "vlm.batch_bake_operator"
    bl_label = "Batch All"
    bl_description = "Performs all the bake steps in a batch, then export an updated VPX table (lengthy operation)"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        start_time = time.time()
        print(f"\nStarting complete bake batch...")
        result = vlm_group_baker.compute_render_groups(self, context)
        if 'FINISHED' not in result: return result
        result = vlm_render_baker.render_all_groups(self, context)
        if 'FINISHED' not in result: return result
        result = vlm_meshes_baker.create_bake_meshes(self, context)
        if 'FINISHED' not in result: return result
        result = vlm_nestmap_baker.render_nestmaps(self, context)
        if 'FINISHED' not in result: return result
        result = vlm_export.export_vpx(self, context)
        if 'FINISHED' not in result: return result
        print(f"\nBatch baking performed in {vlm_utils.format_time(time.time() - start_time)}")
        return {'FINISHED'}


class VLM_OT_state_indirect_only(Operator):
    bl_idname = "vlm.state_indirect_only"
    bl_label = "Indirect"
    bl_description = "Only affect rendering indirectly (reflection/refraction and shadows)"
    bl_options = {"REGISTER", "UNDO"}
    indirect_only: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        for obj in [obj for obj in context.selected_objects]:
            obj.vlmSettings.indirect_only = self.indirect_only
        return {"FINISHED"}


class VLM_OT_state_import_mesh(Operator):
    bl_idname = "vlm.state_import_mesh"
    bl_label = "Mesh"
    bl_description = "Update mesh on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_import: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        for obj in [obj for obj in context.selected_objects]:
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
        return True

    def execute(self, context):
        for obj in [obj for obj in context.selected_objects]:
            obj.vlmSettings.import_transform = self.enable_transform
        return {"FINISHED"}


class VLM_OT_clear_render_group_cache(Operator):
    bl_idname = "vlm.clear_render_group_cache"
    bl_label = "Clear Cache"
    bl_description = "Remove render group from cache"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        if bake_col is not None:
            files = glob.glob(bpy.path.abspath(f"{vlm_utils.get_bakepath(context, type='RENDERS')}") + "* - Group *.exr")
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects and obj.vlmSettings.render_group >= 0]:
                if next((f for f in files if f.endswith(f' {obj.vlmSettings.render_group}.exr')),None) != None:
                    return True
        return False

    def execute(self, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
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
        return len(set((obj.vlmSettings.render_group for obj in context.selected_objects if obj.vlmSettings.render_group >= 0))) == 1

    def execute(self, context):
        context.scene.vlmSettings.render_group_select = next((obj.vlmSettings.render_group for obj in context.selected_objects if obj.vlmSettings.render_group >= 0))
        return {"FINISHED"}


class VLM_OT_select_nestmap_group(Operator):
    bl_idname = "vlm.select_nestmap_group"
    bl_label = "Select Nestmap"
    bl_description = "Select all object from this nestmap"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return next((obj for obj in context.selected_objects if obj.vlmSettings.bake_nestmap >= 0), None) is not None

    def execute(self, context):
        for obj in [obj for obj in context.selected_objects if obj.vlmSettings.bake_nestmap >= 0]:
            for other in context.view_layer.objects:
                if other.vlmSettings.bake_nestmap == obj.vlmSettings.bake_nestmap:
                    other.select_set(True)
        return {"FINISHED"}


class VLM_OT_table_uv(Operator):
    bl_idname = "vlm.set_table_uv"
    bl_label = "Project Table UV"
    bl_description = "Set UV to table coordinates"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        l, t, w, h = context.scene.vlmSettings.playfield_size
        for obj in context.selected_objects:
            uv_layer = obj.data.uv_layers.active
            for loop in obj.data.loops:
                pt = obj.matrix_world @ mathutils.Vector(obj.data.vertices[loop.vertex_index].co)
                uv_layer.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
        return {"FINISHED"}


class VLM_OT_apply_aoi(Operator):
    bl_idname = "vlm.apply_aoi"
    bl_label = "Apply AOI"
    bl_description = "Setup area of influence of selected objects"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
        return camera_object is not None and context.selected_objects

    def execute(self, context):
        camera = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
        influence = None
        for light in context.selected_objects:
            light_influence = vlm_render_baker.get_light_influence(context.scene, context.view_layer.depsgraph, camera, light, None)
            if light_influence:
                if influence:
                    min_x, max_x, min_y, max_y = influence
                    min_x2, max_x2, min_y2, max_y2 = light_influence
                    influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                else:
                    influence = light_influence
        if influence:
            min_x, max_x, min_y, max_y = influence
            context.scene.render.border_min_x = min_x
            context.scene.render.border_max_x = max_x
            context.scene.render.border_min_y = 1 - max_y
            context.scene.render.border_max_y = 1 - min_y
            context.scene.render.use_border = True
        else:
            context.scene.render.use_border = False
        return {"FINISHED"}


class VLM_OT_load_render_images(Operator):
    bl_idname = "vlm.load_render_images_operator"
    bl_label = "Load/Unload Renders"
    bl_description = "Load/Unload render images for preview"
    bl_options = {"REGISTER", "UNDO"}
    is_unload: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        if not result_col: return False
        return next((obj for obj in context.selected_objects if obj.name in result_col.all_objects), None) is not None

    def execute(self, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
        for obj in [o for o in context.selected_objects if o.name in result_col.all_objects]:
            paths = [f'{bakepath}{obj.vlmSettings.bake_lighting} - Group {i}.exr' for i,_ in enumerate(obj.data.materials)]
            images = [vlm_utils.image_by_path(path) for path in paths]
            all_loaded = all((not os.path.exists(bpy.path.abspath(path)) or im is not None for path, im in zip(paths, images)))
            if self.is_unload:
            #if all_loaded:
                for im in images:
                    if im != None and im.name != 'VLM.NoTex': bpy.data.images.remove(im)
            else:
                for path, mat in zip(paths, obj.data.materials):
                    _, im = vlm_utils.get_image_or_black(path)
                    mat.node_tree.nodes["BakeTex"].image = im
        return {"FINISHED"}


class VLM_PT_Importer(bpy.types.Panel):
    bl_label = "VPX Importer"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        vlmProps = context.scene.vlmSettings
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_new_from_vpx.bl_idname)
        row.operator(VLM_OT_update.bl_idname)
        layout.prop(vlmProps, "table_file")
        layout.prop(vlmProps, "units_mode")
        layout.prop(vlmProps, "light_size")
        layout.prop(vlmProps, "light_intensity")
        layout.separator()
        layout.prop(vlmProps, "process_plastics")
        layout.prop(vlmProps, "bevel_plastics")
        layout.separator()
        layout.prop(vlmProps, "process_inserts")
        layout.prop(vlmProps, "insert_size")
        layout.prop(vlmProps, "insert_intensity")
        layout.prop(vlmProps, "use_pf_translucency_map")


class VLM_PT_Camera(bpy.types.Panel):
    bl_label = "VPX Camera"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        vlmProps = context.scene.vlmSettings
        layout.prop(vlmProps, "layback_mode", expand=True)
        layout.prop(vlmProps, "camera_layback")
        layout.prop(vlmProps, "camera_inclination")


class VLM_PT_Lightmapper(bpy.types.Panel):
    bl_label = "VPX Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        vlmProps = context.scene.vlmSettings
        step = 0
        if vlmProps.last_bake_step == 'groups': step = 1
        if vlmProps.last_bake_step == 'renders': step = 2
        if vlmProps.last_bake_step == 'meshes': step = 3
        if vlmProps.last_bake_step == 'nestmaps': step = 4
        layout.prop(vlmProps, "tex_size")
        layout.prop(vlmProps, "padding")
        layout.prop(vlmProps, "remove_backface", text='Backface')
        layout.prop(vlmProps, "keep_pf_reflection_faces")
        layout.prop(vlmProps, "export_mode")
        layout.prop(vlmProps, "playfield_col")
        layout.prop(vlmProps, "enable_vpx_reflection")
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_compute_render_groups.bl_idname, icon='GROUP_VERTEX', text='Groups')
        row.operator(VLM_OT_render_all_groups.bl_idname, icon='RENDER_RESULT', text='Renders', emboss=step>0)
        row.operator(VLM_OT_create_bake_meshes.bl_idname, icon='MESH_MONKEY', text='Meshes', emboss=step>1)
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_render_nestmaps.bl_idname, icon='TEXTURE_DATA', text='Nestmaps', emboss=step>2)
        row.operator(VLM_OT_export_vpx.bl_idname, icon='EXPORT', text='Export', emboss=step>3)
        row.operator(VLM_OT_batch_bake.bl_idname)


class VLM_PT_Col_Props(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "collection"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        col = context.collection
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
        if col.name in bake_col.children:
            layout.prop(col.vlmSettings, 'bake_mode')
            layout.prop(col.vlmSettings, 'is_active_mat', expand=True)
        elif col.name in light_col.children:
            layout.prop(col.vlmSettings, 'light_mode', expand=True)
            layout.prop(col.vlmSettings, 'world', expand=True)
        else:
            layout.label(text="Select a bake or light group") 


class VLM_PT_3D_Bake_Object(bpy.types.Panel):
    bl_label = "Bake Object"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        
        bake_objects = [obj for obj in context.selected_objects if obj.name not in result_col.all_objects]
        if bake_objects:
            if len(bake_objects) == 1:
                obj = bake_objects[0]
                layout.label(text="Link to VPX object:")
                layout.prop(obj.vlmSettings, 'vpx_object', text='VPX', expand=True)
                layout.prop(obj.vlmSettings, 'vpx_subpart', text='Subpart', expand=True)
                if light_col and obj.name in light_col.all_objects:
                    layout.prop(obj.vlmSettings, 'is_rgb_led', expand=True)
                    layout.prop(obj.vlmSettings, 'enable_aoi', expand=True)
                if vlm_utils.is_part_of_bake_category(obj, 'movable'):
                    layout.prop(obj.vlmSettings, 'movable_influence', expand=True)
                layout.separator()
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
            if all((x.vlmSettings.indirect_only for x in bake_objects)):
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Indirect Only', icon='INDIRECT_ONLY_ON').indirect_only = False
            elif all((not x.vlmSettings.indirect_only for x in bake_objects)):
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Default render', icon='INDIRECT_ONLY_OFF').indirect_only = True
            else:
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Mixed', icon='REMOVE').indirect_only = True
            if len(bake_objects) == 1 and bake_col and bake_objects[0].name in bake_col.all_objects:
                layout.prop(bake_objects[0].vlmSettings, 'bake_to')
            layout.separator()
            if len(bake_objects) == 1:
                layout.prop(obj.vlmSettings, 'render_group', text='Render Group', expand=True)
            else:
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
                    layout.label(text=f"Render Group #{single_group}")
            row = layout.row(align=True)
            row.operator(VLM_OT_clear_render_group_cache.bl_idname)
            row.operator(VLM_OT_select_render_group.bl_idname)
            if len(bake_objects) == 1 and bake_objects[0].type == 'CAMERA':
                layout.separator()
                layout.operator(VLM_OT_export_pov.bl_idname)
            layout.separator()
            layout.operator(VLM_OT_table_uv.bl_idname)


class VLM_PT_3D_Bake_Result(bpy.types.Panel):
    bl_label = "Bake Result"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        if not result_col: return
        result_objects = [obj for obj in context.selected_objects if obj.name in result_col.all_objects]
        if result_objects:
            if len(result_objects) == 1:
                props = result_objects[0].vlmSettings
                layout.prop(props, 'bake_lighting')
                layout.prop(props, 'bake_objects')
                layout.prop(props, 'bake_sync_light')
                layout.prop(props, 'bake_sync_trans')
                layout.prop(props, 'bake_type')
                layout.prop(props, 'bake_hdr_range')
                layout.separator()
                layout.prop(props, 'bake_nestmap')
                layout.operator(VLM_OT_select_nestmap_group.bl_idname)
            has_loaded = False
            has_unloaded = False
            bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
            for obj in result_objects:
                paths = [f'{bakepath}{obj.vlmSettings.bake_lighting} - Group {i}.exr' for i,_ in enumerate(obj.data.materials)]
                images = [vlm_utils.image_by_path(path) for path in paths]
                all_loaded = all((not os.path.exists(bpy.path.abspath(path)) or im is not None for path, im in zip(paths, images)))
                if all_loaded:
                    has_loaded = True
                else:
                    has_unloaded = True
            if has_loaded:
                layout.operator(VLM_OT_load_render_images.bl_idname, text='Unload Renders', icon='RESTRICT_RENDER_ON').is_unload = True
            else:
                layout.operator(VLM_OT_load_render_images.bl_idname, text='Load Renders', icon='RESTRICT_RENDER_OFF').is_unload = False
            layout.operator(VLM_OT_export_bake.bl_idname, icon='EXPORT')


class VLM_PT_3D_Tools(bpy.types.Panel):
    bl_label = "Tools"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.scene.vlmSettings, 'render_group_select', expand=True, text='Select Group', icon='RESTRICT_RENDER_OFF')
        layout.operator(VLM_OT_select_indirect.bl_idname)
        layout.operator(VLM_OT_select_occluded.bl_idname)
        layout.separator()
        layout.operator(VLM_OT_apply_aoi.bl_idname)


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
                 f"   permissions. You will need to restart Blender."]
        for line in lines:
            layout.label(text=line)


class VLM_PT_Props_warning_panel(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

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
                 f"   permissions. You will need to restart Blender."]
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
    VLM_PT_Importer,
    VLM_PT_Camera,
    VLM_PT_Lightmapper,
    VLM_PT_Col_Props,
    VLM_PT_3D_Bake_Object,
    VLM_PT_3D_Bake_Result,
    VLM_PT_3D_Tools,
    VLM_OT_new_from_vpx,
    VLM_OT_update,
    VLM_OT_compute_render_groups,
    VLM_OT_render_all_groups,
    VLM_OT_create_bake_meshes,
    VLM_OT_render_nestmaps,
    VLM_OT_batch_bake,
    VLM_OT_state_import_mesh,
    VLM_OT_state_import_transform,
    VLM_OT_state_indirect_only,
    VLM_OT_clear_render_group_cache,
    VLM_OT_select_render_group,
    VLM_OT_select_nestmap_group,
    VLM_OT_select_indirect,
    VLM_OT_select_occluded,
    VLM_OT_apply_aoi,
    VLM_OT_table_uv,
    VLM_OT_load_render_images,
    VLM_OT_export_bake,
    VLM_OT_export_vpx,
    VLM_OT_export_pov,
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