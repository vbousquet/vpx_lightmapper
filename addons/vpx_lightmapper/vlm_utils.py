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
import datetime
from gpu_extras.presets import draw_texture_2d
from gpu_extras.batch import batch_for_shader

from . import vlm_collections


def get_global_scale(context):
    if context.scene.vlmSettings.units_mode == 'vpx':
        return 0.01 # VPX units
    else:
        return 0.01 * 2.69875 / 50 # Metric scale (imperial is performed by blender itself)


def load_library():
    """Append core meshes (without linking them in order to dispose the unused ones after import)
    and core shader node groups (with fake user to avoid loosing them)
    """
    librarypath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VPXMeshes.blend")
    if not os.path.isfile(librarypath):
        print(f'{librarypath} does not exist')
    # Core materials, Compositing, Fixed view shading & miscellaneous nodes
    nodegroups = ('VLM.BakeInfo', 'VPX.Material', 'VPX.Flasher', 'VLM.Overlay', 'VLM.Fixed View Glass', 'VLM.Fixed View Specular', 'VLM.Fixed View Incoming', 'Metal Colors')
    with bpy.data.libraries.load(librarypath, link=False) as (data_from, data_to):
        data_to.objects = [name for name in data_from.objects if name.startswith("VPX.Core.")]
        data_to.images = [name for name in data_from.images if name.startswith("VPX.Core.")]
        data_to.materials = [name for name in data_from.materials if name.startswith("VPX.Core.Mat.")]
        data_to.node_groups = [name for name in nodegroups if not name in bpy.data.node_groups] # only import missing node groups since we add a fake user
    for nodegroup in nodegroups:
        bpy.data.node_groups[nodegroup].use_fake_user = True

    bpy.data.node_groups.get('VLM.BakeInfo').use_fake_user = True
    bpy.data.node_groups.get('VPX.Material').use_fake_user = True
    bpy.data.node_groups.get('VPX.Flasher').use_fake_user = True
    # Fixed view shading
    bpy.data.node_groups.get('VLM.Fixed View Glass').use_fake_user = True
    bpy.data.node_groups.get('VLM.Fixed View Specular').use_fake_user = True
    bpy.data.node_groups.get('VLM.Fixed View Incoming').use_fake_user = True
    # Compositing
    bpy.data.node_groups.get('VLM.Overlay').use_fake_user = True
    # Miscellaneous nodes
    bpy.data.node_groups.get('Metal Colors').use_fake_user = True
    bpy.data.node_groups.get('Color Dispersion').use_fake_user = True


def push_render_settings(set_raw):
    state = (bpy.context.scene.render.use_border, bpy.context.scene.render.use_crop_to_border,
                bpy.context.scene.render.border_min_x, bpy.context.scene.render.border_max_x,
                bpy.context.scene.render.border_min_y, bpy.context.scene.render.border_max_y,
                bpy.context.scene.view_settings.view_transform,
                bpy.context.scene.view_settings.look,
                bpy.context.scene.render.pixel_aspect_x, bpy.context.scene.render.pixel_aspect_y,
                bpy.context.scene.render.engine,
                bpy.context.scene.render.film_transparent,
                bpy.context.scene.eevee.taa_render_samples,
                bpy.context.scene.render.image_settings.file_format,
                bpy.context.scene.render.image_settings.color_mode,
                bpy.context.scene.render.image_settings.color_depth,
                bpy.context.scene.cycles.samples,
                bpy.context.scene.cycles.use_denoising,
                bpy.context.scene.render.image_settings.exr_codec,
                bpy.context.scene.render.bake.use_clear,
                bpy.context.scene.render.bake.use_selected_to_active,
                bpy.context.scene.use_nodes,
                bpy.context.scene.render.resolution_x,
                bpy.context.scene.render.resolution_y,
                bpy.context.view_layer.use_pass_combined,
                bpy.context.view_layer.use_pass_object_index,
                bpy.context.scene.render.bake.cage_extrusion,
                bpy.context.scene.view_settings.exposure,
                bpy.context.scene.view_settings.gamma,
                )
    if set_raw:
        bpy.context.scene.view_settings.view_transform = 'Raw'
        bpy.context.scene.view_settings.look = 'None'
    return state


