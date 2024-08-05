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
    "version": (0, 0, 7),
    "blender": (3, 2, 0),
    "description": "Import/Export Visual Pinball X tables with automated light baking",
    "warning": "Requires installation of external dependencies",
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
if "vlm_occlusion" in locals():
    importlib.reload(vlm_occlusion)
else:
    from . import vlm_occlusion
if "vlm_camera" in locals():
    importlib.reload(vlm_camera)
else:
    from . import vlm_camera

logger = vlm_utils.logger

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
        context.scene.vlmSettings.playfield_width = scaling * context.scene.vlmSettings.playfield_width
        context.scene.vlmSettings.playfield_height = scaling * context.scene.vlmSettings.playfield_height
        context.scene.vlmSettings.active_scale = new_scale


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
    # Baker options
    force_open_console: BoolProperty(name="Console on bake", description="Force open a console on bake if not already present", default = True)
    batch_inc_group: BoolProperty(name="Perform Group", description="Perform Group step when batching", default = True)
    batch_shutdown: BoolProperty(name="Shutdown", description="Shutdown computer after batch", default = False)
    render_height: IntProperty(
        name="PF Render Height", description="Render height of the playfield used to define projective baking render size",
        default = 256, min = 256, max=8192, update=vlm_utils.update_render_size
    )
    render_ratio: IntProperty(name="Render Ratio", description="- For projective baking, this ratio is applied to the target render height.\n- For baking, this ratio is applied to the user defined bake size", default = 100, min = 5, max=100, subtype="PERCENTAGE", update=vlm_utils.update_render_size)
    padding: IntProperty(name="Padding", description="Padding between nested texture parts", default = 2, min = 0)
    remove_backface: FloatProperty(name="Backface Limit", description="Angle (degree) limit for backfacing geometry removal\n90 will disable backface removal, 0 is full backface removal", default = 0.0)
    keep_pf_reflection_faces: BoolProperty(name="Keep playfield reflection", description="Keep faces only visible through playfield reflection", default = False)
    max_lighting: IntProperty(name="Max Light.", description="Maximum number of lighting scenario rendered simultaneously at 4K (0 = no limit)", default = 0, min = 0)
    tex_size: EnumProperty(
        items=[
            ('256', '256', '256x256', '', 256),
            ('512', '512', '512x512', '', 512),
            ('1024', '1024', '1024x1024', '', 1024),
            ('2048', '2048', '2048x2048', '', 2048),
            ('4096', '4096', '4096x4096', '', 4096),
            ('8192', '8192', '8192x8192', '', 8192),
        ],
        name='Texture size', description="Size of the exported texture in which rendered/baked parts will be nested (Should be greater than render size to avoid too much splitting)",
        default='256'
    )
    # Exporter options
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
    export_prefix: StringProperty(name="Export prefix", description="A prefix that will be applied to the nestmaps and VLM layers.", default="")
    # Active table informations
    table_file: StringProperty(name="Table", description="Table filename", default="")
    playfield_width: FloatProperty(name="Playfield Width", description="Width of the playfield in inches", default = 1.0, update=vlm_utils.update_render_size)
    playfield_height: FloatProperty(name="Playfield Height", description="Height of the playfield in inches", default = 1.0, update=vlm_utils.update_render_size)
    active_scale: FloatProperty(name="Active scale", description="Scale of the active table", default = 1.0)
    # Tools
    render_group_select: IntProperty(name="Select Group", description="Select all objects from a render group", default = 0, min = 0, update=select_render_group)


class VLM_Collection_props(PropertyGroup):
    # Bake collection
    bake_mode: EnumProperty(
        items=[
            ('group', 'Merge', 'Merge all objects to a single mesh', '', 0),
            ('split', 'Separate', 'Bake each object to its own mesh', '', 1),
        ],
        name='Merge Mode',
        description='Define how objects in the bake collection are processed (individually or merged together).',
        default='group'
    )
    is_opaque: BoolProperty(name="Opaque", description="Wether this collection only contains opaque objects which do not require blending", default = True)
    use_static_rendering: BoolProperty(name="Static Rendering", description="Mark this baked part to be statically pre-rendered in VPX", default = True)
    depth_bias: IntProperty(name="Depth Bias", description="Depth Bias applied to the layer when exported to VPX. Set to 0 for playfield, Negative for layer above playfield, positive for layers under playfield.", default = 0)
    refraction_probe: StringProperty(name="Refraction Probe", description="Identifier of the refraction probe to be used on export", default = '')
    refraction_thickness: FloatProperty(name="Refraction Thickness", description="Thickness of refraction", default = 10.0)
    reflection_probe: StringProperty(name="Reflection Probe", description="Identifier of the reflection probe to be used on export", default = '')
    reflection_strength: FloatProperty(name="Reflection Strength", description="Strength of reflection", default = 0.3)
    vpx_material: StringProperty(name="VPX Material", description="Name of a material to be used when exporting this collection instead of the default ones", default = '')
    # Light scenario collection
    light_mode: EnumProperty(
        items=[
            ('solid', 'Solid', 'Bakemap: Bake all lights in this collection as a single lighting scenario on which others are applied', '', 0),
            ('group', 'Group', 'Lightmap: Bake all lights in this collection as a single lighting scenario to a lightmap', '', 1),
            ('split', 'Split', 'Lightmap: Bake each light as a separate lighting scenario to a lightmap', '', 2)
        ],
        name='Light Mode',
        description='Light mode for the selected collection',
        default='group'
    )
    world: PointerProperty(name="World", type=bpy.types.World, description="World lighting to be used (should be empty for playfield lights)")


