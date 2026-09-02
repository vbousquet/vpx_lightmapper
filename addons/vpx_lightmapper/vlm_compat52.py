"""Blender API compatibility helpers for VPX Lightmapper.

Primary target: Blender 5.2 LTS.
Keeps the addon source compatible with Blender 4.5 where practical.
"""
import re
import os
import bpy
import gpu


def safe_relpath(filepath):
    """Return a Blender-relative path when possible, otherwise keep it absolute.

    bpy.path.relpath() raises ValueError on Windows when the target file is on a
    different drive than the current .blend file (for example VPX on D: and the
    Blender project on C:). In that case an absolute path is the correct fallback;
    bpy.path.abspath() can still resolve it later.
    """
    filepath = os.fspath(filepath) if hasattr(filepath, '__fspath__') else str(filepath)
    try:
        return bpy.path.relpath(filepath)
    except (ValueError, TypeError):
        return filepath


def set_eevee_engine(scene):
    """Set the EEVEE engine identifier for the running Blender version."""
    if bpy.app.version >= (5, 0, 0):
        scene.render.engine = 'BLENDER_EEVEE'
    elif bpy.app.version >= (4, 2, 0):
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
    else:
        scene.render.engine = 'BLENDER_EEVEE'


def set_compositing_enabled(scene, enabled):
    """Enable/disable scene compositing without relying on deprecated use_nodes."""
    # use_nodes controlled node creation/usage before Blender 5.0. In 5.x the
    # compositor tree is a separate datablock and render.use_compositing is the
    # switch that controls whether it is executed.
    if hasattr(scene, 'render') and hasattr(scene.render, 'use_compositing'):
        scene.render.use_compositing = bool(enabled)
    # Keep old versions working and make sure a tree exists when requested.
    if enabled and bpy.app.version < (5, 0, 0):
        scene.use_nodes = True
    elif not enabled and bpy.app.version < (5, 0, 0):
        scene.use_nodes = False


def get_compositor_tree(scene, create=True, clear=False):
    """Return the scene compositor node tree in Blender 5.x/4.x."""
    if bpy.app.version >= (5, 0, 0):
        tree = scene.compositing_node_group
        if tree is None and create:
            # Blender 5.x stores the active compositor node tree on the Scene.
            # The property is assignable in 5.2; create a dedicated tree instead
            # of relying on the deprecated Scene.use_nodes toggle.
            tree = bpy.data.node_groups.new(f'{scene.name}.Compositor', 'CompositorNodeTree')
            scene.compositing_node_group = tree
        if tree is None:
            return None
    else:
        if create:
            scene.use_nodes = True
        tree = scene.node_tree
        if tree is None:
            return None

    if clear:
        tree.nodes.clear()
        tree.links.clear()
    return tree




def ensure_gpu_backend():
    """Initialize Blender's GPU backend when running without a UI context."""
    if bpy.app.version >= (5, 2, 0) and bpy.app.background and hasattr(gpu, 'init'):
        try:
            gpu.init()
        except Exception as exc:
            raise RuntimeError(f"VPX Lightmapper: failed to initialize Blender GPU backend: {exc}") from exc


def set_material_surface_render_method(material, legacy_mode):
    """Map the addon's legacy material blend modes to Blender 5.x settings."""
    if material is None:
        return
    if bpy.app.version >= (5, 0, 0) and hasattr(material, 'surface_render_method'):
        mode = str(legacy_mode).upper()
        # Blender 5.x no longer uses the old EEVEE blend_method API. BLENDED is
        # the direct equivalent of legacy BLEND. OPAQUE has no separate 5.x
        # enum; DITHERED is the non-blended surface mode and with alpha=1
        # produces the same opaque result used by the lightmapper.
        material.surface_render_method = 'BLENDED' if mode == 'BLEND' else 'DITHERED'
    elif hasattr(material, 'blend_method'):
        material.blend_method = legacy_mode


