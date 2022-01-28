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
import time
from . import vlm_collections

# TODO support 'active' (i.e. non opaque bake) objects


def select_occluded(context):
    """
    Select occluded objects to help the user identify geometry not to be baked (move to indirect or hide)
    Algorithm is the following:
    - Save initial pass id and assign new unique pass id for each baked object
    - Render all opaque baked objects at target resolution (or lower) with cycle, sample = 1, output = object index, tag objects that are not occluded
    [not implemented: - For each render group, render transparent baked objects of the groupe with cycle, z mask with opaque render using the composer, tag objects that are not occluded]
    - Select untagged objects
    - Restore initial pass id
    """
    print("\nStarting occlusion selection")
    start_time = time.time()
    col_state = vlm_collections.push_state()

    rlc = context.view_layer.layer_collection
    root_col = vlm_collections.get_collection('ROOT')
    root_bake_col = vlm_collections.get_collection('BAKE')
    bake_objects = []
    for col in root_col.children:
        vlm_collections.find_layer_collection(rlc, col).exclude = True
    for col in root_bake_col.children:
        if not col.vlmSettings.is_active_mat:
            vlm_collections.find_layer_collection(rlc, col).exclude = False
            bake_objects.extend(col.all_objects)
    initial_pass_ids = [o.pass_index for o in bake_objects]
    for i, o in enumerate(bake_objects, start=1):
        o.pass_index = i
        o.tag = True
    old_samples = context.scene.eevee.taa_render_samples
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    context.scene.render.engine = 'CYCLES'
    context.scene.render.resolution_y = 512 # Height used for the object masks
    context.scene.render.resolution_x = int(context.scene.render.resolution_y * render_aspect_ratio)
    context.scene.eevee.taa_render_samples = 1
    context.view_layer.use_pass_combined = False
    context.view_layer.use_pass_combined = False
    context.view_layer.use_pass_z = False
    context.view_layer.use_pass_object_index = True
    context.scene.use_nodes = True
    context.scene.node_tree.nodes.clear()
    nodes = context.scene.node_tree.nodes
    links = context.scene.node_tree.links
    rl = nodes.new("CompositorNodeRLayers")
    rl.location.x = -200
    out = nodes.new("CompositorNodeComposite")
    out.location.x = 200
    vn = nodes.new("CompositorNodeViewer")
    vn.location.x = 200
    vn.location.y = -200
    links.new(rl.outputs[2], out.inputs[0])
    links.new(rl.outputs[2], vn.inputs[0])
    bpy.ops.render.render()
    pixels = bpy.data.images['Viewer Node'].pixels
    arr = [int(i) for i in pixels[::]]
    for i in arr[::4]:
        if i > 0: bake_objects[i - 1].tag = False
    bpy.ops.object.select_all(action='DESELECT')
    for obj in bake_objects:
        if obj.tag:
            context.view_layer.objects.active = obj
            obj.select_set(True)
    context.scene.node_tree.nodes.clear()
    context.scene.use_nodes = False
    context.scene.render.resolution_y = int(context.scene.vlmSettings.tex_size)
    context.scene.render.resolution_x = int(context.scene.render.resolution_y * render_aspect_ratio)
    context.view_layer.use_pass_combined = True
    context.view_layer.use_pass_object_index = False
    context.scene.eevee.taa_render_samples = old_samples
    for o, pass_id in zip(bake_objects, initial_pass_ids): o.pass_index = pass_id
    vlm_collections.pop_state(col_state)
    print(f"\nOcclusion selection performed in {int(time.time() - start_time)}s.")
    return {'FINISHED'}
    