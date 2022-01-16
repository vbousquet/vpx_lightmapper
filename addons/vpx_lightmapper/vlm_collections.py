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


collection_ids = ['ROOT', 'TRASH', 'HIDDEN', 'INDIRECT', 'OVERLAY', 'LIGHTS', 'GI LIGHTS', 'BAKE', 'BAKE DEFAULT', 'BAKE PLAYFIELD', 'BAKE RESULT']


def find_layer_collection(root_layer_collection, col):
    found = None
    for sub in root_layer_collection.children:
        if sub.collection == col:
            return sub
        found = find_layer_collection(sub, col)
        if found is not None:
            return found
    return found


def create_collection(context, name, parent, excluded, create = True, indirect = False):
    if name not in bpy.data.collections:
        if not create or parent is None:
            return None
        col = bpy.data.collections.new(name)
        parent.children.link(col)
        lc = find_layer_collection(context.view_layer.layer_collection, col)
        lc.exclude = excluded
        lc.indirect_only = indirect
    else:
        col = bpy.data.collections[name]
    return col


def delete_collection(col):
    for sub_col in col.children:
        delete_collection(sub_col)
    for obj in col.objects:
        col.objects.unlink(obj)
    bpy.data.collections.remove(col)


def get_collection(name, create=True):
    context = bpy.context
    if name == 'ROOT':
        return create_collection(context, "VPX Light Mapper", context.scene.collection, False, create)
    if name == 'TRASH':
        return create_collection(context, "VLM.Trash", get_collection('ROOT', create), True, create)
    if name == 'HIDDEN':
        return create_collection(context, "VLM.Hidden", get_collection('ROOT', create), True, create)
    if name == 'INDIRECT':
        return create_collection(context, "VLM.Indirect", get_collection('ROOT', create), False, create, True)
    if name == 'OVERLAY':
        return create_collection(context, "VLM.Overlays", get_collection('ROOT', create), False, create)
    if name == 'LIGHTS':
        return create_collection(context, "VLM.Light Groups", get_collection('ROOT', create), False, create)
    if name == 'GI LIGHTS':
        return create_collection(context, "VLM.GI Lights", get_collection('LIGHTS', create), False, create)
    if name == 'PLAYFIELD LIGHTS':
        return create_collection(context, "VLM.Inserts", get_collection('LIGHTS', create), False, create)
    if name == 'BAKE':
        return create_collection(context, "VLM.Bake Groups", get_collection('ROOT', create), False, create)
    if name == 'BAKE DEFAULT':
        return create_collection(context, "VLM.Default", get_collection('BAKE', create), False, create)
    if name == 'BAKE PLAYFIELD':
        return create_collection(context, "VLM.Playfield", get_collection('BAKE', create), False, create)
    if name == 'BAKE RESULT':
        return create_collection(context, "VLM.Bake Result", get_collection('ROOT', create), False, create)
    if name == 'BAKETMP':
        return create_collection(context, "VLM.Bake Temp", get_collection('ROOT', create), False, create)


def setup_collections():
    for id in collection_ids:
        get_collection(id)
    get_collection('BAKE PLAYFIELD').vlmSettings.bake_mode = 'playfield'


def push_state():
    state = []
    for id in collection_ids:
        col = get_collection(id)
        exc = find_layer_collection(bpy.context.view_layer.layer_collection, col).exclude
        state.append((exc, col.hide_render, col.hide_viewport))
    return state


def pop_state(state):
    for id, s in zip(collection_ids, state):
        col = get_collection(id)
        find_layer_collection(bpy.context.view_layer.layer_collection, col).exclude = s[0]
        col.hide_render = s[1]
        col.hide_viewport = s[2]


def move_to_col(obj, target_col):
    initial_collections = [col for col in obj.users_collection]
    [col.objects.unlink(obj) for col in initial_collections]
    target_col.objects.link(obj)
    return (obj, initial_collections)
    
    
def restore_col_links(saved_state):
    [col.objects.unlink(saved_state[0]) for col in saved_state[0].users_collection]
    [col.objects.link(saved_state[0]) for col in saved_state[1]]


def move_all_to_col(objects, target_col):
    initial_collections = []
    for obj in objects:
        initial_collections.append(move_to_col(obj, target_col))
    return initial_collections


def restore_all_col_links(saved_state):
    for state in saved_state:
        restore_col_links(state)

