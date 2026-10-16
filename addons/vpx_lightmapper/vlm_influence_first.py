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

"""Optional influence first proxy render (scene['vlm_influence_first'] = True, off by default).

VLM's per bake influence image is an EEVEE render of the finished bake, so whether an (unwrapped object, light scenario)
bake contributes anything is only known after paying for it. As a stand-in, before any unwrapped bake, one low
resolution Cycles camera render per set of non overlapping unwrapped objects, with one light group per insert scenario,
gives a proxy of every (object, insert scenario) influence. Pairs whose proxy stays under INFLUENCE_FIRST['factor'] x
lm_threshold over the object mask are not baked. A skipped pair writes no file, which is exactly what the meshes step
reads for a fully pruned pair. The objects are camera visible copies whose material is, for camera rays only,
Add(original, Transparent) so every layer adds up along the ray like the EEVEE influence render; back faces use a white
Translucent BSDF (a bake lights a face from its own side, an upper bound of the diffuse light there). Decisions are
cached in the renders folder, so a resumed render does not redo the proxy renders. On any error, the pairs not decided
yet are baked as usual.

Insert scenarios are the lightmap scenarios whose name starts with one of the comma separated prefixes of
scene['vlm_insert_scenarios'], or, when it is not set, the lightmap scenarios with a light under the playfield (z < 0).
"""

import bpy
import json
import os
import shutil
import time
import numpy as np
from PIL import Image # External dependency
from . import vlm_utils
from .vlm_compat52 import get_compositor_tree, set_compositing_enabled, configure_file_output_node_52

logger = vlm_utils.logger

UNDER_PLAYFIELD_Z = 0.0
# Proxy calibrated on a 2K test table: it reads ~2x under the real influence (median) and only missed near black 1-7 px pieces.
# All light groups in one render (up to 'chunk') beat more renders with fewer groups: the scene sync is the cost.
INFLUENCE_FIRST = {'factor': 0.1, 'res_pct': 50, 'spp': 1024, 'chunk': 96}
INFLUENCE_FIRST_CACHE = 'Influence First.json'

_run = {}


def begin_run(scene):
    """Read the options from the custom properties of the scene being baked and reset the per run state.
    With 'vlm_influence_first' unset or False, nothing is done and skip_pair always returns False."""
    _run.clear()
    if not bool(scene.get('vlm_influence_first', False)):
        return
    _run.update(
        prefixes=tuple(p.strip() for p in str(scene.get('vlm_insert_scenarios', '')).split(',') if p.strip()),
        proxy_skips=set(),
        proxy_stats={'candidates': 0, 'skipped': 0, 'cached_skips': 0, 'renders': 0, 'seconds': 0.0})
    selection = ', '.join(_run['prefixes']) if _run['prefixes'] else 'lightmap scenarios with a light under the playfield'
    logger.info(f'. Influence first: on, insert scenarios: {selection}')


def end_run():
    if _run:
        logger.info(f'. Influence first: {_run["proxy_stats"]}')
    _run.clear()


def skip_pair(obj, scenario):
    """True if the proxy render showed no influence of scenario on obj (the bake can be skipped)"""
    if not _run:
        return False
    # Cached proxy decisions only apply to scenarios that are insert scenarios for the current settings
    return (scenario[0], obj.name) in _run['proxy_skips'] and is_insert_scenario(scenario)


def is_insert_scenario(scenario):
    name, is_lightmap, light_col, lights = scenario
    if not is_lightmap or light_col.vlmSettings.world:
        return False
    if _run.get('prefixes'):
        return name.startswith(_run['prefixes'])
    return any(l.type == 'LIGHT' and l.matrix_world.translation.z < UNDER_PLAYFIELD_Z for l in lights)


def _proxy_material(mat, made):
    """Copy of mat that, for camera rays only, is Add(shading, Transparent), shading being the original on front faces
    and a white Translucent BSDF on back faces"""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return mat
    if mat.name in made:
        return made[mat.name]
    mat2 = made[mat.name] = mat.copy()
    nt = mat2.node_tree
    out = nt.get_output_node('CYCLES')
    if out is None or not out.inputs['Surface'].is_linked:
        return mat2
    src = out.inputs['Surface'].links[0].from_socket
    light_path, geometry, translucent = nt.nodes.new('ShaderNodeLightPath'), nt.nodes.new('ShaderNodeNewGeometry'), nt.nodes.new('ShaderNodeBsdfTranslucent')
    translucent.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    side, camera = nt.nodes.new('ShaderNodeMixShader'), nt.nodes.new('ShaderNodeMixShader')
    nt.links.new(geometry.outputs['Backfacing'], side.inputs[0])
    nt.links.new(src, side.inputs[1])
    nt.links.new(translucent.outputs[0], side.inputs[2])
    add, transparent = nt.nodes.new('ShaderNodeAddShader'), nt.nodes.new('ShaderNodeBsdfTransparent')
    nt.links.new(side.outputs[0], add.inputs[0])
    nt.links.new(transparent.outputs[0], add.inputs[1])
    nt.links.new(light_path.outputs['Is Camera Ray'], camera.inputs[0])
    nt.links.new(src, camera.inputs[1])
    nt.links.new(add.outputs[0], camera.inputs[2])
    nt.links.new(camera.outputs[0], out.inputs['Surface'])
    return mat2


