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
from . import vlm_utils


def select_occluded(op, context):
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

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    start_time = time.time()
    opt_mask_size = 512
    
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    scene.render.engine = 'CYCLES'
    scene.render.resolution_y = opt_mask_size
    scene.render.resolution_x = int(opt_mask_size * context.scene.vlmSettings.render_aspect_ratio)
    scene.render.pixel_aspect_x = context.scene.render.pixel_aspect_x
    scene.cycles.device = 'GPU'
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.world = None #bpy.data.worlds["VPX.Env.Black"]
    scene.use_nodes = True
    scene.view_layers[0].use_pass_combined = False
    scene.view_layers[0].use_pass_z = False
    scene.view_layers[0].use_pass_object_index = True
    
    nodes = scene.node_tree.nodes
    links = scene.node_tree.links
    nodes.clear()
    links.clear()
    rl = nodes.new("CompositorNodeRLayers")
    rl.scene = scene
    rl.location.x = -200
    out = nodes.new("CompositorNodeComposite")
    out.location.x = 200
    vn = nodes.new("CompositorNodeViewer")
    vn.location.x = 200
    vn.location.y = -200
    links.new(rl.outputs[2], out.inputs[0])
    links.new(rl.outputs[2], vn.inputs[0])

    bake_objects = []
    for col in bake_col.children:
        if not col.vlmSettings.is_active_mat and col.vlmSettings.bake_mode == 'default': # playfield and movable are not considered as occluder
            bake_objects.extend(col.all_objects)
    initial_pass_ids = [o.pass_index for o in bake_objects]
    for i, o in enumerate(bake_objects, start=1):
        scene.collection.objects.link(o)
        o.pass_index = i
        o.tag = True

    print(". Rendering scene")
    bpy.ops.render.render(scene=scene.name)
    
    print(". Evaluating occluded objects")
    pixels = bpy.data.images['Viewer Node'].pixels
    arr = [int(i) for i in pixels[::]]
    for i in arr[::4]:
        if i > 0: bake_objects[i - 1].tag = False
    bpy.ops.object.select_all(action='DESELECT')
    for obj in bake_objects:
        if obj.tag:
            context.view_layer.objects.active = obj
            obj.select_set(True)
    for o, pass_id in zip(bake_objects, initial_pass_ids):
        scene.collection.objects.unlink(o)
        o.pass_index = pass_id
    
    print(f'Occlusion selection performed in {int(time.time() - start_time)}s.')
    bpy.data.scenes.remove(scene)
    return {'FINISHED'}
    