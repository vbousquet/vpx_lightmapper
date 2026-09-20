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

"""Pure Python writer for MS-CFB compound files (the container of a .vpx table).

Reading is handled by ``olefile``; writing used to go through the Windows COM
``IStorage``/``IStream`` API, which is why exporting only worked on Windows.
This module writes version 3 compound files (512 byte sectors, 64 byte mini
sectors, 4096 byte mini stream cutoff) from scratch, so export works on Linux
and macOS as well.

The API deliberately mirrors the small subset of ``IStorage`` the exporter
uses, so the COM path can stay in place on Windows:

    writer = CfbWriter(path)
    gamestg = writer.create_storage('GameStg')
    stream = gamestg.create_stream('Version')
    stream.write(data)
    writer.commit()
    writer.close()

Reference: [MS-CFB] Compound File Binary File Format.
"""

import os
import struct
import tempfile

HEADER_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

MAXREGSECT = 0xFFFFFFFA
DIFSECT = 0xFFFFFFFC
FATSECT = 0xFFFFFFFD
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF

SECTOR_SIZE = 512
MINI_SECTOR_SIZE = 64
MINI_STREAM_CUTOFF = 4096

DIFAT_IN_HEADER = 109
FAT_ENTRIES_PER_SECTOR = SECTOR_SIZE // 4          # 128
DIFAT_ENTRIES_PER_SECTOR = FAT_ENTRIES_PER_SECTOR - 1  # 127, last slot chains
DIR_ENTRY_SIZE = 128
DIR_ENTRIES_PER_SECTOR = SECTOR_SIZE // DIR_ENTRY_SIZE  # 4

TYPE_STORAGE = 1
TYPE_STREAM = 2
TYPE_ROOT = 5
COLOR_RED = 0
COLOR_BLACK = 1

MAX_NAME_LEN = 31  # 32 UTF-16 code units including the null terminator