class VLM_Object_props(PropertyGroup):
    # Bake objects properties (for all objects except the one in the result collection)
    vpx_object: StringProperty(name="VPX", description="Identifier of reference VPX object", default = '')
    vpx_subpart: StringProperty(name="Part", description="Sub part identifier for multi part object like bumpers,...", default = '')
    import_mesh: BoolProperty(name="Mesh", description="Update mesh on import", default = True)
    import_transform: BoolProperty(name="Transform", description="Update transform on import", default = True)
    is_rgb_led: BoolProperty(name="RGB Led", description="RGB Led (lightmapped to white then colored in VPX for dynamic colors)", default = False)
    enable_aoi: BoolProperty(name="Enable AOI", description="Area Of Influence rendering optimization", default = True)
    bake_to: PointerProperty(name="Bake To", type=bpy.types.Object, description="Target object used as bake mesh target")
    bake_mask: PointerProperty(name="Bake Mask", type=bpy.types.Object, description="Object to be rendered with this object (for example for alpha layers)")
    indirect_only: BoolProperty(name="Indirect", description="Do not bake this object but consider its influence on the scene", default = False)
    hide_from_others: BoolProperty(name="Hide from others", description="Hide this object from other objects. For example hide flipper bat from playfield. WARNING: when using UV projected baking the part will only be hidden from parts in other render groups", default = False)
    render_group: IntProperty(name="Render Group", description="ID of group for batch rendering", default = -1)
    layback_offset: FloatProperty(name="Layback offset", description="Y offset caused by current layback", default = 0.0)
    bake_normalmap: BoolProperty(name="Normal Map", description="Bake a normal map", default = False)
    #bake_albedo: BoolProperty(name="Albedo", description="Bake an albedo map", default = False)
    #bake_orm: BoolProperty(name="O.R.M.", description="Bake an ORM map (Occlusion/Roughness/Metallic)", default = False)
    use_bake: BoolProperty(name="Use Bake", description="Use UV unwrapped camera baking instead of using camera renders with UV projected from camera. The object needs to be UV unwrapped. The process will be much slower but will produce better results", default = False)
    bake_width: IntProperty(name="Bake width:", description="Width of bake texture", default = 256, min = 2, max=8192)
    bake_height: IntProperty(name="Bake height:", description="Height of bake texture", default = 256, min = 2, max=8192)
    no_mesh_optimization: BoolProperty(name="No Optimization", description="Disable mesh optimization (for example to preserve normals or unwrapped UV)", default = False)
    # Both bake object and bake result
    is_movable: BoolProperty(name="Use as pivot", description="Use this part origin as the origin of the produced mesh", default = False)
    use_obj_pos: BoolProperty(name="Use Obj Pos", description="Use ObjRot instead of Rot when exporting", default = False)
    # Bake result properties (for object inside the bake result collection)
    bake_lighting: StringProperty(name="Lighting", description="Lighting scenario", default="")
    bake_collections: StringProperty(name="Bake", description="Bake collection that generated this bake/lightmap", default="")
    bake_sync_light: StringProperty(name="Sync Light", description="Object to sync light state on", default="")
    bake_sync_trans: StringProperty(name="Pivot", description="Pivot point if defined", default="")
    is_lightmap: BoolProperty(name="Lightmap", description="This baked part is a lightmap (additive bake to be applied over a base mesh)", default = False)
    bake_hdr_range: FloatProperty(name="HDR Range", description="HDR range of this bake", default=-1)
    bake_nestmap: IntProperty(name="Nestmap", description="ID of output nestmap (multiple bakes may share a nestmap)", default = -1)


class VLM_OT_new_from_vpx(Operator, ImportHelper):
    bl_idname = "vlm.new_from_vpx_operator"
    bl_label = "Import"
    bl_description = "Start a new VPX lightmap project"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".vpx"
    filter_glob: StringProperty(default="*.vpx", options={'HIDDEN'}, maxlen=255,)
    
    @classmethod
    def poll(cls, context):
        if context.blend_data.filepath == '': return False
        return True

    @classmethod
    def description(cls, context, properties):
        desc = "Start a new VPX lightmap project"
        if context.blend_data.filepath == '': desc = desc + "\n\nFile must be saved first"
        return desc
        
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.render.film_transparent = True
        context.scene.cycles.film_transparent_glass = True
        context.scene.vlmSettings.table_file = ""
        unit_update(self, context)
        return vlm_utils.run_with_logger(lambda : vlm_import.read_vpx(self, context, self.filepath))


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
        return vlm_utils.run_with_logger(lambda : vlm_import.read_vpx(self, context, bpy.path.abspath(context.scene.vlmSettings.table_file)))


