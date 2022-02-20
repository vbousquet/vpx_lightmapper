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
    found = None
    for sub in root_layer_collection.children:
        if sub.collection == col:
            return sub
        found = find_layer_collection(sub, col)
        if found is not None:
            return found
    return found


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


def get_collection(name, create=True):
    context = bpy.context
    if name == 'ROOT':
        n, c = create_collection(context, "VPX Light Mapper", context.scene.collection, create)
        return c
    if name == 'SETUP':
        n, c = create_collection(context, "Setup", get_collection('ROOT', create), create)
        return c
    if name == 'TRASH':
        n, c = create_collection(context, "Trash", get_collection('ROOT', create), create)
        if n: find_layer_collection(context.view_layer.layer_collection, c).exclude = True
        return c
    if name == 'HIDDEN':
        n, c = create_collection(context, "Hidden", get_collection('ROOT', create), create)
        if n: find_layer_collection(context.view_layer.layer_collection, c).exclude = True
        return c
    if name == 'INDIRECT':
        n, c = create_collection(context, "Indirect", get_collection('ROOT', create), create)
        if n: find_layer_collection(context.view_layer.layer_collection, c).indirect_only = True
        return c
    if name == 'BAKE TARGET':
        n, c = create_collection(context, "Bake Target", get_collection('ROOT', create), create)
        if n: c.hide_render = True
        return c
    if name == 'LIGHTS':
        n, c = create_collection(context, "Light Groups", get_collection('ROOT', create), create)
        return c
    if name == 'WORLD':
        n, c = create_collection(context, "World", get_collection('LIGHTS', create), create)
        if n: c.vlmSettings.light_mode = 'world'
        return c
    if name == 'GI':
        n, c = create_collection(context, "GI", get_collection('LIGHTS', create), create)
        if n: c.vlmSettings.light_mode = 'group'
        return c
    if name == 'INSERTS':
        n, c = create_collection(context, "Inserts", get_collection('LIGHTS', create), create)
        if n: c.vlmSettings.light_mode = 'split'
        return c
    if name == 'FLASHERS':
        n, c = create_collection(context, "Flashers", get_collection('LIGHTS', create), create)
        if n: c.vlmSettings.light_mode = 'split'
        return c
    if name == 'BAKE':
        n, c = create_collection(context, "Bake Groups", get_collection('ROOT', create), create)
        return c
    if name == 'BAKE DEFAULT':
        n, c = create_collection(context, "Default", get_collection('BAKE', create), create)
        if n:
            c.vlmSettings.bake_mode = 'default'
        return c
    if name == 'BAKE ACTIVE':
        n, c = create_collection(context, "Active", get_collection('BAKE', create), create)
        if n: c.vlmSettings.bake_mode = 'default'
        return c
    if name == 'BAKE PLAYFIELD':
        n, c = create_collection(context, "Playfield", get_collection('BAKE', create), create)
        if n: c.vlmSettings.bake_mode = 'playfield_fv'
        return c
    if name == 'BAKE RESULT':
        n, c = create_collection(context, "Bake Result", get_collection('ROOT', create), create)
        return c
    if name == 'BAKETMP':
        n, c = create_collection(context, "Bake Temp", get_collection('ROOT', create), create)
        return c
    if name == 'LIGHTTMP':
        n, c = create_collection(context, "Light Temp", get_collection('ROOT', create), create)
        return c


def setup_collections():
    default_col_ids = ['ROOT', 'SETUP', 'HIDDEN', 'INDIRECT', 'BAKE TARGET', 'LIGHTS', 'WORLD', 'GI', 'INSERTS', 'FLASHERS', 'BAKE', 'BAKE DEFAULT', 'BAKE ACTIVE', 'BAKE PLAYFIELD']
    for id in default_col_ids:
        get_collection(id)
    get_collection('BAKE PLAYFIELD').vlmSettings.bake_mode = 'playfield'


def push_state(root_col=None):
    state = []
    if not root_col: root_col = get_collection('ROOT', False)
    if root_col:
        for col in root_col.children:
            lc = find_layer_collection(bpy.context.view_layer.layer_collection, col)
            state.append((col.name, col, lc.exclude, lc.indirect_only, col.hide_render, col.hide_viewport))
            state.extend(push_state(col))
    return state


def pop_state(state):
    for name, col, exclude, indirect_only, hide_render, hide_viewport in state:
        if name in bpy.data.collections:
            lc = find_layer_collection(bpy.context.view_layer.layer_collection, col)
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
    find_layer_collection(rlc, root_col).exclude = exclude
    for col in root_col.children:
        exclude_all(context, col, exclude)


def hide_all(context, root_col, exclude=True):
    rlc = context.view_layer.layer_collection
    find_layer_collection(rlc, root_col).hide_viewport = exclude
    for col in root_col.children:
        hide_all(context, col, exclude)
    