def _object_mask(obj, mask_scene, mask_path, opt_mask_size, opt_mask_pad, opt_ar):
    """Padded object mask (bool array, row 0 = top) from the object mask cache, rendered if missing, or None"""
    path = bpy.path.abspath(f'{mask_path}{vlm_utils.clean_filename(obj.name)}.png')
    w, h = int(opt_mask_size * opt_ar), opt_mask_size
    if not os.path.exists(path) or Image.open(path).size != (w, h):
        prev = (mask_scene.render.resolution_x, mask_scene.render.resolution_y, mask_scene.render.filepath,
                mask_scene.render.image_settings.file_format, mask_scene.render.image_settings.color_mode)
        dup = obj.copy()
        try:
            mask_scene.render.resolution_x, mask_scene.render.resolution_y = w, h
            mask_scene.render.image_settings.file_format, mask_scene.render.image_settings.color_mode = 'PNG', 'RGBA'
            mask_scene.render.filepath = path
            mask_scene.collection.objects.link(dup)
            bpy.ops.render.render(write_still=True, scene=mask_scene.name)
        finally:
            bpy.data.objects.remove(dup)
            (mask_scene.render.resolution_x, mask_scene.render.resolution_y, mask_scene.render.filepath,
             mask_scene.render.image_settings.file_format, mask_scene.render.image_settings.color_mode) = prev
    if not os.path.exists(path):
        return None
    im = Image.open(path).convert('RGBA')
    for _ in range(opt_mask_pad):
        for d in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            im.alpha_composite(im, d)
    return np.asarray(im)[..., 3] > 0


def _link_like(collection, src, dst):
    """Link dst in collection with the light linking state of src (direct member state, INCLUDE for a nested one)"""
    direct = [o.name for o in collection.objects] # collection_objects is index-paired with objects (direct members)
    state = collection.collection_objects[direct.index(src.name)].light_linking.link_state if src.name in direct else 'INCLUDE'
    collection.objects.link(dst)
    collection.collection_objects[len(direct)].light_linking.link_state = state


def _light_disjoint_chunks(scenarios, size):
    """Split (name, scenario) items into chunks of at most size scenarios, never putting two scenarios sharing a light in
    the same chunk (a light has only one light group per render)"""
    chunks = []
    for name, scenario in scenarios:
        light_names = {l.name for l in scenario[3]}
        for chunk in chunks:
            if len(chunk['scenarios']) < size and not (chunk['lights'] & light_names):
                chunk['scenarios'][name] = scenario
                chunk['lights'] |= light_names
                break
        else:
            chunks.append(dict(scenarios={name: scenario}, lights=set(light_names)))
    return [chunk['scenarios'] for chunk in chunks]


