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

import struct


class BIFF_reader:
    def __init__(self, stream):
        self.data = stream
        self.pos = 0
        self.bytes_in_record_remaining = 0
        self.tag = b''
    
    def is_eof(self):
        return self.pos >= len(self.data) or self.tag == 'ENDB'
    
    def get(self, count):
        p = self.pos
        self.pos = self.pos + count
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - count
        return self.data[p:p+count]

    def get_bool(self):
        self.pos = self.pos + 1
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 1
        return self.data[self.pos - 1] != 0

    def get_bool_padded(self):
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4
        return self.data[self.pos - 4] != 0

    def get_u8(self):
        i = struct.unpack("<B", self.data[self.pos:self.pos+1])[0]
        self.pos = self.pos + 1
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 1
        return i

    def get_u16(self):
        i = struct.unpack("<H", self.data[self.pos:self.pos+2])[0]
        self.pos = self.pos + 2
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 2
        return i

    def get_u32(self):
        i = struct.unpack("<I", self.data[self.pos:self.pos+4])[0]
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4
        return i

    def get_32(self):
        i = struct.unpack("<i", self.data[self.pos:self.pos+4])[0]
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4
        return i

    def get_float(self):
        i = struct.unpack("<f", self.data[self.pos:self.pos+4])[0]
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4
        return i

    def get_str(self, count):
        pos_0 = count
        for p in range(count):
            if self.data[self.pos+p] == 0:
                pos_0 = p
                break
        i = str(struct.unpack("%ds" % pos_0, self.data[self.pos:self.pos+pos_0])[0], 'latin_1')
        self.pos = self.pos + count
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - count
        return i
    
    def get_string(self):
        return self.get_str(self.get_32())
    
    def get_wide_string(self):
        count = self.get_32()
        i = str(self.data[self.pos:self.pos+count], 'utf-16')
        self.pos = self.pos + count
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - count
        return i
    
    def get_color(self, has_alpha=False):
        if has_alpha:
            i = (self.get_u8() / 255.0, self.get_u8() / 255.0, self.get_u8() / 255.0, self.get_u8() / 255.0)
        else:
            i = (self.get_u8() / 255.0, self.get_u8() / 255.0, self.get_u8() / 255.0, 1)
            self.get_u8()
        return i
    
    def skip(self, count):
        self.pos = self.pos + count
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - count

    def skip_tag(self):
        self.pos = self.pos + self.bytes_in_record_remaining
        self.bytes_in_record_remaining = 0

    def next(self):
        if self.bytes_in_record_remaining > 0:
            print(f"{self.tag} : {self.bytes_in_record_remaining} unread octets")
            self.skip(self.bytes_in_record_remaining)
        self.bytes_in_record_remaining = self.get_u32()
        self.tag = self.get_str(4)
        
    def child_reader(self):
        return BIFF_reader(self.data[self.pos:])
