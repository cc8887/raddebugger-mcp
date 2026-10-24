"""Published-patch source contracts only; these are not native/runtime tests.

The Windows fixture and real UE PIE checks must separately validate actual
UTF-16/UTF-32 values, boundary reads, transport decoding, and output metadata.
"""
from pathlib import Path
import re
import unittest


PATCH = Path(__file__).resolve().parents[1] / "native" / "upstream.patch"
VIZ = "src/eval_visualization/eval_visualization_core.c"


def new_side(path: str) -> str:
    active = False
    result = []
    for line in PATCH.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git "):
            active = line.endswith(f" b/{path}")
        elif active and line.startswith("@@"):
            result.append("\n/* omitted unchanged source */\n")
        elif active and line[:1] in (" ", "+") and not line.startswith("+++"):
            result.append(line[1:])
    return "\n".join(result)


def function_body(source: str, name: str) -> str:
    start = source.index("{", source.index(name + "("))
    depth = 1
    cursor = start + 1
    while depth and cursor < len(source):
        depth += (source[cursor] == "{") - (source[cursor] == "}")
        cursor += 1
    if depth:
        raise AssertionError(f"Incomplete {name} contract in shipped patch")
    return source[start + 1:cursor - 1]


class NativeUnicodeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viz = new_side(VIZ)
        cls.read = function_body(cls.viz, "ev_string_from_encoded_eval")
        cls.validate = function_body(cls.viz, "ev_utf8_sequence_size")
        cls.core = new_side("src/raddbg/raddbg_core.c")

    def test_optional_status_separates_encoding_read_and_truncation(self):
        header = new_side("src/eval_visualization/eval_visualization_core.h")
        for field in ("B32 truncated;", "B32 invalid_encoding;", "B32 read_failed;", "EV_StringStatus *status;"):
            self.assertIn(field, header)

    def test_declared_length_is_source_codeunits_not_converted_utf8_bytes(self):
        self.assertIn("params->limit_strings_size : type->count", self.read)
        self.assertIn("Min(declared_units, unit_cap)", self.read)
        self.assertIn("U64 bytes = units*unit_size;", self.read)
        self.assertIn("r1u64(address, address+bytes)", self.read)
        self.assertNotIn("str8_prefix(string, params->limit_strings_size)", self.viz)
        self.assertNotIn("str8_prefix(string, ptr_data->type->count)", self.viz)

    def test_bounded_no_nul_input_never_uses_unbounded_cstring_scan(self):
        for call in ("str8_cstring(", "str16_cstring(", "str32_cstring("):
            self.assertNotIn(call, self.read)
        self.assertIn("for(U64 i = 0; i < units; i += 1)", self.read)
        self.assertIn("if(unit == 0) { units = i; terminated = 1; break; }", self.read)

    def test_unknown_length_stops_at_codeunit_nul_and_reports_budget(self):
        self.assertIn("U64 unit_cap = 4096/unit_size;", self.read)
        self.assertIn("else if(!bounded)", self.read)
        self.assertIn("r1u64(address+off, address+off+unit_size)", self.read)
        self.assertIn("!terminated && (!bounded || declared_units > unit_cap)", self.read)
        self.assertIn("params->status->truncated = 1", self.read)

    def test_utf16_pairs_are_validated_and_never_split_at_internal_budget(self):
        self.assertIn("0xd800 <= cp && cp <= 0xdbff", self.read)
        self.assertIn("if(i == units && cut) { break; }", self.read)
        self.assertIn("low < 0xdc00 || 0xdfff < low", self.read)
        self.assertIn("cp = 0x10000 + ((cp-0xd800)<<10) + (low-0xdc00)", self.read)

    def test_utf32_and_unpaired_surrogates_are_not_replaced(self):
        self.assertIn("(0xd800 <= cp && cp <= 0xdfff) || cp > 0x10ffff", self.read)
        self.assertIn("params->status->invalid_encoding = 1", self.read)
        self.assertIn('return str8_lit("<invalid encoding>")', self.read)
        self.assertNotIn("str8_from_16(", self.read)
        self.assertNotIn("str8_from_32(", self.read)

    def test_utf8_validation_rejects_overlong_surrogate_and_out_of_range(self):
        for condition in (
            "0xc2 <= p[0] && p[0] <= 0xdf", "0xf0 <= p[0] && p[0] <= 0xf4",
            "p[0] == 0xe0 && p[1] < 0xa0", "p[0] == 0xed && p[1] >= 0xa0",
            "p[0] == 0xf0 && p[1] < 0x90", "p[0] == 0xf4 && p[1] >= 0x90",
            "p[i] < 0x80 || 0xbf < p[i]",
        ):
            self.assertIn(condition, self.validate)
        # An incomplete tail is only truncation if all available bytes are valid.
        self.assertIn("if(partial_valid) { break; }", self.read)
        self.assertIn("buffer[j] < 0x80 || 0xbf < buffer[j]", self.read)

    def test_final_cap_respects_complete_utf8_sequences(self):
        self.assertIn("if(!ev_utf8_is_valid(string))", self.viz)
        self.assertIn("ev_utf8_prefix_size(joined, cap-dots)", self.viz)
        self.assertIn("U64 dots = Min(cap, 3);", self.viz)
        self.assertNotIn("str8_chop(string,", self.viz)
        prefix = function_body(self.viz, "ev_utf8_prefix_size")
        self.assertIn("ev_utf8_sequence_size(string, off)", prefix)
        self.assertIn("off += size;", prefix)

    def test_required_read_failures_remain_explicit(self):
        self.assertIn("params->status->read_failed = 1", self.read)
        self.assertIn("value_status.read_failed || type_status.read_failed", self.core)
        self.assertIn("rad_mcp_eval_read_failed = 1", self.core)
        self.assertIn("A required evaluation read failed or was incomplete", self.core)

    def test_eval_reports_native_byte_limit_and_per_field_validity(self):
        for field in (
            "value_truncated:", "type_truncated:", "display_limit_bytes: 512",
            "value_encoding_valid:", "type_encoding_valid:", "invalid_encoding:",
        ):
            self.assertIn(field, self.core)
        self.assertIn("EV_StringStatus value_status = {0};", self.core)
        self.assertIn("EV_StringStatus type_status = {0};", self.core)
        self.assertIn("string_params.status = &type_status;", self.core)

    def test_invalid_or_partial_values_have_messages_not_false_success(self):
        self.assertIn("if(invalid_encoding)", self.core)
        self.assertIn("if(value_status.truncated || type_status.truncated)", self.core)
        self.assertIn("it is not a complete value", self.core)
        self.assertIn("original invalid bytes were not replaced or accepted as text", self.core)
        self.assertIn("ev_utf8_is_valid(msg->text)", self.core)
        self.assertIn("ev_utf8_is_valid(expr)", self.core)


if __name__ == "__main__":
    unittest.main()