def export_obj_selected(
    filepath,
    *,
    global_scale=1.0,
    forward_axis='NEGATIVE_Y',
    up_axis='NEGATIVE_Z',
    export_materials=False,
    export_triangulated_mesh=True,
):
    """Export selected objects through Blender's current OBJ exporter."""
    # The Python OBJ exporter was replaced by bpy.ops.wm.obj_export in Blender
    # 4.0. The lightmapper's supported baseline is 4.5, so this is intentionally
    # the single code path for all supported versions.
    return bpy.ops.wm.obj_export(
        filepath=bpy.path.abspath(filepath),
        export_selected_objects=True,
        global_scale=global_scale,
        forward_axis=forward_axis,
        up_axis=up_axis,
        export_materials=export_materials,
        export_triangulated_mesh=export_triangulated_mesh,
    )


def set_diffuse_color_pass_enabled(view_layer, enabled=True):
    """Enable the diffuse-color render pass when that RNA property exists."""
    prop = 'use_pass_diffuse_color'
    if hasattr(view_layer, prop):
        setattr(view_layer, prop, bool(enabled))
        return True
    # Blender 5.x keeps the human-readable pass name but API properties can
    # differ between render engines/builds. The DIFFUSE bake pass_filter itself
    # remains authoritative, so absence of this optional flag is safe.
    return False


def configure_file_output_node_52(
    node,
    *,
    socket_name='Image',
    directory='',
    file_name='',
    file_format='OPEN_EXR',
    color_mode='RGBA',
    color_depth='16',
    exr_codec='ZIP',
    use_file_extension=False,
):
    """Configure a Blender 5.x compositor File Output node.

    Blender 5.x replaced the old file_slots/layer_slots API with
    file_output_items. The file/media format is controlled by the
    ImageFormatSettings object exposed as node.format. In particular,
    media_type must be set on node.format before file_format is assigned.
    """
    if bpy.app.version < (5, 0, 0):
        raise RuntimeError('configure_file_output_node_52() requires Blender 5.0 or newer')

    # Blender 5.x File Output nodes create their input sockets dynamically
    # through file_output_items. Do not clear/recreate an existing item here: this
    # helper is also called later to change the output path, and recreating the
    # item would destroy the existing compositor link.
    items = node.file_output_items
    socket_name = socket_name or 'Image'
    if len(items) == 0:
        items.new('RGBA', socket_name)
    else:
        # This addon uses exactly one output image per File Output node.
        # Keep the existing item so links remain intact.
        items[0].name = socket_name
        for item in list(items)[1:]:
            items.remove(item)
    if hasattr(node, 'active_item_index'):
        node.active_item_index = 0

    # IMPORTANT: media_type belongs to ImageFormatSettings (node.format),
    # not to CompositorNodeOutputFile itself.
    fmt = node.format
    fmt.media_type = 'IMAGE'
    fmt.file_format = file_format
    fmt.color_mode = color_mode
    fmt.color_depth = color_depth
    if file_format == 'OPEN_EXR':
        fmt.exr_codec = exr_codec

    node.directory = bpy.path.abspath(directory) if directory else ''
    node.file_name = file_name
    node.use_file_extension = bool(use_file_extension)
    return node


def configure_denoise_node_52(node, prefilter='ACCURATE'):
    """Configure the Blender 5.x compositor Denoise node without using removed properties."""
    if bpy.app.version < (5, 0, 0):
        raise RuntimeError('configure_denoise_node_52() requires Blender 5.0 or newer')

    # Blender 5.x exposes Prefilter as an enum input rather than as a node
    # property. The identifiers exposed by the 5.2 RNA are title-cased.
    value = {
        'NONE': 'None',
        'FAST': 'Fast',
        'ACCURATE': 'Accurate',
        'None': 'None',
        'Fast': 'Fast',
        'Accurate': 'Accurate',
    }.get(prefilter, 'Accurate')
    node.inputs['Prefilter'].default_value = value
    return node


# ---------------------------------------------------------------------------
# Blender 5.0 removed gpu.types.GPUShader(vertex_source, fragment_source).
# This helper translates the small GLSL shaders used by the lightmapper to
# GPUShaderCreateInfo at runtime. It deliberately supports the simple shader
# declaration subset used by this addon: vertex attributes, varyings, scalar
# push constants, and sampler2D uniforms.
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    'float': 'FLOAT',
    'vec2': 'VEC2',
    'vec3': 'VEC3',
    'vec4': 'VEC4',
    'int': 'INT',
    'ivec2': 'IVEC2',
    'ivec3': 'IVEC3',
    'ivec4': 'IVEC4',
    'uint': 'UINT',
    'uvec2': 'UVEC2',
    'uvec3': 'UVEC3',
    'uvec4': 'UVEC4',
    'bool': 'BOOL',
    'mat3': 'MAT3',
    'mat4': 'MAT4',
}
_SAMPLER_MAP = {
    'sampler2D': 'FLOAT_2D',
    'sampler2DShadow': 'SHADOW_2D',
    'sampler3D': 'FLOAT_3D',
    'samplerCube': 'FLOAT_CUBE',
}

