"""Round trip a .vpx table through the pure Python compound file writer.

Checks, in order:
  1. every storage and stream survives, byte for byte, when read back;
  2. ``vpxtool extract`` (the Rust ``cfb`` crate, a strict MS-CFB reader) gives
     the same output for the rewritten table as for the original, if vpxtool is
     on PATH;
  3. the MAC hashing rules, by recomputing ``GameStg/MAC`` of the untouched
     table and comparing with what Visual Pinball stored there.

The table to use comes from argv[1], else $VLM_TEST_VPX, else the Blank Table
shipped in docs/.  Run with: python3 tests/test_cfb_roundtrip.py [table.vpx]
"""

import os
import pathlib
import struct
import shutil
import subprocess
import sys
import tempfile

import olefile

from _addon import ADDON_DIR, load

vlm_cfb = load('vlm_cfb')
biff_io = load('biff_io')

DEFAULT_TABLE = ADDON_DIR.parents[1] / 'docs' / 'Blank Table' / 'Blank Table.vpx'


def find_table():
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1])
    if os.environ.get('VLM_TEST_VPX'):
        return pathlib.Path(os.environ['VLM_TEST_VPX'])
    return DEFAULT_TABLE


def rewrite(src_path, dst_path):
    """Copy every storage and stream of src_path into a new file via CfbWriter."""
    src = olefile.OleFileIO(str(src_path))
    try:
        writer = vlm_cfb.CfbWriter(str(dst_path))
        storages = {}
        # Shortest paths first, so a storage exists before its children.
        for entry in sorted(src.listdir(streams=True, storages=True), key=len):
            path = '/'.join(entry)
            parent = writer if len(entry) == 1 else storages['/'.join(entry[:-1])]
            if src.get_type(path) == olefile.STGTY_STORAGE:
                storages[path] = parent.create_storage(entry[-1])
            else:
                parent.create_stream(entry[-1]).write(src.openstream(path).read())
        writer.commit()
        writer.close()
    finally:
        src.close()


def check_streams_identical(src_path, dst_path):
    src = olefile.OleFileIO(str(src_path))
    dst = olefile.OleFileIO(str(dst_path))
    try:
        src_entries = sorted('/'.join(e) for e in src.listdir(streams=True, storages=True))
        dst_entries = sorted('/'.join(e) for e in dst.listdir(streams=True, storages=True))
        assert src_entries == dst_entries, f'entry mismatch: {set(src_entries) ^ set(dst_entries)}'
        streams = 0
        for path in src_entries:
            if src.get_type(path) == olefile.STGTY_STORAGE:
                continue
            assert src.openstream(path).read() == dst.openstream(path).read(), f'stream differs: {path}'
            streams += 1
        return streams, len(src_entries) - streams
    finally:
        src.close()
        dst.close()


def check_vpxtool(src_path, dst_path):
    """Extract both tables with vpxtool and diff the results. None if unavailable."""
    if shutil.which('vpxtool') is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        outputs = []
        for index, table in enumerate((src_path, dst_path)):
            copy = pathlib.Path(tmp) / f'{index}' / 'table.vpx'
            copy.parent.mkdir()
            shutil.copy(table, copy)
            result = subprocess.run(['vpxtool', 'extract', '-f', str(copy)],
                                    capture_output=True, text=True)
            assert result.returncode == 0, f'vpxtool failed on {table}: {result.stdout}{result.stderr}'
            outputs.append(copy.with_suffix(''))
        diff = subprocess.run(['diff', '-r', str(outputs[0]), str(outputs[1])],
                              capture_output=True, text=True)
        assert diff.returncode == 0, f'vpxtool extraction differs:\n{diff.stdout}'
        return True


def check_mac(table):
    """The stored MAC proves the hashing rules, since Visual Pinball wrote it."""
    src = olefile.OleFileIO(str(table))
    try:
        stored = src.openstream('GameStg/MAC').read()
    finally:
        src.close()
    computed = biff_io.compute_table_mac(str(table))
    assert computed == stored, f'MAC mismatch: computed {computed.hex()}, stored {stored.hex()}'
    return stored


