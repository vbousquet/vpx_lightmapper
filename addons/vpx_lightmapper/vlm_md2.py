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

"""Pure Python MD2 (RFC 1319).

Visual Pinball stores an MD2 digest of selected table streams in
``GameStg/MAC``.  MD2 is long obsolete and therefore absent from ``hashlib``;
on Windows the add-on used to reach for the CryptoAPI (``CALG_MD2``) through
pywin32, which made the exporter Windows only.  This module provides the same
digest everywhere, with a ``hashlib``-like API:

    h = vlm_md2.new()
    h.update(b'Visual Pinball')
    mac = h.digest()
"""

# RFC 1319, section 3.1: digits of pi used as a random byte permutation.
PI_SUBST = bytes((
     41,  46,  67, 201, 162, 216, 124,   1,  61,  54,  84, 161, 236, 240,   6,  19,
     98, 167,   5, 243, 192, 199, 115, 140, 152, 147,  43, 217, 188,  76, 130, 202,
     30, 155,  87,  60, 253, 212, 224,  22, 103,  66, 111,  24, 138,  23, 229,  18,
    190,  78, 196, 214, 218, 158, 222,  73, 160, 251, 245, 142, 187,  47, 238, 122,
    169, 104, 121, 145,  21, 178,   7,  63, 148, 194,  16, 137,  11,  34,  95,  33,
    128, 127,  93, 154,  90, 144,  50,  39,  53,  62, 204, 231, 191, 247, 151,   3,
    255,  25,  48, 179,  72, 165, 181, 209, 215,  94, 146,  42, 172,  86, 170, 198,
     79, 184,  56, 210, 150, 164, 125, 182, 118, 252, 107, 226, 156, 116,   4, 241,
     69, 157, 112,  89, 100, 113, 135,  32, 134,  91, 207, 101, 230,  45, 168,   2,
     27,  96,  37, 173, 174, 176, 185, 246,  28,  70,  97, 105,  52,  64, 126,  15,
     85,  71, 163,  35, 221,  81, 175,  58, 195,  92, 249, 206, 186, 197, 234,  38,
     44,  83,  13, 110, 133,  40, 132,   9, 211, 223, 205, 244,  65, 129,  77,  82,
    106, 220,  55, 200, 108, 193, 171, 250,  36, 225, 123,   8,  12, 189, 177,  74,
    120, 136, 149, 139, 227,  99, 232, 109, 233, 203, 213, 254,  59,   0,  29,  57,
    242, 239, 183,  14, 102,  88, 208, 228, 166, 119, 114, 248, 235, 117,  75,  10,
     49,  68,  80, 180, 143, 237,  31,  26, 219, 153, 141,  51, 159,  17, 131,  20,
))

_BLOCK = 16


class MD2:
    """Incremental MD2, mirroring the ``hashlib`` interface the exporter uses."""

    digest_size = 16
    block_size = _BLOCK
    name = 'md2'

    def __init__(self, data=b''):
        self._x = bytearray(48)
        self._checksum = bytearray(_BLOCK)
        self._buffer = bytearray()
        if data:
            self.update(data)

    def update(self, data):
        self._buffer += data
        n = len(self._buffer) - len(self._buffer) % _BLOCK
        for off in range(0, n, _BLOCK):
            self._process(self._buffer[off:off + _BLOCK])
        del self._buffer[:n]

    def _process(self, block):
        x = self._x
        x[16:32] = block
        for i in range(_BLOCK):
            x[32 + i] = x[i] ^ block[i]
        t = 0
        for i in range(18):
            for j in range(48):
                t = x[j] = x[j] ^ PI_SUBST[t]
            t = (t + i) & 0xFF
        # RFC 1319 section 3.2: the checksum runs over the same blocks, and its
        # last byte feeds back into the substitution index.
        checksum = self._checksum
        t = checksum[_BLOCK - 1]
        for i in range(_BLOCK):
            t = checksum[i] = checksum[i] ^ PI_SUBST[block[i] ^ t]

    def copy(self):
        clone = MD2()
        clone._x = bytearray(self._x)
        clone._checksum = bytearray(self._checksum)
        clone._buffer = bytearray(self._buffer)
        return clone

    def digest(self):
        # Finalisation is done on a copy so the object stays usable, like hashlib.
        clone = self.copy()
        pad = _BLOCK - len(clone._buffer)
        clone.update(bytes([pad]) * pad)
        clone._process(clone._checksum)
        return bytes(clone._x[:_BLOCK])

    def hexdigest(self):
        return self.digest().hex()


def new(data=b''):
    """Return a fresh MD2 object, optionally seeded with ``data``."""
    return MD2(data)
