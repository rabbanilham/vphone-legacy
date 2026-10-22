"""Update guest BSD ownership in a clean, UNMOUNTED raw HFS+/HFSX image.

Host mounts can map ownership to the host user. New launchd files nevertheless
need root:wheel inside iOS. Edit only the two catalog BSD IDs on an owned clone;
do not require host root, alter file content, or guess fixed catalog offsets.
Catalogs with overflow extents are deliberately unsupported.
"""
import struct


def set_catalog_owner(image, file_id, uid=0, gid=0):
    with open(image, "r+b") as f:
        f.seek(1024)
        header = f.read(512)
        if len(header) != 512 or header[:2] not in (b"H+", b"HX"):
            raise ValueError("Expected raw HFS+/HFSX image")
        attrs, block = struct.unpack_from(">I", header, 4)[0], struct.unpack_from(">I", header, 40)[0]
        if not attrs & 0x100:
            raise ValueError("HFS volume is not cleanly unmounted")
        if block < 512 or block & (block - 1):
            raise ValueError("Invalid allocation block size")
        extents = [struct.unpack_from(">II", header, 288 + i * 8) for i in range(8)]
        size = f.seek(0, 2)
        def physical(offset, count):
            for start, blocks in extents:
                length = blocks * block
                if offset >= length:
                    offset -= length
                    continue
                if offset + count > length or (start * block + offset + count) > size:
                    raise ValueError("Catalog access exceeds inline extent/image bounds")
                return start * block + offset
            raise ValueError("Catalog requires unsupported overflow extents")
        def read(offset, count):
            f.seek(physical(offset, count))
            data = f.read(count)
            if len(data) != count:
                raise ValueError("Truncated catalog")
            return data
        tree = read(0, 512)
        node_size = struct.unpack_from(">H", tree, 32)[0]
        if node_size < 512 or node_size > 32768 or node_size & (node_size - 1):
            raise ValueError("Invalid catalog node size")
        node = struct.unpack_from(">I", tree, 24)[0]
        visited, matches = set(), []
        while node:
            if node in visited or len(visited) > 100000:
                raise ValueError("Invalid catalog leaf chain")
            visited.add(node)
            data = read(node * node_size, node_size)
            count = struct.unpack_from(">H", data, 10)[0]
            if data[8] != 0xff or count > (node_size - 14) // 2:
                raise ValueError("Invalid catalog leaf")
            for i in range(count):
                record = struct.unpack_from(">H", data, node_size - 2 * (i + 1))[0]
                if record < 14 or record + 8 > node_size - 2 * (count + 1):
                    raise ValueError("Invalid catalog record offset")
                key_size = struct.unpack_from(">H", data, record)[0]
                value = record + 2 + key_size
                if key_size < 6 or value + 2 > node_size:
                    raise ValueError("Invalid catalog record bounds")
                kind = struct.unpack_from(">h", data, value)[0]
                if kind in (1, 2) and value + 48 > node_size:
                    raise ValueError("Truncated catalog BSD record")
                if kind in (1, 2) and struct.unpack_from(">I", data, value + 8)[0] == file_id:
                    chars = struct.unpack_from(">H", data, record + 6)[0]
                    name = data[record + 8:record + 8 + chars * 2].decode("utf-16-be")
                    offset = physical(node * node_size + value + 32, 8)
                    matches.append((offset, name, struct.unpack_from(">II", data, value + 32)))
            node = struct.unpack_from(">I", data, 0)[0]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one catalog ID {file_id}; found {len(matches)}")
        offset, name, before = matches[0]
        f.seek(offset)
        f.write(struct.pack(">II", uid, gid))
        return {"catalog_id": file_id, "name": name, "file_offset": offset,
                "before_uid_gid": list(before), "after_uid_gid": [uid, gid]}