class VLM_OT_select_occluded(Operator):
    bl_idname = "vlm.select_occluded_operator"
    bl_label = "Select Occluded Parts"
    bl_description = "Select objects that are occluded by other parts from the defined view point (lengthy operation)"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_occlusion.select_occluded(self, context))


class VLM_OT_select_indirect(Operator):
    bl_idname = "vlm.select_indirect_operator"
    bl_label = "Select Unbaked Parts"
    bl_description = "Select objects that are not baked/exported, and only affect rendering"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        for obj in context.scene.collection.all_objects:
            obj.select_set(obj.name in context.view_layer.objects and obj.vlmSettings.indirect_only)
        return {'FINISHED'}


class VLM_OT_select_baked(Operator):
    bl_idname = "vlm.select_baked_operator"
    bl_label = "Select UV Unwrapped Baked"
    bl_description = "Select objects that use UV Unwrapped baking"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        for obj in context.scene.collection.all_objects:
            obj.select_set(obj.name in context.view_layer.objects and obj.vlmSettings.use_bake)
        return {'FINISHED'}


class VLM_OT_compute_render_groups(Operator):
    bl_idname = "vlm.compute_render_groups_operator"
    bl_label = "1. Groups"
    bl_description = "Evaluate render groups"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        if context.blend_data.filepath == '': return False
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): return False
        if not context.scene.camera: return False
        return True

    @classmethod
    def description(cls, context, properties):
        desc = "Evaluate render groups"
        if context.blend_data.filepath == '': desc = desc + "\n\nFile must be saved first"
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): desc = desc + "\n\nVLM.Bake must be populated first"
        if not context.scene.camera: desc = desc + "\n\nAn active camera must be defined first"
        return desc
        
    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_group_baker.compute_render_groups(self, context))


class VLM_OT_render_all_groups(Operator):
    bl_idname = "vlm.render_all_groups_operator"
    bl_label = "2. Render"
    bl_description = "Render all groups for all lighting situation"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        if context.blend_data.filepath == '': return False
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False): return False
        if not context.scene.camera: return False
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        if not bake_col: return False
        for obj in bake_col.all_objects:
            if not obj.vlmSettings.indirect_only and not obj.vlmSettings.use_bake and obj.vlmSettings.render_group < 0: return False
        return True

    @classmethod
    def description(cls, context, properties):
        desc = "Render all groups for all lighting situation"
        if context.blend_data.filepath == '': desc = desc + "\n\nFile must be saved first"
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        if not bake_col:
            desc = desc + "\n\nVLM.Bake must be populated first"
        elif next((obj for obj in bake_col.all_objects if not obj.vlmSettings.indirect_only and not obj.vlmSettings.use_bake and obj.vlmSettings.render_group < 0), None) is not None:
            desc = desc + "\n\nRender groups must be evaluated first"
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False): desc = desc + "\n\nVLM.Lights must be populated first"
        if not context.scene.camera: desc = desc + "\n\nAn active camera must be defined first"
        return desc
        
    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_render_baker.render_all_groups(self, context))


class VLM_OT_create_bake_meshes(Operator):
    bl_idname = "vlm.create_bake_meshes_operator"
    bl_label = "3. Bake Meshes"
    bl_description = "Create all bake meshes for all lighting situation"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        if context.blend_data.filepath == '': return False
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): return False
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False): return False
        if not context.scene.camera: return False
        return True

    @classmethod
    def description(cls, context, properties):
        desc = "Create all bake meshes for all lighting situation"
        if context.blend_data.filepath == '': desc = desc + "\n\nFile must be saved first"
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): desc = desc + "\n\nVLM.Bake must be populated first"
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False): desc = desc + "\n\nVLM.Lights must be populated first"
        if not context.scene.camera: desc = desc + "\n\nAn active camera must be defined first"
        return desc
        
    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_meshes_baker.create_bake_meshes(self, context))


class VLM_OT_render_nestmaps(Operator):
    bl_idname = "vlm.render_nestmaps_operator"
    bl_label = "4. Nestmaps"
    bl_description = "Compute and render all nestmaps"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        return result_col is not None and len(result_col.all_objects) > 0

    @classmethod
    def description(cls, context, properties):
        desc = "Compute and render all nestmaps"
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        if result_col is None or len(result_col.all_objects) == 0:
            desc = desc + "\n\nYou must generate the baked meshes first"
        return desc

    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_nestmap_baker.render_nestmaps(self, context))


