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
import stat
import struct
import shutil
import subprocess
import sys
import tempfile

import olefile

from _addon import ADDON_DIR, load

vlm_cfb = load('vlm_cfb')
biff_io = load('biff_io')
vlm_md2 = load('vlm_md2')

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
        left, right = black_height(node['left'], is_red), black_height(node['right'], is_red)
        assert left == right, f"unbalanced black height at {node['name']}"
        return left + (0 if is_red else 1)

    def check_order(index, low, high):
        """Every node must sort inside the bounds its ancestors impose.

        Comparing a node only against its two immediate children would miss a
        misplaced grandchild, which is exactly the shape of bug a binary
        searching reader trips over.
        """
        if index == vlm_cfb.NOSTREAM:
            return 0
        node = entry(index)
        key = vlm_cfb._entry_sort_key(node['name'])
        assert low is None or key > low, f"{node['name']} sorts below its ancestor bound"
        assert high is None or key < high, f"{node['name']} sorts above its ancestor bound"
        return 1 + check_order(node['left'], low, key) + check_order(node['right'], key, high)

    checked = 0
    pending = [0]
    while pending:
        index = pending.pop()
        node = entry(index)
        if node['child'] == vlm_cfb.NOSTREAM:
            continue
        assert entry(node['child'])['color'] == vlm_cfb.COLOR_BLACK, \
            f"subtree root under {node['name']} is not black"
        black_height(node['child'], False)
        checked += check_order(node['child'], None, None)
        # Walk the whole sibling subtree so nested storages are checked too.
        subtree = [node['child']]
        while subtree:
            child_index = subtree.pop()
            pending.append(child_index)
            child = entry(child_index)
            subtree += [child[s] for s in ('left', 'right') if child[s] != vlm_cfb.NOSTREAM]
    return checked


def test_synthetic_directories(tmp):
    """Entry counts around the tree's level boundaries, plus mini/regular streams."""
    for count in (0, 1, 2, 3, 6, 7, 8, 15, 16, 127, 128, 300):
        path = pathlib.Path(tmp) / f'synthetic-{count}.vpx'
        writer = vlm_cfb.CfbWriter(str(path))
        storage = writer.create_storage('GameStg')
        expected = {}
        for index in range(count):
            # Sizes straddle the 4096 byte mini stream cutoff.
            # Straddle the 4096 byte mini stream cutoff, and land exactly on it.
            boundary = (4095, 4096, 4097, 0, 512)
            size = boundary[index % len(boundary)] if index < 2 * len(boundary) \
                else (index * 997) % 9000
            expected[f'S{index}'] = bytes([index % 256]) * size
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