def _entry_sort_key(name):
    """Directory sibling ordering defined by [MS-CFB] 2.6.4.

    Shorter names sort first; names of equal length compare by upper-cased
    UTF-16 code unit.  Strict readers (the Rust ``cfb`` crate used by vpxtool,
    and Windows ``StgOpenStorage``) reject files that break this rule.
    """
    units = name.encode('utf-16-le')
    # [MS-CFB] specifies a simple one to one uppercase mapping.  Python's
    # str.upper() expands some characters ('ss' for a sharp s, 'FI' for a
    # ligature), which would change the code unit sequence, so only fold
    # characters whose uppercase is a single code point.
    upper = ''.join(c.upper() if len(c.upper()) == 1 else c for c in name)
    upper_units = upper.encode('utf-16-le')
    return (len(units) // 2, struct.unpack(f'<{len(upper_units) // 2}H', upper_units))


class Stream:
    """A stream being built in memory; flushed to disk on commit."""

    def __init__(self, entry):
        self._entry = entry

    def write(self, data):
        # bytearray, so chunked writes stay linear rather than rebuilding the
        # payload on every call.
        self._entry.data.extend(data)
        return len(data)

    def close(self):
        pass

    # COM compatible aliases, so the exporter can drive either backend.
    Write = write
    Close = close


class Storage:
    """A storage (directory) node."""

    def __init__(self, entry, writer):
        self._entry = entry
        self._writer = writer

    def create_storage(self, name, *args, **kwargs):
        return Storage(self._writer._add_entry(self._entry, name, TYPE_STORAGE), self._writer)

    def create_stream(self, name, *args, **kwargs):
        return Stream(self._writer._add_entry(self._entry, name, TYPE_STREAM))

    def commit(self, *args, **kwargs):
        """Present for API parity; the whole file is written by CfbWriter.commit."""

    CreateStorage = create_storage
    CreateStream = create_stream
    Commit = commit


class _Entry:
    __slots__ = ('name', 'type', 'children', 'data', 'id', 'color', 'left', 'right', 'child', 'start', 'size')

    def __init__(self, name, type_):
        # The 64 byte name field holds 32 UTF-16 code units including the
        # terminator.  Count code units, not code points: a non BMP character
        # is a surrogate pair and takes two of them.
        units = len(name.encode('utf-16-le')) // 2
        if units > MAX_NAME_LEN:
            raise ValueError(f'Compound file entry name too long ({units} > {MAX_NAME_LEN} UTF-16 code units): {name!r}')
        self.name = name
        self.type = type_
        self.children = []
        self.data = bytearray()
        self.id = 0
        self.color = COLOR_BLACK
        self.left = NOSTREAM
        self.right = NOSTREAM
        self.child = NOSTREAM
        # [MS-CFB] 2.6.1: a storage object's start sector and size MUST be zero.
        self.start = ENDOFCHAIN if type_ == TYPE_STREAM else 0
        self.size = 0


class CfbWriter(Storage):
    """Builds a compound file in memory and writes it out on ``commit()``."""

    def __init__(self, path):
        self._path = path
        self._root = _Entry('Root Entry', TYPE_ROOT)
        self._names = {id(self._root): set()}
        self._committed = False
        Storage.__init__(self, self._root, self)

    def _add_entry(self, parent, name, type_):
        siblings = self._names.setdefault(id(parent), set())
        # Siblings are ordered, and compared, case insensitively: 'Foo' and
        # 'FOO' collide.  Keying the check on the name alone would let both in
        # and produce a tree that is not a valid search tree under the CFB
        # comparator, which a binary searching reader can fail to walk.
        key = _entry_sort_key(name)
        if key in siblings:
            raise ValueError(f'Duplicate entry {name!r} in compound file storage {parent.name!r}')
        siblings.add(key)
        entry = _Entry(name, type_)
        parent.children.append(entry)
        return entry

    def commit(self, *args, **kwargs):
        # Write to a temporary file next to the target and rename it into
        # place, so a failure part way through leaves the previous file intact
        # rather than a truncated one (the COM backend got this from
        # STGM_TRANSACTED).
        directory = os.path.dirname(os.path.abspath(self._path)) or '.'
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.vlm-cfb-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                for chunk in self.iterbytes():
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._committed = True

    def close(self):
        if not self._committed:
            self.commit()

    Commit = commit
    Close = close

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.close()
        return False

    # ------------------------------------------------------------------ layout

    def _build_directory(self):
        """Assign directory ids and link each storage's children as a red-black tree."""
        entries = [self._root]
        # Breadth-first walk, so directory ids follow creation order.
        queue = [self._root]
        while queue:
            node = queue.pop(0)
            for child in node.children:
                child.id = len(entries)
                entries.append(child)
                queue.append(child)

        for entry in entries:
            entry.child = self._link_children(entry.children)
        return entries

    @staticmethod
    def _link_children(children):
        """Lay siblings out as a complete BST and colour it as a valid red-black tree.

        Sorted entries are dropped into a heap-shaped (complete) binary tree by
        in-order position.  Every node is black except, when the bottom level is
        only partly filled, that bottom level -- whose nodes are then all leaves.
        Red nodes therefore never touch and every root-to-leaf path crosses the
        same number of black nodes, which is exactly the red-black invariant.
        """
        n = len(children)
        if n == 0:
            return NOSTREAM
        ordered = sorted(children, key=lambda e: _entry_sort_key(e.name))

        positions = []
        stack = [(1, False)]
        while stack:  # iterative in-order over heap indices 1..n
            index, visited = stack.pop()
            if index > n:
                continue
            if visited:
                positions.append(index)
            else:
                stack.append((2 * index + 1, False))
                stack.append((index, True))
                stack.append((2 * index, False))

        by_index = {index: ordered[i] for i, index in enumerate(positions)}
        max_depth = n.bit_length() - 1
        bottom_level_full = n == (1 << (max_depth + 1)) - 1
        for index, entry in by_index.items():
            depth = index.bit_length() - 1
            entry.color = COLOR_RED if (not bottom_level_full and depth == max_depth) else COLOR_BLACK
            entry.left = by_index[2 * index].id if 2 * index <= n else NOSTREAM
            entry.right = by_index[2 * index + 1].id if 2 * index + 1 <= n else NOSTREAM
        return by_index[1].id

    def tobytes(self):
        """The whole file as one bytes object (tests and small files)."""
        return b''.join(self.iterbytes())

    def iterbytes(self):
        """Yield the file in order: header, data sectors, FAT, DIFAT.

        Yielding rather than joining keeps a second full size copy of the file
        out of memory, and each data sector is released as soon as it has been
        handed over, so the caller can stream a large table to disk.
        """
        entries = self._build_directory()

        sectors = []  # 512 byte payloads, index == sector number
        fat = []      # fat[i] == next sector of the chain holding sector i

        def alloc(data, sector_size=SECTOR_SIZE):
            """Append data as a new sector chain, return its first sector number."""
            if not data:
                return ENDOFCHAIN
            first = len(sectors)
            count = (len(data) + sector_size - 1) // sector_size
            for i in range(count):
                chunk = data[i * sector_size:(i + 1) * sector_size]
                sectors.append(chunk.ljust(sector_size, b'\0'))
                fat.append(len(sectors) if i + 1 < count else ENDOFCHAIN)
            return first

        # Streams below the cutoff live in the mini stream, which is itself a
        # regular stream hanging off the root entry.
        mini_stream = bytearray()
        mini_fat = []
        for entry in entries:
            if entry.type != TYPE_STREAM:
                continue
            entry.size = len(entry.data)
            if entry.size == 0:
                entry.start = ENDOFCHAIN
            elif entry.size < MINI_STREAM_CUTOFF:
                entry.start = len(mini_stream) // MINI_SECTOR_SIZE
                count = (entry.size + MINI_SECTOR_SIZE - 1) // MINI_SECTOR_SIZE
                base = len(mini_fat)
                for i in range(count):
                    mini_fat.append(base + i + 1 if i + 1 < count else ENDOFCHAIN)
                mini_stream += entry.data
                mini_stream += b'\0' * (count * MINI_SECTOR_SIZE - entry.size)

        for entry in entries:
            if entry.type == TYPE_STREAM and entry.size >= MINI_STREAM_CUTOFF:
                entry.start = alloc(entry.data)

        self._root.size = len(mini_stream)
        self._root.start = alloc(bytes(mini_stream))

        mini_fat_data = struct.pack(f'<{len(mini_fat)}I', *mini_fat) if mini_fat else b''
        # Unused slots in the last MiniFAT sector must read as FREESECT.
        if mini_fat_data:
            pad = (-len(mini_fat_data)) % SECTOR_SIZE
            mini_fat_data += b'\xff' * pad
        first_mini_fat = alloc(mini_fat_data)
        n_mini_fat = len(mini_fat_data) // SECTOR_SIZE

        dir_data = b''.join(self._pack_entry(e) for e in entries)
        pad = (-len(dir_data)) % SECTOR_SIZE
        # Unallocated directory entries are zeroed but for their sibling ids.
        dir_data += self._pack_unused() * (pad // DIR_ENTRY_SIZE)
        first_dir = alloc(dir_data)

        # The FAT (and any DIFAT sectors) occupy sectors too, so solve for the
        # smallest self-consistent count.
        n_data = len(sectors)
        n_fat = n_difat = 0
        while True:
            total = n_data + n_fat + n_difat
            new_fat = max(1, -(-total // FAT_ENTRIES_PER_SECTOR))
            new_difat = 0 if new_fat <= DIFAT_IN_HEADER else -(-(new_fat - DIFAT_IN_HEADER) // DIFAT_ENTRIES_PER_SECTOR)
            if (new_fat, new_difat) == (n_fat, n_difat):
                break
            n_fat, n_difat = new_fat, new_difat

        fat_sectors = list(range(n_data, n_data + n_fat))
        difat_sectors = list(range(n_data + n_fat, n_data + n_fat + n_difat))
        fat.extend([FATSECT] * n_fat)
        fat.extend([DIFSECT] * n_difat)
        fat.extend([FREESECT] * (n_fat * FAT_ENTRIES_PER_SECTOR - len(fat)))
        fat_data = struct.pack(f'<{len(fat)}I', *fat)

        difat_data = b''
        for i, sector in enumerate(difat_sectors):
            block = fat_sectors[DIFAT_IN_HEADER + i * DIFAT_ENTRIES_PER_SECTOR:
                                DIFAT_IN_HEADER + (i + 1) * DIFAT_ENTRIES_PER_SECTOR]
            block = list(block) + [FREESECT] * (DIFAT_ENTRIES_PER_SECTOR - len(block))
            block.append(difat_sectors[i + 1] if i + 1 < n_difat else ENDOFCHAIN)
            difat_data += struct.pack(f'<{FAT_ENTRIES_PER_SECTOR}I', *block)

        header = self._pack_header(
            n_fat=n_fat, first_dir=first_dir,
            first_mini_fat=first_mini_fat, n_mini_fat=n_mini_fat,
            first_difat=difat_sectors[0] if difat_sectors else ENDOFCHAIN, n_difat=n_difat,
            header_difat=fat_sectors[:DIFAT_IN_HEADER])

        yield header
        for i in range(len(sectors)):
            chunk = sectors[i]
            sectors[i] = None  # release as we go
            yield chunk
        yield fat_data
        yield difat_data

    @staticmethod
    def _pack_header(n_fat, first_dir, first_mini_fat, n_mini_fat, first_difat, n_difat, header_difat):
        difat = list(header_difat) + [FREESECT] * (DIFAT_IN_HEADER - len(header_difat))
        return struct.pack(
            '<8s16sHHHHH6xIIIIIIIII436s',
            HEADER_SIGNATURE,
            b'\0' * 16,       # header CLSID, must be zero
            0x003E,           # minor version
            3,                # major version (512 byte sectors)
            0xFFFE,           # little endian
            9,                # sector shift: 1 << 9 == 512
            6,                # mini sector shift: 1 << 6 == 64
            0,                # directory sector count, unused in v3
            n_fat,
            first_dir,
            0,                # transaction signature
            MINI_STREAM_CUTOFF,
            first_mini_fat,
            n_mini_fat,
            first_difat,
            n_difat,
            struct.pack(f'<{DIFAT_IN_HEADER}I', *difat),
        )

    @staticmethod
    def _pack_entry(entry):
        name = entry.name.encode('utf-16-le')
        return struct.pack(
            '<64sHBBIII16sIQQIQ',
            name.ljust(64, b'\0'),
            len(name) + 2,     # name length in bytes, including the null terminator
            entry.type,
            entry.color,
            entry.left,
            entry.right,
            entry.child,
            b'\0' * 16,        # CLSID
            0,                 # state bits
            0, 0,              # creation / modification time, left unset
            entry.start,
            entry.size,
        )

    @staticmethod
    def _pack_unused():
        return struct.pack('<64sHBBIII16sIQQIQ', b'\0' * 64, 0, 0, COLOR_RED,
                           NOSTREAM, NOSTREAM, NOSTREAM, b'\0' * 16, 0, 0, 0, 0, 0)


# --------------------------------------------------------------------- backends

try:  # pywin32 is optional and Windows only
    import pythoncom
    from win32com import storagecon
    _HAS_COM = True
except ImportError:
    _HAS_COM = False

_COM_CHILD_FLAGS = None
if _HAS_COM:
    _COM_CHILD_FLAGS = (storagecon.STGM_DIRECT | storagecon.STGM_READWRITE
                        | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE)


class ComStream:
    """Thin wrapper giving a COM IStream the same API as Stream."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        return self._stream.Write(data)

    def close(self):
        pass


class ComStorage:
    """Thin wrapper giving a COM IStorage the same API as Storage/CfbWriter."""

    def __init__(self, storage):
        self._storage = storage

    def create_storage(self, name):
        return ComStorage(self._storage.CreateStorage(name, _COM_CHILD_FLAGS, 0, 0))

    def create_stream(self, name):
        return ComStream(self._storage.CreateStream(name, _COM_CHILD_FLAGS, 0, 0))

    def commit(self):
        self._storage.Commit(storagecon.STGC_DEFAULT)

    def close(self):
        pass


def create_writer(path):
    """Return a compound file writer for ``path``.

    On Windows with pywin32 installed this keeps using the COM implementation,
    so files written there are bit for bit what previous releases produced.
    Everywhere else (and when ``VLM_PURE_PYTHON_CFB`` is set, which is handy for
    checking the two backends against each other) the pure Python writer is used.
    """
    if _HAS_COM and os.name == 'nt' and not os.environ.get('VLM_PURE_PYTHON_CFB'):
        flags = (storagecon.STGM_TRANSACTED | storagecon.STGM_READWRITE
                 | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE)
        return ComStorage(pythoncom.StgCreateStorageEx(
            path, flags, storagecon.STGFMT_DOCFILE, 0, pythoncom.IID_IStorage, None, None))
    return CfbWriter(path)
