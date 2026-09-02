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


def find_layer_collection(root_layer_collection, col):
    """Find the LayerCollection wrapper for a datablock Collection.

    Blender's scene collection is represented by the root LayerCollection
    itself, so check the root before recursively searching its children.
    """
    if root_layer_collection is None or col is None:
        return None
    if root_layer_collection.collection == col:
        return root_layer_collection
    for sub in root_layer_collection.children:
        found = find_layer_collection(sub, col)
        if found is not None:
            return found
    return None


def get_layer_collection(scene, col):
    """Return a LayerCollection for *col* in the supplied scene.

    Always use the scene's own ViewLayer hierarchy.  This is important for
    temporary render scenes where bpy.context.view_layer may refer to another
    scene/view layer.
    """
    if scene is None or col is None:
        return None
    for view_layer in scene.view_layers:
        lc = find_layer_collection(view_layer.layer_collection, col)
        if lc is not None:
            return lc
    return None


def create_collection(context, name, parent, create = True):
    if name not in bpy.data.collections:
        if not create or parent is None:
            return False, None
        col = bpy.data.collections.new(name)
        parent.children.link(col)
        return True, col
    else:
        return False, bpy.data.collections[name]


def delete_collection(col):
    for sub_col in col.children:
        delete_collection(sub_col)
    for obj in col.objects:
        col.objects.unlink(obj)
    bpy.data.collections.remove(col)


def unlink_collection(col):
    for sub_col in bpy.data.collections:
        if col.name in sub_col.children:
            sub_col.children.unlink(col)


def get_collection(parent_col, col_name, create=True):
    if parent_col is None:
        return None
    if col_name in parent_col.children:
        return parent_col.children[col_name]
    if create:
        print(f'. Creating collection {col_name} in {parent_col.name}')
        new_col = bpy.data.collections.new(col_name)
        new_col.name = col_name # Force naming
        parent_col.children.link(new_col)
        return new_col
    return None


def push_state(root_col=None):
    state = []
    if not root_col: root_col = get_collection('ROOT', False)
    if root_col:
        for col in root_col.children:
            lc = get_layer_collection(bpy.context.scene, col)
            if lc is not None:
                state.append((col.name, col, lc.exclude, lc.indirect_only, col.hide_render, col.hide_viewport))
            state.extend(push_state(col))
    return state


def pop_state(state):
    for name, col, exclude, indirect_only, hide_render, hide_viewport in state:
        if name in bpy.data.collections:
            lc = get_layer_collection(bpy.context.scene, col)
            if lc is not None:
                lc.exclude = exclude
                lc.indirect_only = indirect_only
            col.hide_render = hide_render
            col.hide_viewport = hide_viewport


def unlink(obj):
    [col.objects.unlink(obj) for col in obj.users_collection]


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
    for obj in [o for o in objects]:
        initial_collections.append(move_to_col(obj, target_col))
    return initial_collections


def restore_all_col_links(saved_state):
    for state in saved_state:
        restore_col_links(state)


def exclude_all(context, root_col, exclude=True):
    rlc = context.view_layer.layer_collection
    root_view_layer = find_layer_collection(rlc, root_col) # May be None for scene collection
    if root_view_layer: root_view_layer.exclude = exclude
    for col in root_col.children:
        exclude_all(context, col, exclude)


def hide_all(context, root_col, exclude=True):
    rlc = context.view_layer.layer_collection
    root_view_layer = find_layer_collection(rlc, root_col)
    if root_view_layer is not None:
        root_view_layer.hide_viewport = exclude
    for col in root_col.children:
        hide_all(context, col, exclude)
    