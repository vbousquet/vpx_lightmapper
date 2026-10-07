"""Import add-on modules outside of Blender.

The add-on package's ``__init__`` imports ``bpy``, so it cannot be imported by a
plain python3 interpreter.  The modules under test (``vlm_cfb``, ``vlm_md2``,
``biff_io``) are Blender free but use relative imports, so they are exposed here
through a stand-in package that points at the add-on directory.
"""

import importlib
import pathlib
import sys
import types

ADDON_DIR = pathlib.Path(__file__).resolve().parents[1] / 'addons' / 'vpx_lightmapper'

_PACKAGE = 'vlm_addon'
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(ADDON_DIR)]
    sys.modules[_PACKAGE] = package


def load(name):
    return importlib.import_module(f'{_PACKAGE}.{name}')
