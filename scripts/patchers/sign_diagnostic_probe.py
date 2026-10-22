"""Minimal SHA-1 ad-hoc signing for the iOS 7 guest diagnostic client.

No Apple identity, CMS ticket, or activation record is created. The diagnostic
guest's native AMFI allow-any-signature policy is required. Entitlements grant
only the two native Lockdown GetValue reads used by this test.
"""
import hashlib
import plistlib
import struct

ENTITLEMENTS = {"com.apple.private.lockdown.finegrained-get":
                ["NULL/ActivationState", "NULL/BrickState"]}


def sign_probe(source):
    data = bytearray(source)
    if len(data) < 32 or struct.unpack_from("<II", data) != (0xfeedfacf, 0x100000c):
        raise ValueError("Expected ARM64 Mach-O probe")
    ncmds, size = struct.unpack_from("<II", data, 16)
    cursor, linkedit, first_section = 32, None, len(data)
    for _ in range(ncmds):
        cmd, count = struct.unpack_from("<II", data, cursor)
        if count < 8 or cursor + count > 32 + size:
            raise ValueError("Invalid load command")
        if cmd == 0x1d:
            raise ValueError("Probe is already signed")
        if cmd == 0x19:
            name = data[cursor + 8:cursor + 24].split(b"\0")[0]
            if name == b"__LINKEDIT":
                linkedit = cursor
            sections = struct.unpack_from("<I", data, cursor + 64)[0]
            for i in range(sections):
                offset = struct.unpack_from("<I", data, cursor + 72 + 80 * i + 48)[0]
                if offset:
                    first_section = min(first_section, offset)
        cursor += count
    if cursor != 32 + size or cursor + 16 > first_section or any(data[cursor:cursor + 16]):
        raise ValueError("No spare load-command space for diagnostic signature")
    if linkedit is None:
        raise ValueError("Probe has no LINKEDIT segment")
    signature_offset = (len(data) + 15) & ~15
    data.extend(bytes(signature_offset - len(data)))
    identifier = b"org.vphone.virtual-activation-probe\0"
    xml = plistlib.dumps(ENTITLEMENTS, fmt=plistlib.FMT_XML, sort_keys=True)
    ent = struct.pack(">II", 0xfade7171, 8 + len(xml)) + xml
    slots = (signature_offset + 4095) // 4096
    hash_offset = 48 + len(identifier) + 5 * 20
    cd_size = hash_offset + slots * 20
    signature_size = 28 + cd_size + len(ent)
    struct.pack_into("<IIII", data, cursor, 0x1d, 16, signature_offset, signature_size)
    struct.pack_into("<II", data, 16, ncmds + 1, size + 16)
    fileoff = struct.unpack_from("<Q", data, linkedit + 40)[0]
    end = signature_offset + signature_size
    struct.pack_into("<Q", data, linkedit + 32, (end - fileoff + 0x3fff) & ~0x3fff)
    struct.pack_into("<Q", data, linkedit + 48, end - fileoff)
    # CodeDirectory v0x20100, CS_ADHOC, SHA-1, 4 KiB signing pages.
    cd = bytearray(struct.pack(">9I4B2I", 0xfade0c02, cd_size, 0x20100, 2,
                               hash_offset, 48, 5, slots, signature_offset,
                               20, 1, 0, 12, 0, 0))
    cd.extend(identifier)
    cd.extend(hashlib.sha1(ent).digest() + bytes(4 * 20))
    for offset in range(0, signature_offset, 4096):
        cd.extend(hashlib.sha1(data[offset:min(offset + 4096, signature_offset)]).digest())
    assert len(cd) == cd_size
    superblob = struct.pack(">7I", 0xfade0cc0, signature_size, 2,
                            0, 28, 5, 28 + cd_size)
    data.extend(superblob + cd + ent)
    return bytes(data)