class VLM_OT_export_vpx(Operator):
    bl_idname = "vlm.export_vpx_operator"
    bl_label = "5. Export VPX"
    bl_description = "Export to an updated VPX table file"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        if context.blend_data.filepath == '': return False
        if not os.path.isfile(bpy.path.abspath(context.scene.vlmSettings.table_file)): return False
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): return False
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        if not result_col: return False
        for obj in result_col.all_objects:
            if obj.vlmSettings.bake_nestmap < 0: return False
        return True

    @classmethod
    def description(cls, context, properties):
        desc = "Export to an updated VPX table file"
        if context.blend_data.filepath == '': desc = desc + "\n\nFile must be saved first"
        if not os.path.isfile(bpy.path.abspath(context.scene.vlmSettings.table_file)): desc = desc + "\n\nVPX template file must be defined first"
        if not vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False): desc = desc + "\n\nVLM.Bake must be populated first"
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        if not result_col:
            desc = desc + "\n\nBaked meshes must be generated first"
        elif next((obj for obj in result_col.all_objects if obj.vlmSettings.bake_nestmap < 0), None) is not None:
            desc = desc + "\n\nNestmaps must be generated first"
        return desc
        
    def execute(self, context):
        return vlm_utils.run_with_logger(lambda : vlm_export.export_vpx(self, context))


class VLM_OT_batch_bake(Operator):
    bl_idname = "vlm.batch_bake_operator"
    bl_label = "Batch All"
    bl_description = "Performs all the bake steps in a batch, then export an updated VPX table (lengthy operation)"
    bl_options = {"REGISTER", "UNDO"}

    def do_shutdown(self, context, result):
        if context.scene.vlmSettings.batch_shutdown:
            vlm_utils.run_with_logger(lambda : logger.info('\n>> Shutting down'))
            os.system("shutdown /s /t 1")
        return result
    
    def spawn_console(self):
        # This will only spawn a console window if one does not already exist.
        try:
            import win32gui, bpy
        except:
            #ghetto guard for windows use only, as it will need completely different methods to query windows on other platforms
            return
            
        def get_window_titles():
            ret = []
            def winEnumHandler(hwnd, ctx):
                if win32gui.IsWindowVisible(hwnd):
                    txt = win32gui.GetWindowText(hwnd)
                    if txt:
                        ret.append((hwnd,txt))
            win32gui.EnumWindows(winEnumHandler, None)
            return ret

        all_titles = get_window_titles()
        window_ends = lambda title: [(hwnd,full_title) for (hwnd,full_title) in all_titles if full_title.endswith(title)]
        all_matching_windows = window_ends('blender.exe') # a slightly broad assumption that only the console window ends with blender.exr in the title but works
        if len(all_matching_windows) == 0:
            bpy.ops.wm.console_toggle()
            
    def execute(self, context):
        if context.scene.vlmSettings.force_open_console:
            self.spawn_console()
        start_time = time.time()
        vlm_utils.run_with_logger(lambda : logger.info(f"\nStarting complete bake batch..."))
        if context.scene.vlmSettings.batch_inc_group:
            result = vlm_utils.run_with_logger(lambda : vlm_group_baker.compute_render_groups(self, context))
            if 'FINISHED' not in result: return self.do_shutdown(context, result)
            bpy.ops.wm.save_mainfile()
        result = vlm_utils.run_with_logger(lambda : vlm_render_baker.render_all_groups(self, context))
        if 'FINISHED' not in result: return self.do_shutdown(context, result)
        bpy.ops.wm.save_mainfile()
        result = vlm_utils.run_with_logger(lambda : vlm_meshes_baker.create_bake_meshes(self, context))
        if 'FINISHED' not in result: return self.do_shutdown(context, result)
        bpy.ops.wm.save_mainfile()
        result = vlm_utils.run_with_logger(lambda : vlm_nestmap_baker.render_nestmaps(self, context))
        if 'FINISHED' not in result: return self.do_shutdown(context, result)
        bpy.ops.wm.save_mainfile()
        result = vlm_utils.run_with_logger(lambda : vlm_export.export_vpx(self, context))
        if 'FINISHED' not in result: return self.do_shutdown(context, result)
        bpy.ops.wm.save_mainfile()
        vlm_utils.run_with_logger(lambda : logger.info(f"\nBatch baking performed in {vlm_utils.format_time(time.time() - start_time)}"))
        return self.do_shutdown(context, result)


class VLM_OT_export_obj(Operator):
    bl_idname = "vlm.export_obj_operator"
    bl_label = "Export OBJ"
    bl_description = "Export object to a Wavefront OBJ file (with its nested texture for bakes)"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        #return next((obj for obj in context.selected_objects if obj.vlmSettings.bake_lighting != ''), None) is not None
        return next((obj for obj in context.selected_objects), None) is not None

    def execute(self, context):
        return vlm_export_obj.export_obj(self, context)


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


class VLM_OT_select_render_group(Operator):
    bl_idname = "vlm.select_render_group"
    bl_label = "Select"
    bl_description = "Select all object from this render group"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(set((obj.vlmSettings.render_group for obj in context.selected_objects if obj.vlmSettings.render_group >= 0))) == 1

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
        return context.mode == 'OBJECT' and next((obj for obj in context.selected_objects if obj.vlmSettings.bake_nestmap >= 0), None) is not None

    def execute(self, context):
        for obj in [obj for obj in context.selected_objects if obj.vlmSettings.bake_nestmap >= 0]:
            for other in context.view_layer.objects:
                if other.vlmSettings.bake_nestmap == obj.vlmSettings.bake_nestmap:
                    other.select_set(True)
        return {"FINISHED"}


