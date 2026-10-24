"""Synthetic PE32+ fixtures exercise export parsing without Unreal dependencies."""

from pathlib import Path
import struct
import tempfile
import unittest

from scripts.ue_pe import exported_rva


EXPORT_NAME = "?GFrameCounter@@3_KA"


def pe_fixture() -> bytearray:
    data = bytearray(0x400)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x8664, 1)
    struct.pack_into("<H", data, 0x94, 0xF0)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<II", data, 0x98 + 56, 0x2000, 0x200)
    struct.pack_into("<I", data, 0x98 + 108, 16)
    struct.pack_into("<II", data, 0x98 + 112, 0x1000, 0x80)
    section = 0x98 + 0xF0
    data[section:section + 8] = b".edata\0\0"
    struct.pack_into("<IIII", data, section + 8, 0x300, 0x1000, 0x200, 0x200)
    struct.pack_into("<IIIII", data, 0x200 + 20, 1, 1, 0x1040, 0x1044, 0x1048)
    struct.pack_into("<I", data, 0x240, 0x1180)
    struct.pack_into("<I", data, 0x244, 0x1050)
    struct.pack_into("<H", data, 0x248, 0)
    encoded = EXPORT_NAME.encode("ascii") + b"\0"
    data[0x250:0x250 + len(encoded)] = encoded
    return data


class ExportedRvaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "fixture.dll"
        self.image = pe_fixture()

    def resolve(self, name=EXPORT_NAME):
        self.path.write_bytes(self.image)
        return exported_rva(self.path, name)

    def test_returns_an_rva_not_a_file_offset(self):
        self.assertEqual(self.resolve(), 0x1180)

    def test_exported_zero_initialized_data_can_be_in_virtual_tail(self):
        struct.pack_into("<I", self.image, 0x240, 0x1280)
        self.assertEqual(self.resolve(), 0x1280)

    def test_exact_name_is_required(self):
        for name in ("GFrameCounter", EXPORT_NAME.lower(), EXPORT_NAME + "Extra"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not found"):
                self.resolve(name)

    def test_invalid_requested_names_are_rejected(self):
        for name in ("", "a\0b", "计数器"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.resolve(name)

    def test_bad_or_truncated_headers_are_rejected(self):
        for data in (b"", b"MZ", b"XX" + bytes(self.image[2:]), bytes(self.image[:0x100])):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                self.path.write_bytes(data)
                exported_rva(self.path, EXPORT_NAME)

    def test_bad_pe_signature_is_rejected(self):
        self.image[0x80:0x84] = b"PE??"
        with self.assertRaisesRegex(ValueError, "signature"):
            self.resolve()

    def test_pe32_is_rejected(self):
        struct.pack_into("<H", self.image, 0x98, 0x10B)
        with self.assertRaisesRegex(ValueError, "64-bit"):
            self.resolve()

    def test_missing_export_directory_is_rejected(self):
        struct.pack_into("<I", self.image, 0x98 + 112, 0)
        with self.assertRaisesRegex(ValueError, "export directory"):
            self.resolve()

    def test_forwarded_export_is_rejected(self):
        struct.pack_into("<I", self.image, 0x240, 0x1068)
        with self.assertRaisesRegex(ValueError, "Forwarded"):
            self.resolve()

    def test_zero_export_target_is_missing(self):
        struct.pack_into("<I", self.image, 0x240, 0)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.resolve()

    def test_invalid_name_ordinal_is_rejected(self):
        struct.pack_into("<H", self.image, 0x248, 1)
        with self.assertRaisesRegex(ValueError, "ordinal"):
            self.resolve()

    def test_export_tables_must_be_completely_file_backed(self):
        for field_offset, value in ((0x220, 0x1280), (0x218, 0x1000), (0x224, 0x1280)):
            with self.subTest(offset=field_offset), self.assertRaises(ValueError):
                self.image = pe_fixture()
                struct.pack_into("<I", self.image, field_offset, value)
                self.resolve()

    def test_unterminated_export_name_is_rejected(self):
        self.image[0x250:] = b"x" * (len(self.image) - 0x250)
        with self.assertRaisesRegex(ValueError, "Unterminated"):
            self.resolve()

    def test_export_target_outside_virtual_sections_is_rejected(self):
        struct.pack_into("<I", self.image, 0x240, 0x1800)
        with self.assertRaisesRegex(ValueError, "virtual sections"):
            self.resolve()

    def test_section_raw_mapping_cannot_extend_beyond_file(self):
        struct.pack_into("<I", self.image, 0x188 + 20, 0x300)
        with self.assertRaisesRegex(ValueError, "outside the file"):
            self.resolve()

    def test_overlapping_virtual_sections_are_rejected(self):
        struct.pack_into("<H", self.image, 0x86, 2)
        struct.pack_into("<IIII", self.image, 0x1B0 + 8, 0x80, 0x1100, 0x80, 0x300)
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
