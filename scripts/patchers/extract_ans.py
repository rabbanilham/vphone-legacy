"""Extract the byte-exact ANS payload embedded in the verified n53ap iBoot.

This is firmware validation, not coprocessor initialization. Native iBoot's
runtime CisC fixups and A7IOP execution must be implemented separately.
"""
import hashlib
import struct

from patchers.patch_kernel import decrypt_im4p

IBOOT_SHA256 = "05b1a19952c119bfbc7e58868e484252f593efd616a16317eb4fd2c501400d81"
ANS_SHA256 = "c066bf9d1edef96e59fe618eb30933847ba00ac82bff2b8a7b3ef0b7a32c9b7e"
IBOOT_BASE = 0x83d7f7000


def extract_ans_payload(iboot: bytes) -> tuple[bytes, dict]:
    """Use the audited provider chain in this exact firmware, failing closed."""
    if len(iboot) != 0x6e000 or hashlib.sha256(iboot).hexdigest() != IBOOT_SHA256:
        raise ValueError("Unsupported iBoot content; ANS provider contract is verified only for n53ap 11D257")
    provider = struct.unpack_from("<Q", iboot, 0x333d8)[0] - IBOOT_BASE
    if provider != 0x333f8:
        raise ValueError("Native ANS provider table mismatch")
    getters = [struct.unpack_from("<Q", iboot, provider + offset)[0] - IBOOT_BASE
               for offset in (0x18, 0x20, 0x28)]
    if getters != [0x711c, 0x7110, 0x7100]:
        raise ValueError("Native ANS source/size/config getter mismatch")
    # These constants are returned by the verified getter instructions. They
    # are not recovered by searching arbitrary embedded string signatures.
    source, size = 0x35000, 0x39000
    config_offset = struct.unpack_from("<I", iboot, 0x33a58)[0]
    if source + size != len(iboot) or config_offset + 0x54 > size:
        raise ValueError("Native ANS source or config extent exceeds iBoot")
    blob = iboot[source:source + size]
    magic, version, memory_size, alignment = struct.unpack_from("<4sIII", blob, config_offset)
    shared_size = struct.unpack_from("<I", blob, config_offset + 0x14)[0]
    shared_count = struct.unpack_from("<Q", blob, config_offset + 0x18)[0]
    if (magic, version, memory_size, alignment, shared_size, shared_count) != (
            b"CisC", 2, 0x982f0, 0x1000, 0x400000, 2):
        raise ValueError("Native ANS CisC configuration mismatch")
    if hashlib.sha256(blob).hexdigest() != ANS_SHA256:
        raise ValueError("Native ANS payload checksum mismatch")
    return blob, {
        "source_iboot_sha256": IBOOT_SHA256, "source_offset": source, "source_size": size,
        "provider_table_offset": provider, "config_offset": config_offset,
        "name": "s5l8960xans", "version": "AppleStorageProcessor-378.5.46",
        "native_region_base": 0x83f800000, "native_region_size": 0x800000,
        "config_version": version, "firmware_memory_size": memory_size,
        "alignment": alignment, "shared_memory_requested": shared_size,
        "shared_region_count": shared_count,
        "runtime_fixups_applied": False, "execution_verified": False,
        "required_fixups": ["chip_id", "chip_revision", "clock_table_entries_0_and_41",
                            "shared_region_offset_and_size", "secondary_shared_region_clear",
                            "boot_policy_debug_flag"],
    }


def decode_ans_components(data: bytes, iv: str, key: str) -> tuple[bytes, bytes, dict]:
    iboot = decrypt_im4p(data, b"ibot", iv, key)
    blob, metadata = extract_ans_payload(iboot)
    return iboot, blob, metadata