def check_directory_tree(path):
    """Verify the on-disk directory really is a red-black tree, per [MS-CFB] 2.6.4."""
    raw = pathlib.Path(path).read_bytes()
    n_fat = struct.unpack('<I', raw[44:48])[0]
    first_dir = struct.unpack('<I', raw[48:52])[0]
    fat_sectors = list(struct.unpack('<109I', raw[76:512]))[:n_fat]
    difat = struct.unpack('<I', raw[68:72])[0]
    while difat != vlm_cfb.ENDOFCHAIN and len(fat_sectors) < n_fat:
        block = struct.unpack('<128I', raw[512 + difat * 512:1024 + difat * 512])
        fat_sectors += [s for s in block[:127] if s != vlm_cfb.FREESECT]
        difat = block[127]
    fat_sectors = fat_sectors[:n_fat]
    fat = struct.unpack('<%dI' % (128 * n_fat),
                        b''.join(raw[512 + s * 512:1024 + s * 512] for s in fat_sectors))
    directory = b''
    sector = first_dir
    while sector != vlm_cfb.ENDOFCHAIN:
        directory += raw[512 + sector * 512:1024 + sector * 512]
        sector = fat[sector]

    def entry(index):
        raw_entry = directory[index * 128:(index + 1) * 128]
        name_len = struct.unpack('<H', raw_entry[64:66])[0]
        return {
            'name': raw_entry[:max(name_len - 2, 0)].decode('utf-16-le'),
            'color': raw_entry[67],
            'left': struct.unpack('<I', raw_entry[68:72])[0],
            'right': struct.unpack('<I', raw_entry[72:76])[0],
            'child': struct.unpack('<I', raw_entry[76:80])[0],
        }

    def black_height(index, parent_is_red):
        if index == vlm_cfb.NOSTREAM:
            return 1
        node = entry(index)
        is_red = node['color'] == vlm_cfb.COLOR_RED
        assert not (is_red and parent_is_red), f"red node {node['name']} under a red parent"
        for side, expected in (('left', True), ('right', False)):
            if node[side] != vlm_cfb.NOSTREAM:
                smaller = vlm_cfb._entry_sort_key(entry(node[side])['name']) < \
                    vlm_cfb._entry_sort_key(node['name'])
                assert smaller == expected, f"{side} child of {node['name']} is out of order"
        left, right = black_height(node['left'], is_red), black_height(node['right'], is_red)
        assert left == right, f"unbalanced black height at {node['name']}"
        return left + (0 if is_red else 1)

    pending = [0]
    while pending:
        node = entry(pending.pop())
        if node['child'] == vlm_cfb.NOSTREAM:
            continue
        assert entry(node['child'])['color'] == vlm_cfb.COLOR_BLACK, \
            f"subtree root under {node['name']} is not black"
        black_height(node['child'], False)
        subtree = [node['child']]
        while subtree:
            index = subtree.pop()
            pending.append(index)
            child = entry(index)
            subtree += [child[s] for s in ('left', 'right') if child[s] != vlm_cfb.NOSTREAM]


def test_synthetic_directories(tmp):
    """Entry counts around the tree's level boundaries, plus mini/regular streams."""
    for count in (0, 1, 2, 3, 6, 7, 8, 15, 16, 127, 128, 300):
        path = pathlib.Path(tmp) / f'synthetic-{count}.vpx'
        writer = vlm_cfb.CfbWriter(str(path))
        storage = writer.create_storage('GameStg')
        expected = {}
        for index in range(count):
            # Sizes straddle the 4096 byte mini stream cutoff.
            expected[f'S{index}'] = bytes([index % 256]) * ((index * 997) % 9000)
            storage.create_stream(f'S{index}').write(expected[f'S{index}'])
        writer.create_stream('Empty').write(b'')
        writer.commit()
        writer.close()
        check_directory_tree(path)
        handle = olefile.OleFileIO(str(path))
        try:
            for name, data in expected.items():
                assert handle.openstream(f'GameStg/{name}').read() == data, f'{count}/{name}'
            assert handle.openstream('Empty').read() == b''
        finally:
            handle.close()


def main():
    table = find_table()
    if not table.is_file() or table.stat().st_size == 0:
        print(f'SKIP: no test table at {table} (pass one as an argument or set $VLM_TEST_VPX)')
        return 0
    print(f'table: {table} ({table.stat().st_size} bytes)')
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        rewritten = pathlib.Path(tmp) / 'rewritten.vpx'
        try:
            rewrite(table, rewritten)
            streams, storages = check_streams_identical(table, rewritten)
            print(f'ok   round trip: {streams} streams byte identical, {storages} storages, '
                  f'{rewritten.stat().st_size} bytes out')
        except AssertionError as error:
            failures += 1
            print(f'FAIL round trip: {error}')

        try:
            test_synthetic_directories(tmp)
            check_directory_tree(rewritten)
            print('ok   directory: red-black tree and sibling ordering valid')
        except AssertionError as error:
            failures += 1
            print(f'FAIL directory: {error}')

        try:
            result = check_vpxtool(table, rewritten)
            print('ok   vpxtool: extractions identical' if result else
                  'skip vpxtool: not on PATH')
        except AssertionError as error:
            failures += 1
            print(f'FAIL vpxtool: {error}')

        try:
            print(f'ok   MAC: recomputed digest matches GameStg/MAC ({check_mac(table).hex()})')
        except AssertionError as error:
            failures += 1
            print(f'FAIL MAC: {error}')

    print('CFB: all tests passed' if not failures else f'CFB: {failures} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