class VLM_OT_table_uv(Operator):
    bl_idname = "vlm.set_table_uv"
    bl_label = "Add Table UV Project"
    bl_description = "Add a UV modifier adjusted to table coordinates"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(context.selected_objects) > 0

    def execute(self, context):
        w = context.scene.vlmSettings.playfield_width * (2.54 / 100.0)
        h = context.scene.vlmSettings.playfield_height * (2.54 / 100.0)
        o = bpy.data.objects.new("Table UV", None)
        context.scene.collection.objects.link(o)
        o.empty_display_type = 'ARROWS'
        o.empty_display_size = 0.1
        o.location = (0.5*w, -0.5*h, 0.0)
        o.scale = (0.5*w, 0.5*h, 1.0)
        for obj in context.selected_objects:
            uv_modifier = obj.modifiers.new('Table UV', 'UV_PROJECT')
            uv_modifier.uv_layer = 'UVMap'
            uv_modifier.projector_count = 1
            uv_modifier.projectors[0].object = o
            # uv_layer = obj.data.uv_layers.active
            # for loop in obj.data.loops:
                # pt = obj.matrix_world @ mathutils.Vector(obj.data.vertices[loop.vertex_index].co)
                # uv_layer.data[loop.index].uv = ((pt[0]-l) / w, (pt[1]-t+h) / h)
        return {"FINISHED"}


class VLM_OT_toggle_no_exp_modifier(Operator):
    bl_idname = "vlm.toggle_no_exp_modifier"
    bl_label = "Toogle NoExp"
    bl_description = "Toggle modifiers marked with NoExp of the current selection"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) > 0

    def execute(self, context):
        all_on = True
        for obj in context.selected_objects:
            for modifier in obj.modifiers:
                if 'NoExp' in modifier.name:
                    all_on = all_on and modifier.show_viewport
        for obj in context.selected_objects:
            for modifier in obj.modifiers:
                if 'NoExp' in modifier.name:
                    modifier.show_viewport = not all_on
        return {"FINISHED"}


class VLM_OT_apply_aoi(Operator):
    bl_idname = "vlm.apply_aoi"
    bl_label = "Apply AOI"
    bl_description = "Setup area of influence of selected objects"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return context.scene.camera and context.selected_objects

    def execute(self, context):
        influence = None
        for light in context.selected_objects:
            light_influence = vlm_render_baker.get_light_influence(context.scene, context.view_layer.depsgraph, context.scene.camera, light, None)
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


class VLM_OT_render_blueprint(Operator):
    bl_idname = "vlm.blueprint"
    bl_label = "Render Blueprint/Mask"
    bl_description = "Render a blueprint or a mask of the table\nRender object visible viewport in a fitted top-down view"
    bl_options = {"REGISTER", "UNDO"}
    height: IntProperty(
        name="Blueprint Height", description="Blueprint height (width will be computed from table size)", 
        default = 4096, min = 256, max=8192
    )
    solid: bpy.props.BoolProperty(name="Solid Blueprint", description="Render filled parts with solid black", default = False)
    
    def execute(self, context):
        vlm_utils.render_blueprint(context, self.height, self.solid);
        return {"FINISHED"}