def test_mini_stream_cutoff(tmp):
    """A stream of exactly 4096 bytes belongs in the regular FAT, not the mini stream.

    [MS-CFB]: streams *smaller than* the cutoff go in the mini stream, so the
    boundary is strict.  Both readers Visual Pinball relies on agree (POLE's
    `size >= threshold` and olefile's `size < minisectorcutoff`), and getting it
    wrong makes a 4096 byte stream read back as garbage.  The check needs a
    large stream ahead of the boundary one, otherwise the mini stream starts at
    sector 0 and a misplaced stream reads correctly by coincidence.
    """
    path = pathlib.Path(tmp) / 'cutoff.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    storage = writer.create_storage('GameStg')
    payloads = {
        'Ahead': b'A' * 40000,      # pushes the mini stream off sector 0
        'Under': b'u' * 4095,       # mini stream
        'Exact': b'e' * 4096,       # regular FAT: the boundary is strict
        'Over': b'o' * 4097,        # regular FAT
    }
    for name, data in payloads.items():
        storage.create_stream(name).write(data)
    writer.commit()
    writer.close()

    handle = olefile.OleFileIO(str(path))
    try:
        for name, data in payloads.items():
            assert handle.openstream(f'GameStg/{name}').read() == data, \
                f'{name} ({len(data)} bytes) did not read back'
    finally:
        handle.close()

    # Read the directory directly: reading alone would not prove placement if
    # the mini stream happened to line up with the regular sectors.
    raw = path.read_bytes()
    mini_cutoff = struct.unpack('<I', raw[56:60])[0]
    assert mini_cutoff == 4096, f'mini stream cutoff is {mini_cutoff}, expected 4096'
    n_fat = struct.unpack('<I', raw[44:48])[0]
    first_dir = struct.unpack('<I', raw[48:52])[0]
    fat_sectors = list(struct.unpack(f'<{min(n_fat, 109)}I', raw[76:76 + 4 * min(n_fat, 109)]))
    fat = struct.unpack(f'<{128 * len(fat_sectors)}I',
                        b''.join(raw[512 + s * 512:1024 + s * 512] for s in fat_sectors))
    directory = b''
    sector = first_dir
    while sector != vlm_cfb.ENDOFCHAIN:
        directory += raw[512 + sector * 512:1024 + sector * 512]
        sector = fat[sector]
    for index in range(len(directory) // 128):
        entry = directory[index * 128:(index + 1) * 128]
        name_len = struct.unpack('<H', entry[64:66])[0]
        name = entry[:max(name_len - 2, 0)].decode('utf-16-le')
        if name != 'Exact':
            continue
        size = struct.unpack('<Q', entry[120:128])[0]
        assert size == 4096, f'Exact is {size} bytes'
        start = struct.unpack('<I', entry[116:120])[0]
        # A regular sector index addresses the file directly, so the payload
        # must be there; for a mini stream index it would not be.
        assert raw[512 + start * 512:512 + start * 512 + 16] == b'e' * 16, \
            'a 4096 byte stream was placed in the mini stream'
        break
    else:
        raise AssertionError('Exact entry not found in the directory')


def test_large_file_difat(tmp):
    """Over 109 FAT sectors, so the DIFAT chain is written and walked.

    The threshold is 109 * 128 * 512 bytes, about 7.1 MB, which neither the
    Blank Table fixture nor the synthetic directories above reach, so without
    this the DIFAT branch never runs unless someone passes a big table.
    """
    path = pathlib.Path(tmp) / 'difat.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    storage = writer.create_storage('GameStg')
    expected = {}
    for index in range(16):
        # 16 x 512 KB = 8 MB of data, comfortably past the threshold.
        payload = bytes([index % 256]) * (512 * 1024)
        storage.create_stream(f'Big{index}').write(payload)
        expected[f'Big{index}'] = payload
    writer.commit()
    writer.close()

    raw = path.read_bytes()
    n_fat, n_difat = struct.unpack('<I', raw[44:48])[0], struct.unpack('<I', raw[72:76])[0]
    assert n_fat > vlm_cfb.DIFAT_IN_HEADER, f'test did not reach the DIFAT path ({n_fat} FAT sectors)'
    assert n_difat > 0, 'FAT needs more than 109 sectors but no DIFAT sector was written'
    check_directory_tree(path)
    handle = olefile.OleFileIO(str(path))
    try:
        for name, data in expected.items():
            assert handle.openstream(f'GameStg/{name}').read() == data, name
    finally:
        handle.close()
    return n_fat, n_difat


def test_name_edges(tmp):
    """Name length is counted in UTF-16 code units, and siblings collide case insensitively."""
    path = pathlib.Path(tmp) / 'names.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    storage = writer.create_storage('GameStg')

    longest = 'N' * vlm_cfb.MAX_NAME_LEN
    storage.create_stream(longest).write(b'ok')
    try:
        storage.create_stream('N' * (vlm_cfb.MAX_NAME_LEN + 1))
        raise AssertionError('a 32 character name was accepted')
    except ValueError:
        pass
    # 30 ASCII + one astral character is 31 code points but 32 code units, so it
    # overflows the 64 byte field and must be rejected too (checked below, twice).
    # A name rejected for its length must not reserve anything, so retrying the
    # same name reports the same problem rather than a phantom duplicate.
    for attempt in range(2):
        try:
            storage.create_stream('A' * 30 + '\U0001F600')
            raise AssertionError('an over long name was accepted on retry')
        except vlm_cfb.DuplicateEntryError:
            raise AssertionError(
                'a rejected name reserved its slot: retry reported a duplicate '
                f'instead of a length error (attempt {attempt + 1})')
        except ValueError:
            pass
    storage.create_stream('A' * 30 + 'x').write(b'retry')
    # Siblings are compared case insensitively, so these two would produce a
    # tree a binary searching reader cannot walk.
    storage.create_stream('Tag')
    try:
        storage.create_stream('TAG')
        raise AssertionError('a case insensitive duplicate was accepted')
    except vlm_cfb.DuplicateEntryError:
        pass
    writer.commit()
    writer.close()
    check_directory_tree(path)
    handle = olefile.OleFileIO(str(path))
    try:
        assert handle.openstream(f'GameStg/{longest}').read() == b'ok'
        assert handle.openstream(f'GameStg/{"A" * 30}x').read() == b'retry'
    finally:
        handle.close()


def _empty_biff():
    """An empty but valid BIFF stream (just the ENDB record)."""
    writer = biff_io.BIFF_writer()
    writer.close()
    return writer.get_data()


empty_biff = _empty_biff()


def test_custom_info_tags(tmp):
    """A table carrying custom info tags hashes its TableInfo/<tag> streams.

    Visual Pinball stores each tag's value in TableInfo/<tag> (PinTable::LoadInfo,
    "TableInfo/" + tag) and hashes it straight after GameStg/CustomInfoTags.
    Both shipped fixtures have an empty CustomInfoTags stream, so without this
    the rule is never exercised.
    """
    tags = ['MyTag', 'Second']
    payloads = {'MyTag': b'first value', 'Second': b'second value'}

    writer_biff = biff_io.BIFF_writer()
    for tag in tags:
        writer_biff.write_tagged_string(b'CUST', tag)
    writer_biff.close()
    cust_stream = writer_biff.get_data()

    parsed = list(biff_io.iter_custom_info_tags(cust_stream))
    assert parsed == tags, f'custom info tags round trip: {parsed} != {tags}'
    for tag in tags:
        assert biff_io.custom_info_path(tag) == f'TableInfo/{tag}', 'wrong TableInfo path'

    def build(values, screenshot=None):
        path = pathlib.Path(tmp) / f'custom-{abs(hash(tuple(sorted(values.items())))) & 0xffff}.vpx'
        writer = vlm_cfb.CfbWriter(str(path))
        gamestg = writer.create_storage('GameStg')
        tableinfo = writer.create_storage('TableInfo')
        gamestg.create_stream('Version').write(b'\x0a\x00\x00\x00')
        gamestg.create_stream('CustomInfoTags').write(cust_stream)
        gamestg.create_stream('GameData').write(empty_biff)
        tableinfo.create_stream('TableName').write(b'a table')
        if screenshot is not None:
            tableinfo.create_stream('Screenshot').write(screenshot)
        for tag, value in values.items():
            tableinfo.create_stream(tag).write(value)
        writer.commit()
        writer.close()
        return path

    path = build(payloads)
    handle = olefile.OleFileIO(str(path))
    try:
        for tag in tags:
            assert handle.openstream(f'TableInfo/{tag}').read() == payloads[tag], tag
    finally:
        handle.close()

    # The values must actually reach the digest, in Visual Pinball's order:
    # each TableInfo/<tag> straight after GameStg/CustomInfoTags.
    expected = vlm_md2.new()
    expected.update(b'Visual Pinball')
    expected.update(b'\x0a\x00\x00\x00')          # GameStg/Version
    expected.update(b'a table')                     # TableInfo/TableName
    biff_io.hash_biff_stream(expected, cust_stream)  # GameStg/CustomInfoTags
    for tag in tags:
        expected.update(payloads[tag])              # TableInfo/<tag>
    biff_io.hash_biff_stream(expected, empty_biff)   # GameStg/GameData
    computed = biff_io.compute_table_mac(str(path))
    assert computed == expected.digest(), \
        f'custom info tag digest {computed.hex()} != hand rolled {expected.digest().hex()}'

    # Changing a tag's value must change the digest, otherwise the values are
    # not being hashed at all.
    other = dict(payloads, MyTag=b'a different value')
    assert biff_io.compute_table_mac(str(build(other))) != computed, \
        'digest did not change when a custom info tag value changed'

    return len(tags)


def test_mac_file_structure(tmp):
    """Every entry of MAC_FILE_STRUCTURE: its path, mode and hashed flag, in order.

    Neither shipped fixture carries an AuthorName, ReleaseDate, AuthorEmail,
    TableBlurb, TableRules or a Screenshot, so a wrong mode, a wrong hashed
    flag, a path typo or a reordering in those entries would otherwise go
    unnoticed - which is exactly the class of bug the Screenshot mode was.
    Build a table holding every one of them and check the digest against one
    rolled by hand in Visual Pinball's order.
    """
    # Raw bytes, deliberately not valid BIFF, so hashing them as records would
    # produce a different digest (a real screenshot is a JPEG or PNG blob).
    shot = bytes(range(256)) * 7
    values = {
        'GameStg/Version': b'\x0a\x00\x00\x00',
        'TableInfo/TableName': b'a table',
        'TableInfo/AuthorName': b'an author',
        'TableInfo/TableVersion': b'1.2.3',
        'TableInfo/ReleaseDate': b'2026-09-20',
        'TableInfo/AuthorEmail': b'nobody@example.com',
        'TableInfo/AuthorWebSite': b'https://example.com',
        'TableInfo/TableBlurb': b'a blurb',
        'TableInfo/TableDescription': b'a description',
        'TableInfo/TableRules': b'the rules',
        'TableInfo/TableSaveDate': b'a save date',   # present but NOT hashed
        'TableInfo/TableSaveRev': b'7',              # present but NOT hashed
        'TableInfo/Screenshot': shot,
        'GameStg/CustomInfoTags': empty_biff,
        'GameStg/GameData': empty_biff,
    }
    # This test's own ground truth, read from Visual Pinball rather than from the
    # table under test: PinTable::SaveInfo (pintable.cpp:3325-3356) writes each
    # TableInfo value with BiffWriter::WriteBytes, which hashes raw bytes with no
    # record framing (media/fileio.cpp:191); TableSaveDate and TableSaveRev pass a
    # NULL hash; CustomInfoTags and GameData are BIFF streams.  Deriving this from
    # MAC_FILE_STRUCTURE instead would make the test circular, passing whenever a
    # wrong mode or flag changed the expected digest to match.
    expected_structure = (
        ('GameStg/Version', 0, True),
        ('TableInfo/TableName', 0, True),
        ('TableInfo/AuthorName', 0, True),
        ('TableInfo/TableVersion', 0, True),
        ('TableInfo/ReleaseDate', 0, True),
        ('TableInfo/AuthorEmail', 0, True),
        ('TableInfo/AuthorWebSite', 0, True),
        ('TableInfo/TableBlurb', 0, True),
        ('TableInfo/TableDescription', 0, True),
        ('TableInfo/TableRules', 0, True),
        ('TableInfo/TableSaveDate', 0, False),
        ('TableInfo/TableSaveRev', 0, False),
        ('TableInfo/Screenshot', 0, True),
        ('GameStg/CustomInfoTags', 1, True),
        ('GameStg/GameData', 1, True),
    )
    assert tuple(biff_io.MAC_FILE_STRUCTURE) == expected_structure, (
        'MAC_FILE_STRUCTURE no longer matches what Visual Pinball hashes:\n'
        f'  code: {tuple(biff_io.MAC_FILE_STRUCTURE)}\n'
        f'  spec: {expected_structure}')
    assert [path for path, _, _ in expected_structure] == list(values), \
        'this test is missing a stream for one of the structure entries'

    path = pathlib.Path(tmp) / 'structure.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    storages = {'GameStg': writer.create_storage('GameStg'),
                'TableInfo': writer.create_storage('TableInfo')}
    for stream_path, value in values.items():
        parent, name = stream_path.split('/')
        storages[parent].create_stream(name).write(value)
    writer.commit()
    writer.close()

    expected = vlm_md2.new()
    expected.update(b'Visual Pinball')
    for stream_path, mode, hashed in expected_structure:
        if not hashed:
            continue
        value = values[stream_path]
        if mode == 0:
            expected.update(value)
        else:
            biff_io.hash_biff_stream(expected, value)
    computed = biff_io.compute_table_mac(str(path))
    assert computed == expected.digest(), \
        f'structure digest {computed.hex()} != hand rolled {expected.digest().hex()}'

    # A different but still valid BIFF stream, for mutating the record based
    # entries: appending trailing bytes would prove nothing, because hashing
    # correctly stops at ENDB the way Visual Pinball's BiffReader::Load does.
    other_biff = biff_io.BIFF_writer()
    other_biff.write_tagged_string(b'CUST', 'a different record')
    other_biff.close()
    other_biff = other_biff.get_data()

    # Each hashed value must reach the digest, and each unhashed one must not.
    for stream_path, mode, hashed in expected_structure:
        mutated = other_biff if mode == 1 else values[stream_path] + b'!'
        assert mutated != values[stream_path], f'mutation for {stream_path} changed nothing'
        altered = dict(values, **{stream_path: mutated})
        other = pathlib.Path(tmp) / 'structure-alt.vpx'
        alt_writer = vlm_cfb.CfbWriter(str(other))
        alt_storages = {'GameStg': alt_writer.create_storage('GameStg'),
                        'TableInfo': alt_writer.create_storage('TableInfo')}
        for alt_path, value in altered.items():
            parent, name = alt_path.split('/')
            alt_storages[parent].create_stream(name).write(value)
        alt_writer.commit()
        alt_writer.close()
        changed = biff_io.compute_table_mac(str(other)) != computed
        assert changed == hashed, (
            f'{stream_path} is marked hashed={hashed} but changing it '
            f'{"did not change" if hashed else "changed"} the digest')
    return len(values)


