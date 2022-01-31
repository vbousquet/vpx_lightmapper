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
import array
import os
import pathlib
import gpu
import math
import mathutils
import functools
from gpu_extras.presets import draw_texture_2d
from gpu_extras.batch import batch_for_shader

from . import vlm_utils
from . import vlm_collections


# TODO
# - Layback lattice breaks the split normals: we should rotate them accordingly when this is toggled


def camera_inclination_update(self, context):
    """Update bake camera position based on its inclination, in order to fit the following constraints:
    - look at the center of the playfield
    - view all baked objects
    - satisfy the target texture size on the vertical axis (height of the render)
    
    There are 3 way to take layback in account:
    - disable: no layback, just normal fitting.
    - deform: deform geometry (like in VPX) and perform normal fitting on the deformed geometry.
    - camera: take layback in account in camera orientation (for correct shading and face visibility), 
    and overscale image on the y axis (for bake texture density) corresponding to what it would have been
    if we have deformed the geometry (like in VPX)
    """
    lattice = bpy.data.objects.get('Layback')
    camera_object = context.scene.objects.get('Bake Camera') 
    setup_col = vlm_collections.get_collection('SETUP', create=False)
    root_col = vlm_collections.get_collection('ROOT', create=False)
    bake_col = vlm_collections.get_collection('BAKE', create=False)
    playfield_left, playfield_top, playfield_width, playfield_height = context.scene.vlmSettings.playfield_size
    if not root_col or not lattice or not setup_col or not camera_object or not bake_col:
        return
    
    # Adjust the camera
    layback_mode = context.scene.vlmSettings.layback_mode
    if layback_mode == 'disable':
        context.scene.render.pixel_aspect_x = 1
        camera_inclination = context.scene.vlmSettings.camera_inclination
        camera_layback = 0
        fit_camera(context, camera_inclination, camera_layback)
    elif layback_mode == 'deform':
        context.scene.render.pixel_aspect_x = 1
        camera_inclination = context.scene.vlmSettings.camera_inclination
        camera_layback = context.scene.vlmSettings.camera_layback
        fit_camera(context, camera_inclination, camera_layback)
    elif layback_mode == 'camera':
        context.scene.render.pixel_aspect_x = 1
        camera_inclination = context.scene.vlmSettings.camera_inclination +  context.scene.vlmSettings.camera_layback / 2
        camera_layback = 0
        fit_camera(context, context.scene.vlmSettings.camera_inclination, context.scene.vlmSettings.camera_layback)
        target_ar = context.scene.vlmSettings.render_aspect_ratio
        target_x = context.scene.render.resolution_x
        fit_camera(context, camera_inclination, camera_layback) # compute x scaling that correspond to the aspect ratio with layback, but using an adjusted camera inclination instead
        context.scene.render.pixel_aspect_x = context.scene.vlmSettings.render_aspect_ratio / target_ar
        context.scene.vlmSettings.render_aspect_ratio = target_ar
        context.scene.render.resolution_x = target_x
       
    # Update the layback lattice transform
    lattice.location = (playfield_left + 0.5 * playfield_width, 2.0, 2.0) #playfield_top - 0.5 * playfield_height, 2.0)
    layback_factor = -math.tan(math.radians(camera_layback) / 2)
    for obj in root_col.all_objects:
        if obj.type == 'MESH' or obj.type == 'CURVE':
            if layback_mode == 'deform':
                lattice_mod = obj.modifiers.get('Layback')
                if not lattice_mod:
                    lattice_mod = obj.modifiers.new('Layback', 'LATTICE')
                    lattice_mod.object = lattice
            else:
                lattice_mod = obj.modifiers.get('Layback')
                if lattice_mod: obj.modifiers.remove(lattice_mod)
        elif obj.type == 'LIGHT':
            new_lb = (lattice.location.z - obj.location.z) * layback_factor
            obj.location.y = obj.location.y - obj.vlmSettings.layback_offset + new_lb
            obj.vlmSettings.layback_offset = new_lb
    setup_exclude = vlm_collections.find_layer_collection(context.view_layer.layer_collection, setup_col).exclude
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, setup_col).exclude = False
    bpy.ops.object.select_all(action='DESELECT')
    playfield_left, playfield_top, playfield_width, playfield_height = context.scene.vlmSettings.playfield_size
    lattice.data = bpy.data.lattices.new('Layback')
    lattice.select_set(True)
    context.view_layer.objects.active = lattice
    bpy.ops.object.mode_set(mode = 'EDIT')
    bpy.ops.lattice.select_all(action='SELECT')
    bpy.ops.transform.shear(value=layback_factor, orient_axis='X', orient_axis_ortho='Y', orient_type='GLOBAL', orient_matrix=((0, 1, 0), (0, 0, 1), (1, 0, 0)), orient_matrix_type='VIEW', mirror=True, use_proportional_edit=False, proportional_edit_falloff='SMOOTH', proportional_size=1, use_proportional_connected=False, use_proportional_projected=False)
    bpy.ops.object.mode_set(mode = 'OBJECT')
    lattice.data.interpolation_type_u = 'KEY_LINEAR'
    lattice.data.interpolation_type_v = 'KEY_LINEAR'
    lattice.data.interpolation_type_w = 'KEY_LINEAR'
    lattice.scale[0] = 6
    lattice.scale[1] = 6
    lattice.scale[2] = 6
    vlm_collections.find_layer_collection(context.view_layer.layer_collection, setup_col).exclude = setup_exclude