class VLM_OT_fit_camera(Operator):
    bl_idname = "vlm.fitcamera"
    bl_label = "Fit Camera"
    bl_description = "Fit camera to parts to be baked"
    bl_options = {"REGISTER", "UNDO"}
    inclination: FloatProperty(name="Inclination", description="Camera inclination", default = 15.0)
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        if not context.scene.camera or not bake_col: return False
        return True

    def execute(self, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        context.scene.render.pixel_aspect_x = 1
        vlm_camera.fit_camera(context, context.scene.camera, self.inclination, 0.0, bake_col)
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
        for obj in [o for o in context.selected_objects if o.name in result_col.all_objects]:
            paths = [vlm_utils.get_packmap_bakepath(context, mat) for mat in obj.data.materials]
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


class VLM_OT_select_table_file(Operator, ImportHelper):
    bl_idname = "vlm.select_table_file"
    bl_label = "Select VPX table file"
    __doc__ = ""

    filter_glob = StringProperty(
        default="*.vpx", 
        options={'HIDDEN'}
    )

    def execute(self, context):
        context.scene.vlmSettings.table_file = bpy.path.relpath(self.filepath)
        return {'FINISHED'} 


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
        row = layout.row()
        row.prop(vlmProps, "table_file", expand=True)
        row = row.row()
        row.alignment = 'RIGHT'
        row.operator(VLM_OT_select_table_file.bl_idname, text='...')
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
        # Size & render size properties
        #layout.prop(vlmProps, "active_scale")
        layout.prop(vlmProps, "playfield_width")
        layout.prop(vlmProps, "playfield_height")
        # Render properties
        layout.prop(vlmProps, "render_height")
        layout.prop(vlmProps, "render_ratio")
        layout.separator()
        layout.prop(vlmProps, "max_lighting")
        layout.prop(vlmProps, "remove_backface", text='Backface')
        layout.prop(vlmProps, "keep_pf_reflection_faces")
        layout.separator()
        # Nest properties
        layout.prop(vlmProps, "tex_size")
        layout.prop(vlmProps, "padding")
        layout.separator()
        # Export properties
        layout.prop(vlmProps, "export_mode")
        layout.prop(vlmProps, "export_prefix")
        layout.separator()
        # Actions buttons
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_compute_render_groups.bl_idname, icon='GROUP_VERTEX', text='Groups')
        row.operator(VLM_OT_render_all_groups.bl_idname, icon='RENDER_RESULT', text='Renders')
        row.operator(VLM_OT_create_bake_meshes.bl_idname, icon='MESH_MONKEY', text='Meshes')
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_render_nestmaps.bl_idname, icon='TEXTURE_DATA', text='Nestmaps')
        row.operator(VLM_OT_export_vpx.bl_idname, icon='EXPORT', text='Export')
        row.operator(VLM_OT_batch_bake.bl_idname)
        row = layout.row()
        row.use_property_split = False
        row.alignment = 'CENTER'
        row.label(text='Batch:')
        row.prop(vlmProps, "force_open_console", expand=True)        
        row.prop(vlmProps, "batch_inc_group", expand=True)
        row.prop(vlmProps, "batch_shutdown", expand=True)


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
        if bake_col and col.name in bake_col.children:
            layout.prop(col.vlmSettings, 'bake_mode', expand=True)
            layout.prop(col.vlmSettings, 'vpx_material', expand=True)
            layout.prop(col.vlmSettings, 'is_opaque', expand=True)
            if col.vlmSettings.is_opaque:
                layout.prop(col.vlmSettings, 'use_static_rendering', expand=True)
            else:
                layout.prop(col.vlmSettings, 'depth_bias', expand=True)
                layout.prop(col.vlmSettings, 'refraction_probe', expand=True)
                layout.prop(col.vlmSettings, 'refraction_thickness', expand=True)
            layout.prop(col.vlmSettings, 'reflection_probe', expand=True)
            layout.prop(col.vlmSettings, 'reflection_strength', expand=True)
        elif light_col and col.name in light_col.children:
            layout.prop(col.vlmSettings, 'light_mode', expand=True)
            layout.prop(col.vlmSettings, 'world', expand=True)
        else:
            layout.label(text="Select a bake or light group") 