def pop_render_settings(state):
    bpy.context.scene.render.use_border = state[0]
    bpy.context.scene.render.use_crop_to_border = state[1]
    bpy.context.scene.render.border_min_x = state[2]
    bpy.context.scene.render.border_max_x = state[3]
    bpy.context.scene.render.border_min_y = state[4]
    bpy.context.scene.render.border_max_y = state[5]
    bpy.context.scene.view_settings.view_transform = state[6]
    bpy.context.scene.view_settings.look = state[7]
    bpy.context.scene.render.pixel_aspect_x = state[8]
    bpy.context.scene.render.pixel_aspect_y = state[9]
    bpy.context.scene.render.engine = state[10]
    bpy.context.scene.render.film_transparent = state[11]
    bpy.context.scene.eevee.taa_render_samples = state[12]
    bpy.context.scene.render.image_settings.file_format = state[13]
    bpy.context.scene.render.image_settings.color_mode = state[14]
    bpy.context.scene.render.image_settings.color_depth = state[15]
    bpy.context.scene.cycles.samples = state[16]
    bpy.context.scene.cycles.use_denoising = state[17]
    bpy.context.scene.render.image_settings.exr_codec = state[18]
    bpy.context.scene.render.bake.use_clear = state[19]
    bpy.context.scene.render.bake.use_selected_to_active = state[20]
    bpy.context.scene.use_nodes = state[21]
    bpy.context.scene.render.resolution_x = state[22]
    bpy.context.scene.render.resolution_y = state[23]
    bpy.context.view_layer.use_pass_combined = state[24]
    bpy.context.view_layer.use_pass_object_index = state[25]
    bpy.context.scene.render.bake.cage_extrusion = state[26]
    bpy.context.scene.view_settings.exposure = state[27]
    bpy.context.scene.view_settings.gamma = state[28]


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


def set_selected_and_active(context, obj):
    bpy.ops.object.select_all(action='DESELECT')
    context.view_layer.objects.active = obj
    obj.select_set(True)


def strip_vlm(name):
    if name.startswith('VLM.'):
        return name[4:]
    return name
    

def format_time(length_in_seconds):
    return str(datetime.timedelta(seconds=length_in_seconds)).split('.')[0]


def image_by_path(path):
    for image in bpy.data.images:
        if image.filepath == path:
            return image
    return None


def get_image_or_black(path):
    existing = image_by_path(path)
    if existing:
        return ('existing', existing)
    elif os.path.exists(bpy.path.abspath(path)):
        return ('loaded', bpy.data.images.load(path, check_existing=False))
    else:
        black_image = bpy.data.images.get('VLM.NoTex')
        if not black_image:
            black_image = bpy.data.images.new('VLM.NoTex', 1, 1)
            black_image.generated_type = 'BLANK'
        return ('black', black_image)


def mkpath(path):
    pathlib.Path(bpy.path.abspath(path)).mkdir(parents=True, exist_ok=True)


def is_rgb_led(objects):
    n_objects = len(objects)
    if n_objects == 0 or not objects[0].vlmSettings.is_rgb_led:
        return False
    colors = [o.data.color for o in objects if o.type=='LIGHT']
    n_colors = len(colors)
    if n_colors != n_objects:
        print(f". Lights are marked as RGB Led but use colored emitter which are baked with their colors instead of full white (Lights: {[o.name for o in objects]}).")
        return True
    if n_objects == 1:
        return True
    base_color = functools.reduce(lambda a, b: (a[0]+b[0], a[1]+b[1], a[2]+b[2]), colors)
    base_color = (base_color[0] / n_colors, base_color[1] / n_colors, base_color[2] / n_colors)
    max_dif = max(map(lambda a: mathutils.Vector((a[0] - base_color[0], a[1] - base_color[1], a[2] - base_color[2])).length_squared, colors))
    threshold = 0.1
    if max_dif >= threshold * threshold:
        print(f". Lights are marked as RGB Led but use different colors (Lights: {[o.name for o in objects]}).")
    return True
        

def is_part_of_bake_category(obj, category):
    is_movable = True
    for col in obj.users_collection:
        if col.vlmSettings.bake_mode != category:
            is_movable = False
    return is_movable


def brightness_from_hdr(hdr_range):
    return 1.0 / max(0.1, min(2, hdr_range))
    #return 1.0 / min( 1 + (hdr_range - 1) / 10, 10)


def get_lightings(context):
    """Return the list of lighting situations to be rendered as dictionary of tuples
        (name, is lightmap, light collection, lights, custom data)
    """
    light_scenarios = {}
    lights_col = vlm_collections.get_collection('LIGHTS', create=False)
    if lights_col is None: return light_scenarios
    for light_col in (l for l in lights_col.children if not l.hide_render):
        lights = light_col.objects
        if light_col.vlmSettings.light_mode == 'solid': # Base solid bake
            light_scenarios['Solid'] = ['Solid', False, light_col, lights, None]
        elif light_col.vlmSettings.light_mode == 'group': # Lightmap of a group of lights
            light_scenarios[light_col.name] = [light_col.name, True, light_col, lights, None]
        elif light_col.vlmSettings.light_mode == 'split': # Lightmaps for multiple VPX lights
            for vpx_light in {l.vpx_object for l in lights}:
                light_group = [l for l in lights if l.vpx_object == vpx_light]
                name = f"{light_col.name}-{vpx_light.name}"
                light_scenarios[name] = [name, True, light_col, light_group, None]
    return light_scenarios


def get_n_lightings(context):
    return len(get_lightings(context))
    
    
def get_n_render_groups(context):
    n = 0
    root_bake_col = vlm_collections.get_collection('BAKE', create=False)
    if root_bake_col is not None:
        for obj in root_bake_col.all_objects:
            n = max(n, obj.vlmSettings.render_group + 1)
    return n


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
    
    target_image.scale(width, height)
    buffer.dimensions = width * height * 4
    target_image.pixels = [v / 255 for v in buffer]