_DECL_RE = re.compile(
    r'(?<![A-Za-z0-9_])(in|out|uniform)\s+'
    r'(sampler2DShadow|sampler2D|sampler3D|samplerCube|float|vec[234]|int|ivec[234]|uint|uvec[234]|bool|mat[34])'
    r'\s+([A-Za-z_]\w*)\s*;'
)


def _parse_shader_declarations(source, stage):
    decls = []
    for m in _DECL_RE.finditer(source):
        qualifier, glsl_type, name = m.groups()
        decls.append((m.group(0), qualifier, glsl_type, name))
    return decls


def _strip_shader_declarations(source, stage):
    # Remove declarations that are represented by GPUShaderCreateInfo.
    return _DECL_RE.sub('', source)


def create_shader(vertex_source, fragment_source):
    """Create a GPUShader using the Blender 5.x CreateInfo API.

    The returned object has the normal GPUShader API, so existing calls to
    bind(), uniform_float/int/sampler(), and batch_for_shader() remain valid.
    """
    if bpy.app.version < (5, 0, 0):
        return gpu.types.GPUShader(vertex_source, fragment_source)

    vdecls = _parse_shader_declarations(vertex_source, 'vertex')
    fdecls = _parse_shader_declarations(fragment_source, 'fragment')

    info = gpu.types.GPUShaderCreateInfo()

    # Vertex inputs.
    slot = 0
    for _raw, qualifier, glsl_type, name in vdecls:
        if qualifier == 'in':
            if glsl_type not in _TYPE_MAP:
                raise ValueError(f'Unsupported vertex input type: {glsl_type} {name}')
            info.vertex_in(slot, _TYPE_MAP[glsl_type], name)
            slot += 1

    # Varyings produced by the vertex shader and consumed by the fragment shader.
    vouts = [(t, n) for _r, q, t, n in vdecls if q == 'out']
    fins = {(t, n) for _r, q, t, n in fdecls if q == 'in'}
    if vouts:
        iface = gpu.types.GPUStageInterfaceInfo('vlm_iface')
        for glsl_type, name in vouts:
            if (glsl_type, name) not in fins:
                # A vertex output that is not consumed still needs a valid
                # interface in the CreateInfo model, but there is no reason to
                # expose it to the fragment stage. This addon does not use one.
                continue
            iface.smooth(_TYPE_MAP[glsl_type], name)
        info.vertex_out(iface)

    # Fragment output(s). The addon shaders use one vec4 output named FragColor.
    fout = [(t, n) for _r, q, t, n in fdecls if q == 'out']
    for index, (glsl_type, name) in enumerate(fout):
        if glsl_type not in _TYPE_MAP:
            raise ValueError(f'Unsupported fragment output type: {glsl_type} {name}')
        info.fragment_out(index, _TYPE_MAP[glsl_type], name)

    # Uniforms: samplers become sampler bindings, all scalar/vector values are
    # push constants. The shaders in this addon stay well below the 128-byte
    # push-constant limit.
    sampler_slot = 0
    push_names = set()
    sampler_names = set()
    for _raw, qualifier, glsl_type, name in vdecls + fdecls:
        if qualifier != 'uniform':
            continue
        if glsl_type in _SAMPLER_MAP:
            if name in sampler_names:
                continue
            info.sampler(sampler_slot, _SAMPLER_MAP[glsl_type], name)
            sampler_slot += 1
            sampler_names.add(name)
        else:
            if name in push_names:
                continue
            if glsl_type not in _TYPE_MAP:
                raise ValueError(f'Unsupported uniform type: {glsl_type} {name}')
            info.push_constant(_TYPE_MAP[glsl_type], name)
            push_names.add(name)

    info.vertex_source(_strip_shader_declarations(vertex_source, 'vertex'))
    info.fragment_source(_strip_shader_declarations(fragment_source, 'fragment'))
    return gpu.shader.create_from_info(info)