def test_screenshot_hashed_raw(tmp):
    """TableInfo/Screenshot is hashed as raw bytes, not as BIFF records.

    PinTable::SaveInfo writes it with BiffWriter::WriteBytes, which passes the
    bytes straight to CryptHashData with no record framing, so treating the
    stream as BIFF would produce a MAC Visual Pinball rejects outright
    (APPX_E_BLOCK_HASH_INVALID).  Neither shipped fixture has a screenshot, so
    without this the rule is never exercised.
    """
    # Bytes that are not valid BIFF, so a records based hash would differ (and
    # most likely not even parse) - a real screenshot is a JPEG or PNG blob.
    shot = bytes(range(256)) * 7

    path = pathlib.Path(tmp) / 'screenshot.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    gamestg = writer.create_storage('GameStg')
    tableinfo = writer.create_storage('TableInfo')
    gamestg.create_stream('Version').write(b'\x0a\x00\x00\x00')
    gamestg.create_stream('GameData').write(empty_biff)
    tableinfo.create_stream('TableName').write(b'a table')
    tableinfo.create_stream('Screenshot').write(shot)
    writer.commit()
    writer.close()

    expected = vlm_md2.new()
    expected.update(b'Visual Pinball')
    expected.update(b'\x0a\x00\x00\x00')
    expected.update(b'a table')
    expected.update(shot)                            # raw, not hash_biff_stream
    biff_io.hash_biff_stream(expected, empty_biff)
    computed = biff_io.compute_table_mac(str(path))
    assert computed == expected.digest(), \
        f'screenshot digest {computed.hex()} != raw bytes digest {expected.digest().hex()}'

    mode = [m for path_, m, hashed in biff_io.MAC_FILE_STRUCTURE
            if path_ == 'TableInfo/Screenshot'][0]
    assert mode == 0, 'TableInfo/Screenshot must be hashed as raw bytes (mode 0)'