def influence_first(scene, render_col, indirect_col, bake_col, light_scenarios, bakepath, mask_scene, mask_path, opt_mask_size, opt_mask_pad, opt_ar,
                    opt_force_render=False):
    """Decide the (unwrapped object, insert scenario) pairs to skip from proxy renders. Never raises: on any error the
    pairs it did not decide are baked as usual."""
    if not _run:
        return
    if bpy.app.version < (5, 0, 0):
        logger.info('. Influence first: needs Blender 5.0 or newer, all pairs will be baked')
        return
    t_start = time.time()
    stats = _run['proxy_stats']
    threshold = INFLUENCE_FIRST['factor'] * vlm_utils.get_lm_threshold()
    abs_bakepath = bpy.path.abspath(bakepath)
    cache_path = os.path.join(abs_bakepath, INFLUENCE_FIRST_CACHE)
    cache_key = json.dumps(INFLUENCE_FIRST, sort_keys=True)
    cached = {} # a forced render ignores the cached decisions (they are rewritten at the end)
    try:
        with open(cache_path) as f:
            data = json.load(f)
        if data.get('key') == cache_key and not opt_force_render:
            cached = {(a, b): v for a, b, v in data['values']}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    _run['proxy_skips'].update(k for k, v in cached.items() if v < threshold)
    stats['cached_skips'] = len(_run['proxy_skips'])

    scenarios = [s for s in light_scenarios if is_insert_scenario(s) and s[3] and all(l.type == 'LIGHT' for l in s[3])]
    candidates = {} # object name -> [scenarios]
    for obj in [o for o in bake_col.all_objects if o.vlmSettings.use_bake]:
        for scenario in scenarios:
            key = (scenario[0], obj.name)
            if key in cached or (not opt_force_render and os.path.exists(f'{abs_bakepath}{scenario[0]} - Bake - {vlm_utils.clean_filename(obj.name)}.exr')
                                 and os.path.exists(f'{abs_bakepath}{scenario[0]} - Influence - {vlm_utils.clean_filename(obj.name)}.exr')):
                continue
            candidates.setdefault(obj.name, []).append(scenario)
    stats['candidates'] = sum(len(v) for v in candidates.values())
    logger.info(f'\nInfluence first: {stats["candidates"]} candidate pairs over {len(candidates)} objects ({len(_run["proxy_skips"])} cached skips)')
    if not candidates:
        return

    values = {}
    tmp = os.path.join(abs_bakepath, f'Influence First tmp{os.getpid()}')
    vl = scene.view_layers[0]
    prev = dict(pct=scene.render.resolution_percentage, spp=scene.cycles.samples, denoise=scene.cycles.use_denoising,
                border=scene.render.use_border, world=scene.world, compositing=scene.render.use_compositing,
                store=vl.cycles.denoising_store_passes, path=scene.render.filepath, lightgroups=[g.name for g in vl.lightgroups])
    made = {}
    try:
        # Render sets: greedy first fit of the padded object masks, so the members of a set never overlap on screen
        masks = {}
        for name in candidates:
            try:
                mask = _object_mask(bpy.data.objects[name], mask_scene, mask_path, opt_mask_size, opt_mask_pad, opt_ar)
            except Exception as e:
                logger.info(f'. Influence first: no object mask for {name} ({e!r}), its pairs will be baked')
                mask = None
            if mask is not None:
                masks[name] = mask
        sets = []
        for name in sorted(masks, key=lambda n: -int(masks[n].sum())):
            if not bpy.data.objects[name].vlmSettings.hide_from_others:
                for render_set in sets:
                    if not render_set['solo'] and not (render_set['acc'] & masks[name]).any():
                        render_set['names'].append(name)
                        render_set['acc'] |= masks[name]
                        break
                else:
                    sets.append(dict(names=[name], acc=masks[name].copy(), solo=False))
            else:
                sets.append(dict(names=[name], acc=masks[name], solo=True))
        logger.info(f'. Influence first: {len(sets)} render sets {[len(s["names"]) for s in sets]} ({len(candidates) - len(masks)} objects without a mask are baked)')

        scene.render.resolution_percentage = INFLUENCE_FIRST['res_pct']
        scene.cycles.samples = INFLUENCE_FIRST['spp']
        scene.cycles.use_denoising = False
        scene.render.use_border = False
        scene.world = None
        vl.cycles.denoising_store_passes = False
        for set_index, render_set in enumerate(sets):
            set_scenarios = sorted({s[0]: s for n in render_set['names'] for s in candidates[n]}.items())
            chunks = _light_disjoint_chunks(set_scenarios, INFLUENCE_FIRST['chunk'])
            for chunk_index, chunk in enumerate(chunks):
                t0 = time.time()
                members = [bpy.data.objects[n] for n in render_set['names']]
                dups, linked, out_of_indirect, lights_in, prev_lightgroups, prev_colors = [], [], [], [], {}, {}
                try:
                    for obj in members:
                        dup = obj.copy()
                        for slot in dup.material_slots:
                            mat = slot.material
                            slot.link = 'OBJECT'
                            slot.material = _proxy_material(mat, made)
                        render_col.objects.link(dup)
                        dups.append(dup)
                        if obj.name in indirect_col.objects:
                            indirect_col.objects.unlink(obj)
                            out_of_indirect.append(obj)
                        for scenario in chunk.values(): # light linking: the copy joins every link collection holding the object
                            for light in scenario[3]:
                                light_linking = getattr(light, 'light_linking', None)
                                for col in ((light_linking.receiver_collection, light_linking.blocker_collection) if light_linking else ()):
                                    if col is None or dup.name in col.objects or obj.name not in col.all_objects:
                                        continue
                                    _link_like(col, obj, dup)
                                    linked.append((col, dup))
                    for g in list(vl.lightgroups):
                        vl.lightgroups.remove(g)
                    for name, scenario in chunk.items():
                        lightgroup = name.replace('.', '_')
                        vl.lightgroups.add(name=lightgroup)
                        if vlm_utils.is_rgb_led(scenario[3]):
                            for light in scenario[3]:
                                prev_colors.setdefault(light.data.name, (light.data, tuple(light.data.color)))  # keyed by datablock: linked duplicates share one colour
                                light.data.color = (1.0, 1.0, 1.0)
                        for light in scenario[3]:
                            prev_lightgroups.setdefault(light.name, (light, light.lightgroup))
                            light.lightgroup = lightgroup
                            if light.name not in render_col.objects:
                                render_col.objects.link(light)
                                lights_in.append(light)
                    # One File Output per light group, each in its own folder (Blender 5.x decorates the file names)
                    tree = get_compositor_tree(scene, create=True, clear=True)
                    rl = tree.nodes.new('CompositorNodeRLayers')
                    rl.scene = scene
                    out_dirs = {}
                    for k, name in enumerate(chunk):
                        out_dirs[name] = os.path.join(tmp, str(k))
                        os.makedirs(out_dirs[name], exist_ok=True)
                        out = tree.nodes.new('CompositorNodeOutputFile')
                        configure_file_output_node_52(out, socket_name='Image', directory=out_dirs[name], file_name='p', file_format='OPEN_EXR',
                                                      color_mode='RGB', color_depth='16', exr_codec='ZIP', use_file_extension=False)
                        tree.links.new(rl.outputs[f'Combined_{name.replace(".", "_")}'], out.inputs[0])
                    set_compositing_enabled(scene, True)
                    bpy.ops.render.render(write_still=False, scene=scene.name)
                    set_compositing_enabled(scene, False)
                    stats['renders'] += 1
                    import OpenImageIO as oiio
                    resized_masks = None
                    for name in chunk:
                        files = [os.path.join(out_dirs[name], f) for f in os.listdir(out_dirs[name])]
                        if len(files) != 1:
                            raise RuntimeError(f'light group output for {name}: {files}')
                        inp = oiio.ImageInput.open(files[0])
                        w, h = inp.spec().width, inp.spec().height
                        px = np.nan_to_num(np.asarray(inp.read_image(0, 0, 0, 3, 'float')).reshape(h, w, 3)).max(axis=2)
                        inp.close()
                        os.remove(files[0])
                        if resized_masks is None:
                            resized_masks = {n: np.asarray(Image.fromarray(masks[n].astype(np.uint8) * 255).resize((w, h), Image.BOX)) > 0 for n in render_set['names']}
                        for n in render_set['names']:
                            if any(s[0] == name for s in candidates[n]):
                                values[(name, n)] = float(px[resized_masks[n]].max()) if resized_masks[n].any() else 0.0
                finally:
                    for col, dup in linked:
                        col.objects.unlink(dup)
                    for dup in dups:
                        bpy.data.objects.remove(dup)
                    for obj in out_of_indirect:
                        indirect_col.objects.link(obj)
                    for light in lights_in:
                        render_col.objects.unlink(light)
                    for light, lightgroup in prev_lightgroups.values():
                        light.lightgroup = lightgroup
                    for data, color in prev_colors.values():
                        data.color = color
                logger.info(f'. Influence first: set {set_index + 1}/{len(sets)} chunk {chunk_index + 1}/{len(chunks)} ({len(members)} objects, {len(chunk)} light groups) in {time.time() - t0:.1f}s')
    except Exception as e:
        logger.info(f'. Influence first: ERROR {e!r}, the undecided pairs will be baked')
    finally:
        for g in list(vl.lightgroups):
            vl.lightgroups.remove(g)
        for name in prev['lightgroups']:
            vl.lightgroups.add(name=name)
        scene.render.resolution_percentage, scene.cycles.samples, scene.cycles.use_denoising = prev['pct'], prev['spp'], prev['denoise']
        scene.render.use_border, scene.world, scene.render.filepath = prev['border'], prev['world'], prev['path']
        vl.cycles.denoising_store_passes = prev['store']
        set_compositing_enabled(scene, prev['compositing'])
        for mat in made.values():
            bpy.data.materials.remove(mat)
        get_compositor_tree(scene, create=True, clear=True)
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    skipped = [k for k, v in values.items() if v < threshold]
    _run['proxy_skips'].update(skipped)
    stats['skipped'] += len(skipped)
    stats['seconds'] += round(time.time() - t_start, 1)
    if values:
        cached.update(values)
        try:
            with open(cache_path + '.tmp', 'w') as f:
                json.dump(dict(key=cache_key, values=[[a, b, v] for (a, b), v in sorted(cached.items())]), f)
            os.replace(cache_path + '.tmp', cache_path)
        except OSError as e:
            logger.info(f'. Influence first: could not write {cache_path}: {e}')
    logger.info(f'. Influence first: {len(skipped)} of {len(values)} evaluated pairs under {threshold:g} will not be baked ({stats})')
