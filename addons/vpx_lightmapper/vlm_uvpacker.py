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
import os
import struct
import bmesh
import subprocess

# This bridge is derived from UVPacker addon https://www.uv-packer.com/blender/ (GPL v3)
# Implemented here as a basic blocking implementation

def uvpacker_pack(meshes, padding, width, height):
    packerDir = os.path.dirname(os.path.realpath(__file__))
    packerExe = packerDir + "\\UV-Packer-Blender.exe"
    process = None
    try:
        process = subprocess.Popen([packerExe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=False)
    except:
        print(f'ERROR: UV-Packer executable not found. Please copy UV-Packer-Blender.exe to: {packerDir}')
        return {"FINISHED"}
    numObjects = len(meshes)
    if numObjects == 0:
        return {"FINISHED"}
    binaryData = bytearray()
    binaryData += (1).to_bytes(4, byteorder="little") # Plugin version (from officiel plugin)
    binaryData += (1).to_bytes(4, byteorder="little") # Plugin version (from officiel plugin)
    binaryData += (0).to_bytes(4, byteorder="little") # Plugin version (from officiel plugin)
    binaryData += (1).to_bytes(4, byteorder="little") # PackMode: 0 (fast) or 1 (best)
    binaryData += (width).to_bytes(4, byteorder="little") # Width
    binaryData += (height).to_bytes(4, byteorder="little") # Height
    binaryData += bytearray(struct.pack("<d", padding)) # Padding
    binaryData += bytearray(struct.pack("<?", True)) # Combine
    binaryData += bytearray(struct.pack("<?", False)) # Rescale
    binaryData += bytearray(struct.pack("<?", True)) # Pre rotate
    binaryData += bytearray(struct.pack("<?", True)) # Use full rotation
    binaryData += (1).to_bytes(4, byteorder="little") # Rotate string "0" None / "1" 90, "2" 45, "3" 23
    binaryData += (1).to_bytes(4, byteorder="little") # Tiles X
    binaryData += (1).to_bytes(4, byteorder="little") # Tiles Y
    binaryData += (numObjects).to_bytes(4, byteorder="little")
    for object_idx, obj in enumerate(meshes):
        binaryData += (object_idx).to_bytes(4, byteorder="little")
        bm = bmesh.from_edit_mesh(obj.data)
        bm.normal_update()
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.verify()
        nameBytes = obj.name.encode()
        binaryData += (len(nameBytes)).to_bytes(4, byteorder="little")
        binaryData.extend(nameBytes)
        binaryData += (len(bm.verts)).to_bytes(4, byteorder="little")
        for vert in bm.verts:
            binaryData += bytearray(struct.pack("<ddd", vert.co.x, vert.co.y, vert.co.z))
        indexCount = 0
        binaryData += (len(bm.faces)).to_bytes(4, byteorder="little")
        for i, face in enumerate(bm.faces):
            binaryData += (len(face.loops)).to_bytes(4, byteorder="little")
            for loop in face.loops:
                vert = loop.vert
                binaryData += (vert.index).to_bytes(4, byteorder="little")
                binaryData += bytearray(struct.pack("<ddd", vert.normal.x, vert.normal.y, vert.normal.z))
                uv_coord = loop[uv_layer].uv
                isPinned = loop[uv_layer].pin_uv
                binaryData += bytearray(struct.pack("<dd", uv_coord.x, uv_coord.y))
                binaryData += bytearray(struct.pack("<?", isPinned))
                binaryData += (indexCount).to_bytes(4, byteorder="little")
                indexCount += 1
    sumBytes = len(binaryData)
    binaryData = sumBytes.to_bytes(4, byteorder="little") + binaryData
    try:
        out_stream = process.stdin
        out_stream.write(binaryData)
        out_stream.flush()
        message = ""
        while True:
            messageSize = struct.unpack("<I", process.stdout.read(4))[0]
            message = process.stdout.read(messageSize)
            readPtr = 0
            messageType = struct.unpack_from("<I", message, readPtr)[0]
            readPtr += 4
            if messageType == 0: # success
                break
            elif messageType == 1: # progress
                print(f'. UVPacker progress {struct.unpack_from("<d", message, readPtr)[0]}')
            elif messageType == 2: # error
                msgSize = struct.unpack_from("<I", message, readPtr)[0]
                readPtr += 4
                print(f'. UVPacker error: {message[readPtr:readPtr+msgSize].decode()}')
                return {"FINISHED"}
            else:
                print("Error: unsupported message " + str(messageType))
        numObjects = struct.unpack_from("<I", message, readPtr)[0]
        readPtr += 4
        for obj in range(0, numObjects):
            objId = struct.unpack_from("<I", message, readPtr)[0]
            readPtr += 4
            nameSize = struct.unpack_from("<I", message, readPtr)[0]
            readPtr += 4
            objName = message[readPtr:readPtr+nameSize].decode()
            readPtr += nameSize
            obj = meshes[objId]
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            uv_layer = bm.loops.layers.uv.verify()
            faces = [f for f in bm.faces]
            numResultVerts = struct.unpack_from("<I", message, readPtr)[0]
            readPtr += 4
            for face in faces:
                for loop in face.loops:
                    x = struct.unpack_from("<d", message, readPtr)[0]
                    readPtr += 8
                    y = struct.unpack_from("<d", message, readPtr)[0]
                    readPtr += 8
                    loop[uv_layer].uv = [x, y]
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        coverage = struct.unpack_from("<d", message, readPtr)[0]
        print(f'. UVPacker packing complete {coverage}')
    except:
        return {"FINISHED"}

    return {"FINISHED"}