def fit_camera(context, camera_inclination, camera_layback):
    camera_object = context.scene.objects.get('Bake Camera') 
    camera_fov = camera_object.data.angle
    bake_col = vlm_collections.get_collection('BAKE', create=False)
    playfield_left, playfield_top, playfield_width, playfield_height = context.scene.vlmSettings.playfield_size
    opt_tex_size = int(context.scene.vlmSettings.tex_size)
    layback = mathutils.Matrix()
    layback[0][2] -math.tan(math.radians(camera_layback) / 2)
    camera_angle = math.radians(camera_inclination)
    camera_object.rotation_euler = mathutils.Euler((camera_angle, 0.0, 0.0), 'XYZ')
    camera_object.data.shift_x = 0
    camera_object.data.shift_y = 0
    view_vector = mathutils.Vector((0, math.sin(camera_angle), -math.cos(camera_angle)))
    aspect_ratio = 1.0
    for i in range(3): # iterations since it depends on the aspect ratio fitting which change after each computation
        # Compute the camera distance with the current aspect ratio
        camera_object.location = (playfield_left + 0.5 * playfield_width, -playfield_top -0.5 * playfield_height, 0)
        modelview_matrix = camera_object.matrix_basis.inverted()
        s = 1.0 / math.tan(camera_fov/2.0)
        sx = s if aspect_ratio > 1.0 else s/aspect_ratio
        sy = s if aspect_ratio < 1.0 else s*aspect_ratio
        min_dist = 0
        for obj in bake_col.all_objects:
            if obj.type == 'MESH':
                bbox_corners = [modelview_matrix @ obj.matrix_world @ layback @ mathutils.Vector(corner) for corner in obj.bound_box]
                proj_x = map(lambda a: abs(sx * a.x + a.z), bbox_corners)
                proj_y = map(lambda a: abs(sy * a.y + a.z), bbox_corners)
                min_dist = max(min_dist, max(proj_x), max(proj_y))
        camera_object.location.y -= min_dist * view_vector.y
        camera_object.location.z -= min_dist * view_vector.z
        # adjust aspect ratio and compute camera shift to fill the render output
        modelview_matrix = camera_object.matrix_basis.inverted()
        projection_matrix = camera_object.calc_matrix_camera(context.evaluated_depsgraph_get())
        max_x = max_y = min_x = min_y = 0
        for obj in bake_col.all_objects:
            if obj.type == 'MESH':
                bbox_corners = [projection_matrix @ modelview_matrix @ obj.matrix_world @ layback @ mathutils.Vector((corner[0], corner[1], corner[2], 1)) for corner in obj.bound_box]
                proj_x = [o for o in map(lambda a: a.x / a.w, bbox_corners)]
                proj_y = [o for o in map(lambda a: a.y / a.w, bbox_corners)]
                min_x = min(min_x, min(proj_x))
                min_y = min(min_y, min(proj_y))
                max_x = max(max_x, max(proj_x))
                max_y = max(max_y, max(proj_y))
        aspect_ratio = (max_x - min_x) / (max_y - min_y)
        context.scene.render.resolution_x = int(opt_tex_size * aspect_ratio)
        context.scene.render.resolution_y = opt_tex_size
        context.scene.vlmSettings.render_aspect_ratio = aspect_ratio
    # Center on render output
    camera_object.data.shift_x = 0.25 * (max_x + min_x)
    camera_object.data.shift_y = 0.25 * (max_y + min_y)
    