class VLM_PT_3D_VPX_Import(bpy.types.Panel):
    bl_label = "VPX Import"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    @classmethod
    def poll(cls, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        bake_objects = [obj for obj in context.selected_objects if result_col is None or obj.name not in result_col.all_objects]
        return bake_objects

    def draw(self, context):
        self.layout.use_property_split = True
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        bake_objects = [obj for obj in context.selected_objects if result_col is None or obj.name not in result_col.all_objects]
        row = self.layout.row(align=True)
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


class VLM_PT_3D_VPX_Object(bpy.types.Panel):
    bl_label = "VPX Object"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        return bake_col and context.active_object and context.active_object.name in bake_col.all_objects and len(context.selected_objects) == 1

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.prop(context.active_object.vlmSettings, 'vpx_object', text='Name', expand=True)
        self.layout.prop(context.active_object.vlmSettings, 'vpx_subpart', text='Subpart', expand=True)


class VLM_PT_3D_VPX_Light(bpy.types.Panel):
    bl_label = "VPX Light"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    @classmethod
    def poll(cls, context):
        light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
        return light_col and context.active_object and context.active_object.name in light_col.all_objects and len(context.selected_objects) == 1

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.prop(context.active_object.vlmSettings, 'vpx_object', text='Light', expand=True)
        self.layout.prop(context.active_object.vlmSettings, 'is_rgb_led', text='White Bake', expand=True)
        self.layout.prop(context.active_object.vlmSettings, 'enable_aoi', text='Enable AOI', expand=True)
        self.layout.operator(VLM_OT_apply_aoi.bl_idname)

from itertools import pairwise

class VLM_OT_calc_bake_size(Operator):
    bl_idname = "vlm.calc_bake_size"
    bl_label = "Fit Bake Size"
    bl_description = "Compute bake size based on selected UVMap edges and scene settings.\nIf 2 ortogonal UV edges are selected, aspect ratio of texture is computed. Otherwise, the size of the texture is computed, keeping the user defined aspect ratio.\n\nNote that you must select some UV edges, without using 'UV Sync Selection' which does not select UV edges"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        if context.object.mode != 'OBJECT': return False
        if not context.scene.camera: return False
        if 'UVMap' not in context.active_object.data.uv_layers: return False
        return True

    def execute(self, context):
        camera = context.scene.camera
        uv_unwrapped_layer = context.active_object.data.uv_layers['UVMap']
        uv_projected_layer = context.active_object.data.uv_layers.new()
        proj_ar = vlm_utils.get_render_proj_ar(context)
        render_size = vlm_utils.get_render_size(context)
        vlm_utils.project_uv(camera, context.active_object, proj_ar, uv_projected_layer)
        width = height = 0
        minimum_length_u = 0.0001
        # First try to fit separately x and y axis (needs to have 2 orthogonal UV edges selected)
        print('Trying to fit to user selected orthogonal UV edges:')
        for face in context.active_object.data.polygons:
            for loop_idx0, loop_idx1 in pairwise(list(face.loop_indices) + [face.loop_indices[0]]):
                if uv_unwrapped_layer.edge_selection[loop_idx0].value:
                    uv_u = uv_unwrapped_layer.uv[loop_idx1].vector - uv_unwrapped_layer.uv[loop_idx0].vector
                    l_u = uv_u.length
                    if l_u >= minimum_length_u:
                        uv_p = uv_projected_layer.uv[loop_idx1].vector - uv_projected_layer.uv[loop_idx0].vector
                        uv_p[0] *= render_size[0]
                        uv_p[1] *= render_size[1]
                        l_p = uv_p.length
                        if abs(uv_u.x) > 100 * abs(uv_u.y):
                            width = max(width, l_p / l_u)
                        elif abs(uv_u.y) > 100 * abs(uv_u.x):
                            height = max(height, l_p / l_u)
                        print(f'. {uv_unwrapped_layer.uv[loop_idx0].vector} {uv_unwrapped_layer.uv[loop_idx1].vector} => {l_u:.5f} {l_p:10.5f} {l_p / l_u:.5f} => {width:5.0f}x{height:5.0f}')
        # If failed, try to fit using the user defined aspect ratio
        if round(width) < 2 or round(height) < 2:
            print('Trying to fit to user selected UV edges, using user defined aspect ratio:')
            bake_ar = context.active_object.vlmSettings.bake_height / context.active_object.vlmSettings.bake_width
            for face in context.active_object.data.polygons:
                for loop_idx0, loop_idx1 in pairwise(list(face.loop_indices) + [face.loop_indices[0]]):
                    if uv_unwrapped_layer.edge_selection[loop_idx0].value:
                        uv_u = uv_unwrapped_layer.uv[loop_idx1].vector - uv_unwrapped_layer.uv[loop_idx0].vector
                        uv_u[1] = uv_u[1] * bake_ar
                        l_u = uv_u.length
                        if l_u >= minimum_length_u:
                            uv_p = uv_projected_layer.uv[loop_idx1].vector - uv_projected_layer.uv[loop_idx0].vector
                            uv_p[0] *= render_size[0]
                            uv_p[1] *= render_size[1]
                            l_p = uv_p.length
                            width = max(width, l_p / l_u)
                            height = bake_ar * width
                            print(f'. {uv_unwrapped_layer.uv[loop_idx0].vector} {uv_unwrapped_layer.uv[loop_idx1].vector} => {l_u:.5f} {l_p:10.5f} {l_p / l_u:.5f} => {width:5.0f}x{height:5.0f}')
        context.active_object.data.uv_layers.remove(uv_projected_layer)
        if round(width) < 2 or round(height) < 2:
            self.report({"ERROR"}, 'Failed to compute fitted bake size')
            return {"CANCELLED"}
        context.active_object.vlmSettings.bake_width = round(width)
        context.active_object.vlmSettings.bake_height = round(height)
        print(f'Bake size fitting succeeded. Result: {context.active_object.vlmSettings.bake_width}x{context.active_object.vlmSettings.bake_height}')
        return {"FINISHED"}


class VLM_PT_3D_Bake_Options(bpy.types.Panel):
    bl_label = "Bake Options"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        bake_objects = [obj for obj in context.selected_objects if bake_col and obj.name in bake_col.all_objects]
        return bake_objects

    def draw(self, context):
        layout = self.layout
        self.layout.use_property_split = True
        bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
        bake_objects = [obj for obj in context.selected_objects if bake_col and obj.name in bake_col.all_objects]
        if context.active_object in bake_objects and len(bake_objects) == 1:
            layout.prop(context.active_object.vlmSettings, 'indirect_only', text='Bake Part', invert_checkbox=True)
            if not context.active_object.vlmSettings.indirect_only:
                col = layout.column()
                col.enabled = context.active_object.vlmSettings.use_bake
                col.prop(context.active_object.vlmSettings, 'bake_normalmap', text='Bake Normal Map')
                col = layout.column()
                #col.enabled = False
                #col.prop(context.active_object.vlmSettings, 'bake_albedo', text = 'Bake Albedo Map')
                #col.prop(context.active_object.vlmSettings, 'bake_orm', text = 'Bake ORM Map')
                layout.prop(context.active_object.vlmSettings, 'hide_from_others', text='Hide from others')
                layout.prop(context.active_object.vlmSettings, 'bake_mask', text='Mask')
                layout.separator()
                layout.prop(context.active_object.vlmSettings, 'is_movable')
                col = layout.column()
                col.enabled = context.active_object.vlmSettings.is_movable
                col.prop(context.active_object.vlmSettings, 'use_obj_pos')
                layout.separator()
                layout.prop(context.active_object.vlmSettings, 'use_bake', toggle=1, text='Unwrapped Bake' if context.active_object.vlmSettings.use_bake else 'Camera Render')
                if context.active_object.vlmSettings.use_bake:
                    layout.prop(context.active_object.vlmSettings, 'bake_width', text='Width')
                    layout.prop(context.active_object.vlmSettings, 'bake_height', text='Height')
                    layout.operator(VLM_OT_calc_bake_size.bl_idname, text='Fit size to UV edges')
                else:
                    layout.prop(context.active_object.vlmSettings, 'no_mesh_optimization', text='Optimize mesh', invert_checkbox=True)
                    layout.prop(context.active_object.vlmSettings, 'bake_to')
                    col = layout.column()
                    col.enabled = False
                    #col.active = False # If we still want to allow editing (really need to know what you are doing: better to disable and force a render group evaluation)
                    col.prop(context.active_object.vlmSettings, 'render_group', text='Group')
        else:
            layout.label(text=f"{len(bake_objects)} parts selected")
            if all((x.vlmSettings.indirect_only for x in bake_objects)):
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Bake disabled', icon='INDIRECT_ONLY_ON').indirect_only = False
            elif all((not x.vlmSettings.indirect_only for x in bake_objects)):
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Bake enabled', icon='INDIRECT_ONLY_OFF').indirect_only = True
            else:
                layout.operator(VLM_OT_state_indirect_only.bl_idname, text='Bake mixed on/off', icon='REMOVE').indirect_only = True
            single_group = -5
            for obj in bake_objects:
                if obj.vlmSettings.use_bake:
                    if single_group == -5:
                        single_group = -4
                    elif single_group != -4:
                        single_group = -3
                        break
                elif single_group == -4:
                    single_group = -3
                    break
                elif single_group == -5:
                    single_group = obj.vlmSettings.render_group
                elif single_group != obj.vlmSettings.render_group:
                    single_group = -2
            if single_group == -4:
                layout.label(text="Unwrapped UV bake")
            elif single_group == -3:
                layout.label(text="Mixed Unwrapped/Projected UV")
            elif single_group == -2:
                layout.label(text="Multiple render groups")
            elif single_group == -1:
                layout.label(text="Undefined render groups")
            else:
                layout.label(text=f"Render Group #{single_group}")


class VLM_PT_3D_Bake_Result(bpy.types.Panel):
    bl_label = "Bake Result"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    
    @classmethod
    def poll(cls, context):
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        result_objects = [obj for obj in context.selected_objects if result_col and obj.name in result_col.all_objects]
        return result_objects

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        result_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Result', create=False)
        result_objects = [obj for obj in context.selected_objects if obj.name in result_col.all_objects]
        if len(result_objects) == 1:
            props = result_objects[0].vlmSettings
            col = layout.column()
            col.enabled = False
            col.prop(props, 'bake_collections')
            col.prop(props, 'bake_sync_trans')
            col.prop(props, 'is_lightmap')
            col.separator()
            col.prop(props, 'bake_lighting')
            col.prop(props, 'bake_sync_light')
            col.prop(props, 'bake_hdr_range')
            col.separator()
            col.prop(props, 'bake_nestmap')
            layout.operator(VLM_OT_select_nestmap_group.bl_idname)
        has_loaded = False
        has_unloaded = False
        bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
        for obj in result_objects:
            paths = [vlm_utils.get_packmap_bakepath(context, mat) for mat in obj.data.materials]
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


class VLM_PT_3D_Tools(bpy.types.Panel):
    bl_label = "Tools"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.scene.vlmSettings, 'render_group_select', expand=True, text='Select Group', icon='RESTRICT_RENDER_OFF')
        layout.operator(VLM_OT_select_baked.bl_idname)
        layout.operator(VLM_OT_select_indirect.bl_idname)
        layout.operator(VLM_OT_select_occluded.bl_idname)
        layout.separator()
        layout.operator(VLM_OT_toggle_no_exp_modifier.bl_idname)
        layout.separator()
        layout.operator(VLM_OT_table_uv.bl_idname)
        layout.separator()
        layout.operator(VLM_OT_render_blueprint.bl_idname)
        layout.separator()
        layout.operator(VLM_OT_export_obj.bl_idname, icon='EXPORT')
        layout.separator()
        layout.operator(VLM_OT_fit_camera.bl_idname, icon='CAMERA_DATA')


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
    VLM_PT_Lightmapper,
    VLM_PT_Col_Props,
    VLM_PT_3D_VPX_Import,
    VLM_PT_3D_VPX_Object,
    VLM_PT_3D_VPX_Light,
    VLM_PT_3D_Bake_Options,
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
    VLM_OT_select_render_group,
    VLM_OT_select_nestmap_group,
    VLM_OT_select_baked,
    VLM_OT_select_indirect,
    VLM_OT_select_occluded,
    VLM_OT_toggle_no_exp_modifier,
    VLM_OT_apply_aoi,
    VLM_OT_table_uv,
    VLM_OT_render_blueprint,
    VLM_OT_fit_camera,
    VLM_OT_load_render_images,
    VLM_OT_export_obj,
    VLM_OT_export_vpx,
    VLM_OT_select_table_file,
    VLM_OT_calc_bake_size
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
    registered_classes.clear()
    if dependencies_installed:
        del bpy.types.Scene.vlmSettings
        del bpy.types.Collection.vlmSettings
        del bpy.types.Object.vlmSettings


if __name__ == "__main__":
    register()
