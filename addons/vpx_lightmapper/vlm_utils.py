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

from . import vlm_collections


global_scale = 0.01


def push_color_grading(set_neutral):
    state = (bpy.context.scene.view_settings.view_transform, bpy.context.scene.view_settings.look)
    if set_neutral:
        bpy.context.scene.view_settings.view_transform = 'Standard'
        bpy.context.scene.view_settings.look = 'None'
    return state


def pop_color_grading(state):
    bpy.context.scene.view_settings.view_transform = state[0]
    bpy.context.scene.view_settings.look = state[1]


def apply_split_normals(me):
	# Write the blender internal smoothing as custom split vertex normals
	me.calc_normals_split()
	cl_nors = array.array('f', [0.0] * (len(me.loops) * 3))
	me.loops.foreach_get('normal', cl_nors)
	me.polygons.foreach_set('use_smooth', [False] * len(me.polygons))
	nors_split_set = tuple(zip(*(iter(cl_nors),) * 3))
	me.normals_split_custom_set(nors_split_set)
	# Enable the use custom split normals data
	me.use_auto_smooth = True


def get_bakepath(context, type='ROOT'):
    if type == 'RENDERS':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Renders/"
    elif type == 'MASKS':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Object Masks/"
    elif type == 'EXPORT':
        return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/Export/"
    return f"//{os.path.splitext(bpy.path.basename(context.blend_data.filepath))[0]} - Bakes/"


def strip_vlm(name):
    if name.startswith('VLM.'):
        return name[4:]
    return name
    

def image_by_path(path):
    for image in bpy.data.images:
        if image.filepath == path:
            return image
    return None


def mkpath(path):
    pathlib.Path(bpy.path.abspath(path)).mkdir(parents=True, exist_ok=True)


def is_same_light_color(objects, threshold):
    colors = [o.data.color for o in objects if o.type=='LIGHT']
    n_colors = len(colors)
    base_color = functools.reduce(lambda a, b: (a[0]+b[0], a[1]+b[1], a[2]+b[2]), colors)
    base_color = (base_color[0] / n_colors, base_color[1] / n_colors, base_color[2] / n_colors)
    # maybe use intensity distance instead of raw RGB distance ?
    max_dif = max(map(lambda a: mathutils.Vector((a[0] - base_color[0], a[1] - base_color[1], a[2] - base_color[2])).length_squared, colors))
    # colors are similar enough to be considered as a single color situation
    return n_colors == len(objects) and max_dif < threshold * threshold


def camera_inclination_update(self, context):
    """Update bake camera position based on its inclination, in order to fit the following constraints:
    - look at the center of the playfield
    - view all baked objects
    - satisfy the target texture size on the vertical axis (height of the render)
    """
    lattice = bpy.data.objects.get('Layback')
    camera_object = context.scene.objects.get('Bake Camera') 
    setup_col = vlm_collections.get_collection('SETUP', create=False)
    root_col = vlm_collections.get_collection('ROOT', create=False)
    bake_col = vlm_collections.get_collection('BAKE', create=False)
    if not root_col or not lattice or not setup_col or not camera_object or not bake_col:
        return
    
    camera_fov = camera_object.data.angle
    camera_inclination = context.scene.vlmSettings.camera_inclination
    camera_layback = context.scene.vlmSettings.camera_layback if context.scene.vlmSettings.enable_layback else 0
    playfield_left, playfield_top, playfield_width, playfield_height = context.scene.vlmSettings.playfield_size
    opt_tex_size = int(context.scene.vlmSettings.tex_size)

    # Update the layback lattice transform
    lattice.location = (playfield_left + 0.5 * playfield_width, 2.0, 2.0) #playfield_top - 0.5 * playfield_height, 2.0)
    layback_factor = -math.tan(math.radians(camera_layback) / 2)
    for obj in root_col.all_objects:
        if obj.type == 'LIGHT':
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
    
    layback = mathutils.Matrix()
    layback[0][2] -math.tan(math.radians(camera_layback) / 2)
    camera_angle = math.radians(camera_inclination)
    camera_object.rotation_euler = mathutils.Euler((camera_angle, 0.0, 0.0), 'XYZ')
    camera_object.data.shift_x = 0
    camera_object.data.shift_y = 0
    view_vector = mathutils.Vector((0, math.sin(camera_angle), -math.cos(camera_angle)))
    aspect_ratio = 1.0
    for i in range(3): # iterations since it depenfds on the aspect ratio fitting which change after each computation
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


def render_mask(context, width, height, target_image, view_matrix, projection_matrix):
    """Uses Blender's internal renderer to render the active scene as an opacity mask
    to the given image (not saved)
    """
    offscreen = gpu.types.GPUOffScreen(width, height)
    area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    space = area.spaces.active
    state = [
        space.overlay.show_floor,
        space.overlay.show_overlays,
        space.shading.background_type,
        space.shading.background_color,
        space.shading.light,
        space.shading.color_type,
        space.shading.single_color,
        space.shading.type,
        space.shading.render_pass
    ]
    space.overlay.show_floor = False
    space.overlay.show_overlays = False
    space.shading.background_type = 'VIEWPORT'
    space.shading.background_color = (0,0,0)
    space.shading.light = 'FLAT'
    space.shading.color_type = 'SINGLE'
    space.shading.single_color = (1,0, 0)
    space.shading.type = 'SOLID'
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.0, 0.0, 0.0, 0.0))
        offscreen.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            area.regions[-1],
            view_matrix,
            projection_matrix,
            do_color_management=False)
        vertex_shader = '''
            in vec2 position;
            in vec2 uv;
            out vec2 uvInterp;
            void main() {
                uvInterp = uv;
                gl_Position = vec4(position, 0.0, 1.0);
            }
        '''
        bw_fragment_shader = '''
            uniform sampler2D image;
            in vec2 uvInterp;
            out vec4 FragColor;
            void main() {
                vec4 t = texture(image, uvInterp).rgba;
                FragColor = vec4(0.0, 0.0, 0.0, 2.1 * t.r);
            }
        '''
        bw_shader = gpu.types.GPUShader(vertex_shader, bw_fragment_shader)
        bw_shader.bind()
        bw_shader.uniform_sampler("image", offscreen.texture_color)
        batch_for_shader(
            bw_shader, 'TRI_FAN',
            {
                "position": ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                "uv": ((0, 0), (1, 0), (1, 1), (0, 1)),
            },
        ).draw(bw_shader)
        buffer = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'UBYTE')
    offscreen.free()
    space.overlay.show_floor = state[0]
    space.overlay.show_overlays = state[1]
    space.shading.background_type = state[2]
    space.shading.background_color = state[3]
    space.shading.light = state[4]
    if state[5] != '':
        space.shading.color_type = state[5]
    space.shading.single_color = state[6]
    space.shading.type = state[7]
    
    if not target_image in bpy.data.images:
        bpy.data.images.new(target_image, width, height)
    image = bpy.data.images[target_image]
    image.scale(width, height)
    buffer.dimensions = width * height * 4
    image.pixels = [v / 255 for v in buffer]
    
    return image