def test_commit_semantics(tmp):
    """iterbytes matches tobytes, commit is atomic, and the target keeps its mode."""
    def fill(writer):
        storage = writer.create_storage('GameStg')
        storage.create_stream('Small').write(b'x' * 100)        # mini stream
        storage.create_stream('Large').write(b'y' * 20000)      # regular sectors

    path = pathlib.Path(tmp) / 'commit.vpx'
    writer = vlm_cfb.CfbWriter(str(path))
    fill(writer)
    joined = writer.tobytes()
    assert joined == b''.join(writer.iterbytes()), 'iterbytes differs from tobytes'
    assert joined == writer.tobytes(), 'tobytes is not repeatable'
    writer.commit()
    writer.commit()             # an explicit second commit rewrites the same bytes
    assert path.read_bytes() == joined, 'committed file differs from tobytes'
    before_close = path.stat()
    writer.close()              # close() after commit() must NOT rewrite
    after_close = path.stat()
    assert (after_close.st_ino, after_close.st_mtime_ns) == \
        (before_close.st_ino, before_close.st_mtime_ns), \
        'close() rewrote an already committed file'
    assert path.read_bytes() == joined, 'close() changed the committed file'
    # A new file gets what a plain open() would have produced, not mkstemp's
    # 0600.  Compare against a file actually created that way rather than
    # against the module's own constant, which would pass whatever it said.
    reference = pathlib.Path(tmp) / 'reference-mode'
    with open(reference, 'wb') as handle:
        handle.write(b'')
    expected_mode = stat.S_IMODE(reference.stat().st_mode)
    assert stat.S_IMODE(path.stat().st_mode) == expected_mode, (
        f'new file mode {oct(stat.S_IMODE(path.stat().st_mode))} != '
        f'{oct(expected_mode)} that open() would have produced')

    # An existing target keeps its permissions and stays put on failure.
    path.chmod(0o644)
    before = path.read_bytes()
    failing = vlm_cfb.CfbWriter(str(path))
    fill(failing)
    original_iterbytes = failing.iterbytes

    def exploding():
        for index, chunk in enumerate(original_iterbytes()):
            if index > 1:
                raise RuntimeError('boom')
            yield chunk

    failing.iterbytes = exploding
    try:
        failing.commit()
        raise AssertionError('commit swallowed a write failure')
    except RuntimeError:
        pass
    assert path.read_bytes() == before, 'a failed commit damaged the existing file'
    leftovers = list(pathlib.Path(tmp).glob('.vlm-cfb-*'))
    assert not leftovers, f'temporary files left behind: {leftovers}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o644, \
        f'target mode changed to {oct(stat.S_IMODE(path.stat().st_mode))}'

    # Committing over an existing 0644 file must preserve that mode.
    again = vlm_cfb.CfbWriter(str(path))
    fill(again)
    again.commit()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644, \
        f'commit dropped the target mode to {oct(stat.S_IMODE(path.stat().st_mode))}'

    # A symlinked target is written through, not replaced.
    real = pathlib.Path(tmp) / 'real.vpx'
    link = pathlib.Path(tmp) / 'link.vpx'
    real.write_bytes(b'placeholder')
    link.symlink_to(real)
    through = vlm_cfb.CfbWriter(str(link))
    fill(through)
    through.commit()
    assert link.is_symlink(), 'commit replaced the symlink with a regular file'
    assert real.read_bytes() == joined, 'commit did not write through the symlink'


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
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL round trip: {error}')

        try:
            # Separate from the synthetic cases below: if rewrite() failed above,
            # this should report a failure rather than raise FileNotFoundError.
            nodes = check_directory_tree(rewritten)
            print(f'ok   directory: red-black tree and full ordering valid ({nodes} entries)')
        except (AssertionError, OSError) as error:
            failures += 1
            print(f'FAIL directory: {error}')

        try:
            test_synthetic_directories(tmp)
            test_name_edges(tmp)
            print('ok   synthetic: entry counts, stream sizes, name and duplicate limits')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL synthetic: {error}')

        try:
            test_mini_stream_cutoff(tmp)
            print('ok   mini stream: 4096 byte cutoff is strict')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL mini stream: {error}')

        try:
            n_fat, n_difat = test_large_file_difat(tmp)
            print(f'ok   DIFAT: {n_fat} FAT sectors over {n_difat} DIFAT sector(s)')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL DIFAT: {error}')

        try:
            count = test_custom_info_tags(tmp)
            print(f'ok   custom info tags: {count} tags hashed from TableInfo/<tag>')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL custom info tags: {error}')

        try:
            count = test_mac_file_structure(tmp)
            print(f'ok   MAC structure: {count} entries, modes and hashed flags verified')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL MAC structure: {error}')

        try:
            test_screenshot_hashed_raw(tmp)
            print('ok   screenshot: hashed as raw bytes, matching Visual Pinball')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL screenshot: {error}')

        try:
            test_commit_semantics(tmp)
            print('ok   commit: atomic, repeatable, keeps target mode and symlinks')
        except (AssertionError, OSError, ValueError, RuntimeError) as error:
            failures += 1
            print(f'FAIL commit: {error}')

        skipped = []
        try:
            result = check_vpxtool(table, rewritten)
            if result:
                print('ok   vpxtool: extractions identical')
            else:
                skipped.append('vpxtool (not on PATH)')
                print('skip vpxtool: not on PATH — the only strict reader check did NOT run')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL vpxtool: {error}')

        try:
            print(f'ok   MAC: recomputed digest matches GameStg/MAC ({check_mac(table).hex()})')
        except (AssertionError, OSError, ValueError) as error:
            failures += 1
            print(f'FAIL MAC: {error}')

    if failures:
        print(f'CFB: {failures} failure(s)')
    elif skipped:
        print(f"CFB: all tests passed, but SKIPPED {', '.join(skipped)}")
    else:
        print('CFB: all tests passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
