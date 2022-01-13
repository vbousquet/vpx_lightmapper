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


def strip_vlm(name):
    if name.startswith('VLM.'):
        return name[4:]
    return name
    

