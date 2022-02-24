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

import io
import struct


class BIFF_reader:
    def __init__(self, stream):
        self.data = stream
        self.pos = 0
        self.record_start = 0
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
        return self.get_str(self.get_u32())
    
    def get_wide_string(self):
        count = self.get_u32()
        i = str(self.data[self.pos:self.pos+count], 'utf-16-le')
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
    
    def get_record_data(self, with_tag=False):
        if with_tag:
            d = self.data[self.pos-4:self.pos + self.bytes_in_record_remaining]
        else:
            d = self.data[self.pos:self.pos + self.bytes_in_record_remaining]
        self.pos = self.pos + self.bytes_in_record_remaining
        self.bytes_in_record_remaining = 0
        return d
    
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
        self.record_start = self.pos
        self.bytes_in_record_remaining = self.get_u32()
        self.tag = self.get_str(4)
    
    def delete_tag(self):
        self.data = self.data[:self.record_start] + self.data[self.pos + self.bytes_in_record_remaining:]
        self.pos = self.record_start
        self.bytes_in_record_remaining = 0
        self.tag = b''
    
    def delete_bytes(self, count):
        self.data = self.data[:self.pos] + self.data[self.pos + count:]
    
    def insert_data(self, new_data):
        for d in new_data:
            self.data.insert(self.pos, d)
            self.pos += 1
    
    def put_bool(self, value):
        if value:
            self.data[self.pos] = 0xFF
        else:
            self.data[self.pos] = 0
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4

    def put_u32(self, value):
        self.data[self.pos:self.pos+4] = struct.pack("<I", value)
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4

    def put_float(self, value):
        self.data[self.pos:self.pos+4] = struct.pack("<f", value)
        self.pos = self.pos + 4
        self.bytes_in_record_remaining = self.bytes_in_record_remaining - 4

    def child_reader(self):
        return BIFF_reader(self.data[self.pos:])


class BIFF_writer:
    def __init__(self):
        self.data = io.BytesIO()
        self.tag_start = 0
        self.tag = b''
        self.record_size = 0

    def get_data(self):
        return self.data.getvalue()

    def end_tag(self):
        if self.tag != b'':
            length = self.data.tell()
            self.data.seek(self.tag_start)
            self.data.write(struct.pack("<I", self.record_size))
            self.data.seek(length)
        self.tag = b''
        
    def new_tag(self, tag):
        self.end_tag()
        self.tag_start = self.data.tell()
        self.data.write(b'0000') # placeholder for record size
        self.data.write(tag)
        self.tag = tag
        self.record_size = 4

    def write_u32(self, value):
        self.record_size = self.record_size + 4
        self.data.write(struct.pack("<I", value))

    def write_32(self, value):
        self.record_size = self.record_size + 4
        self.data.write(struct.pack("<i", value))

    def write_float(self, value):
        self.record_size = self.record_size + 4
        self.data.write(struct.pack("<f", value))
        
    def write_string(self, value):
        d = value.encode('latin_1')
        self.write_u32(len(d))
        self.write_data(d)
        
    def write_wide_string(self, value):
        d = value.encode('utf-16-le')
        self.write_u32(len(d))
        self.write_data(d)
        
    def write_bool(self, value):
        if value:
            self.write_u32(0xFFFFFFFF)
        else:
            self.write_u32(0x00000000)

    def write_data(self, value):
        self.record_size = self.record_size + len(value)
        self.data.write(value)

    def write_tagged_empty(self, tag):
        self.new_tag(tag)
        self.end_tag()

    def write_tagged_bool(self, tag, value):
        self.new_tag(tag)
        self.write_bool(value)
        self.end_tag()

    def write_tagged_float(self, tag, value):
        self.new_tag(tag)
        self.write_float(value)
        self.end_tag()

    def write_tagged_u32(self, tag, value):
        self.new_tag(tag)
        self.write_u32(value)
        self.end_tag()

    def write_tagged_32(self, tag, value):
        self.new_tag(tag)
        self.write_32(value)
        self.end_tag()

    def write_tagged_string(self, tag, value):
        self.new_tag(tag)
        self.write_string(value)
        self.end_tag()
        
    def write_tagged_wide_string(self, tag, value):
        self.new_tag(tag)
        self.write_wide_string(value)
        self.end_tag()
    
    def write_tagged_vec2(self, tag, x, y):
        self.new_tag(tag)
        self.write_float(x)
        self.write_float(y)
        self.end_tag()

    def write_tagged_padded_vector(self, tag, x, y, z):
        self.new_tag(tag)
        self.write_float(x)
        self.write_float(y)
        self.write_float(z)
        self.write_float(0.0)
        self.end_tag()

    def write_tagged_data(self, tag, value):
        self.new_tag(tag)
        self.write_data(value)
        self.end_tag()

    def close(self, write_endb=True):
        if write_endb:
            self.new_tag(b'ENDB')
        self.end_tag()
