"""Read a named PE32+ export RVA for native integration tests, using stdlib only."""

from pathlib import Path
import struct


def exported_rva(path: Path, exact_name: str) -> int:
    """Return an exact named export's RVA, rejecting missing or forwarded exports.

    Export tables and strings must be backed by file bytes. The exported target
    itself may be zero-initialized data in a section's virtual tail.
    """
    if not exact_name or "\0" in exact_name:
        raise ValueError("Export name must be nonempty and contain no NUL.")
    try:
        wanted = exact_name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("PE export names must be ASCII.") from exc

    data = Path(path).read_bytes()

    def unpack(fmt: str, offset: int):
        size = struct.calcsize(fmt)
        if offset < 0 or offset > len(data) - size:
            raise ValueError("Truncated PE structure.")
        return struct.unpack_from(fmt, data, offset)

    if data[:2] != b"MZ":
        raise ValueError("Missing DOS MZ header.")
    pe, = unpack("<I", 0x3C)
    if pe < 0x40 or data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Missing PE signature.")
    section_count, = unpack("<H", pe + 6)
    optional_size, = unpack("<H", pe + 20)
    optional = pe + 24
    if optional_size < 120 or optional > len(data) - optional_size:
        raise ValueError("Truncated PE32+ optional header.")
    magic, = unpack("<H", optional)
    if magic != 0x20B:
        raise ValueError("Expected a PE32+ (64-bit) image.")
    image_size, headers_size = unpack("<II", optional + 56)
    directories_count, = unpack("<I", optional + 108)
    if directories_count < 1:
        raise ValueError("Image has no export directory.")
    export_rva, export_size = unpack("<II", optional + 112)
    if not export_rva or export_size < 40 or export_rva + export_size > image_size:
        raise ValueError("Missing or invalid export directory.")
    section_table = optional + optional_size
    if not section_count or not 0 < headers_size <= min(image_size, len(data)):
        raise ValueError("Invalid PE header or section sizes.")
    if section_table + section_count * 40 > headers_size:
        raise ValueError("Section table does not fit in the PE headers.")

    sections = []
    virtual_regions = [(0, headers_size)]
    for index in range(section_count):
        section = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = unpack("<IIII", section + 8)
        mapped_size = max(virtual_size, raw_size)
        if virtual_address + mapped_size > image_size:
            raise ValueError("Section exceeds the virtual image.")
        if raw_size and (raw_offset < headers_size or raw_offset + raw_size > len(data)):
            raise ValueError("Section raw data is outside the file.")
        if mapped_size:
            virtual_regions.append((virtual_address, virtual_address + mapped_size))
        sections.append((virtual_address, raw_offset, raw_size, mapped_size))
    virtual_regions.sort()
    if any(left[1] > right[0] for left, right in zip(virtual_regions, virtual_regions[1:])):
        raise ValueError("Overlapping virtual section mappings.")

    def file_mapping(rva: int, size: int) -> tuple[int, int]:
        if size < 0 or rva < 0 or rva + size > image_size:
            raise ValueError("RVA exceeds the virtual image.")
        if rva < headers_size and size <= headers_size - rva:
            return rva, headers_size - rva
        for va, raw, raw_size, _ in sections:
            relative = rva - va
            if 0 <= relative < raw_size and size <= raw_size - relative:
                return raw + relative, raw_size - relative
        raise ValueError("Export RVA has no complete file-backed mapping.")

    export_offset, _ = file_mapping(export_rva, export_size)
    function_count, name_count, functions_rva, names_rva, ordinals_rva = unpack("<IIIII", export_offset + 20)
    if not function_count or not name_count:
        raise ValueError(f"Named export {exact_name!r} was not found.")
    functions_offset, _ = file_mapping(functions_rva, function_count * 4)
    names_offset, _ = file_mapping(names_rva, name_count * 4)
    ordinals_offset, _ = file_mapping(ordinals_rva, name_count * 2)
    found = None
    for index in range(name_count):
        name_rva, = unpack("<I", names_offset + index * 4)
        name_offset, available = file_mapping(name_rva, 1)
        name_end = data.find(b"\0", name_offset, name_offset + available)
        if name_end < 0:
            raise ValueError("Unterminated export name.")
        ordinal, = unpack("<H", ordinals_offset + index * 2)
        if ordinal >= function_count:
            raise ValueError("Export name has an invalid function ordinal.")
        if data[name_offset:name_end] != wanted:
            continue
        if found is not None:
            raise ValueError("Duplicate named export is ambiguous.")
        found, = unpack("<I", functions_offset + ordinal * 4)
    if found is None or not found:
        raise ValueError(f"Named export {exact_name!r} was not found.")
    if export_rva <= found < export_rva + export_size:
        raise ValueError("Forwarded exports do not have a local target RVA.")
    if not any(va <= found < va + mapped for va, _, _, mapped in sections):
        raise ValueError("Export target is outside the image's virtual sections.")
    return found
