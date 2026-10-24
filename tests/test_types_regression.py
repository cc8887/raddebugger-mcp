"""Pure acceptance-helper checks; no backend, fixture, socket or handle is opened."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from regression_types import (assert_complete_string, assert_direct_ftext_protocol,
                              assert_direct_ftext_semantic, assert_direct_string, assert_error,
                              assert_truncated_direct_string, close_child_handle,
                              map_rows, merge_map_pages)


class DirectUnicodeAcceptanceTests(unittest.TestCase):
    def test_legacy_truncation_with_ok_true_is_still_a_failure(self):
        with self.assertRaisesRegex(AssertionError, "Unicode mismatch"):
            assert_direct_string({"ok": True, "value": '"限"', "read_failed": False}, "限帧")

    def test_short_surrogate_pair_and_mixed_text_must_match_exactly(self):
        for value in ("限帧", "FPS 帧率 / test", "播放 🎮🚀"):
            assert_direct_string({"ok": True, "value": '"' + value + '"',
                                  "value_encoding_valid": True, "value_truncated": False}, value)

    def test_complete_content_cannot_hide_a_truncation_flag(self):
        with self.assertRaisesRegex(AssertionError, "truncated"):
            assert_direct_string({"ok": True, "value": '"限帧"', "value_truncated": True}, "限帧")


class TruncatedDirectStringAcceptanceTests(unittest.TestCase):
    def partial(self, display=None):
        return {"ok": False, "value": display if display is not None else '"' + "中" * 169 + "...",
                "value_encoding_valid": True, "invalid_encoding": False,
                "value_truncated": True, "display_limit_bytes": 512,
                "read_failed": False, "messages": "Formatted evaluation output is truncated; not complete"}

    def test_partial_error_with_unclosed_display_quote_is_expected(self):
        value = self.partial()
        self.assertEqual(len(value["value"].encode("utf-8")), 511)
        assert_truncated_direct_string(value, "中" * 600)

    def test_optional_closing_display_quote_is_not_source_content(self):
        assert_truncated_direct_string(self.partial('"' + "中" * 168 + '..."'), "中" * 600)

    def test_full_or_oversized_output_cannot_pass_a_claimed_byte_cap(self):
        for display in ('"' + "中" * 600 + '"', '"' + "中" * 170 + "..."):
            with self.subTest(display_bytes=len(display.encode("utf-8"))):
                with self.assertRaisesRegex(AssertionError, "512-byte"):
                    assert_truncated_direct_string(self.partial(display), "中" * 600)

    def test_empty_wrong_or_nonprefix_output_is_not_valid_truncation(self):
        for display in ('"..."', '"<unreadable string>..."', '"中英..."'):
            with self.subTest(display=display):
                with self.assertRaisesRegex(AssertionError, "proper prefix"):
                    assert_truncated_direct_string(self.partial(display), "中" * 600)

    def test_missing_ellipsis_or_partial_message_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "ellipsis"):
            assert_truncated_direct_string(self.partial('"中"'), "中" * 600)
        for message in (None, "", "A different evaluation failed"):
            value = self.partial()
            value["messages"] = message
            with self.subTest(message=message):
                with self.assertRaisesRegex(AssertionError, "truncation message"):
                    assert_truncated_direct_string(value, "中" * 600)

    def test_encoding_or_read_failure_cannot_be_disguised_as_truncation(self):
        for field, rejected in (("read_failed", True), ("invalid_encoding", True),
                                ("value_encoding_valid", False)):
            value = self.partial()
            value[field] = rejected
            with self.subTest(field=field):
                with self.assertRaisesRegex(AssertionError, "failed read or invalid encoding"):
                    assert_truncated_direct_string(value, "中" * 600)

    def test_public_normalized_truncation_flag_is_accepted(self):
        value = self.partial()
        value["possibly_truncated"] = value.pop("value_truncated")
        assert_truncated_direct_string(value, "中" * 600)

    def test_explicit_truncation_and_limit_metadata_are_mandatory(self):
        for field in ("value_truncated", "display_limit_bytes"):
            value = self.partial()
            value.pop(field)
            with self.subTest(field=field):
                with self.assertRaisesRegex(AssertionError, "display limit"):
                    assert_truncated_direct_string(value, "中" * 600)

    def test_lone_surrogate_cannot_pass_utf8_boundary_validation(self):
        with self.assertRaises(UnicodeEncodeError):
            assert_truncated_direct_string(self.partial('"中\ud800...'), "中" * 600)


class DirectFTextProtocolAcceptanceTests(unittest.TestCase):
    def display(self, *, truncated=False):
        return {"ok": not truncated, "value": "{TextData = {Object = 0x1234}, Flags = 0}",
                "value_encoding_valid": True, "type_encoding_valid": True,
                "invalid_encoding": False, "read_failed": False,
                "possibly_truncated": truncated, "display_limit_bytes": 512,
                "messages": "Formatted evaluation output is truncated" if truncated else ""}

    def test_complete_structural_view_does_not_promise_display_string_semantics(self):
        data = self.display()
        data["value"] = "{Storage = {Name = 中文 🎮}, Pointer = 0x1234}"
        assert_direct_ftext_protocol(data)

    def test_flagged_partial_error_is_allowed_without_json_or_semantic_recovery(self):
        data = self.display(truncated=True)
        data["value"] = '{TextData = {DisplayString = "' + "中" * 150 + "..."
        assert_direct_ftext_protocol(data)
        data["value_truncated"] = data.pop("possibly_truncated")
        assert_direct_ftext_protocol(data)

    def test_partial_display_must_have_false_ok_and_explicit_message(self):
        for field, rejected in (("ok", True), ("messages", ""),
                                ("messages", "A different evaluation failed")):
            data = self.display(truncated=True)
            data[field] = rejected
            with self.subTest(field=field, rejected=rejected):
                with self.assertRaisesRegex(AssertionError, "flag/message and ok=false"):
                    assert_direct_ftext_protocol(data)

    def test_missing_truncation_flag_cannot_be_hidden_by_partial_error(self):
        data = self.display(truncated=True)
        data.pop("possibly_truncated")
        with self.assertRaisesRegex(AssertionError, "conceal an error or truncation"):
            assert_direct_ftext_protocol(data)

    def test_read_and_encoding_failures_are_never_accepted_as_display_caps(self):
        for field, rejected in (("read_failed", True), ("invalid_encoding", True),
                                ("value_encoding_valid", False), ("type_encoding_valid", False)):
            data = self.display(truncated=True)
            data[field] = rejected
            with self.subTest(field=field):
                with self.assertRaisesRegex(AssertionError, "failed read or invalid encoding"):
                    assert_direct_ftext_protocol(data)

    def test_oversized_display_and_missing_limit_are_rejected(self):
        data = self.display(truncated=True)
        data["value"] = "中" * 171
        with self.assertRaisesRegex(AssertionError, "512-byte"):
            assert_direct_ftext_protocol(data)
        data = self.display()
        data.pop("display_limit_bytes")
        with self.assertRaisesRegex(AssertionError, "display limit"):
            assert_direct_ftext_protocol(data)

    def test_lone_surrogates_in_display_or_message_are_not_utf8_safe(self):
        for field in ("value", "messages"):
            data = self.display()
            data[field] = "中\ud800"
            with self.subTest(field=field):
                with self.assertRaises(UnicodeEncodeError):
                    assert_direct_ftext_protocol(data)


class DirectFTextSemanticAcceptanceTests(unittest.TestCase):
    def semantic(self):
        return {"ok": True, "value": '"播放 🎮🚀"', "read_failed": False,
                "invalid_encoding": False, "value_encoding_valid": True,
                "type_encoding_valid": True, "possibly_truncated": False,
                "display_limit_bytes": 512, "messages": "",
                "representation": "ue4_26_ftext_display_string",
                "semantic_read": {"ok": True, "storage": "generated_display_string",
                                  "complete": True, "truncated": False},
                "native_display_diagnostics": {"value": "<invalid binary text>", "ok": False,
                                               "read_failed": True, "invalid_encoding": True}}

    def check(self, data):
        assert_direct_ftext_semantic(data, "播放 🎮🚀", "generated_display_string")

    def test_exact_semantic_text_can_preserve_failing_private_native_diagnostics(self):
        self.check(self.semantic())

    def test_structural_or_wrong_display_string_is_not_semantic_acceptance(self):
        for display in ('"播放"', '{TextData = 0x1234}'):
            data = self.semantic()
            data["value"] = display
            with self.subTest(display=display):
                with self.assertRaises((AssertionError, ValueError)):
                    self.check(data)

    def test_semantic_representation_is_mandatory(self):
        data = self.semantic()
        data.pop("representation")
        with self.assertRaisesRegex(AssertionError, "semantic representation"):
            self.check(data)

    def test_uncapped_duplicate_text_is_not_allowed_in_semantic_metadata(self):
        data = self.semantic()
        data["semantic_read"]["value"] = "播放 🎮🚀"
        with self.assertRaisesRegex(AssertionError, "uncapped semantic value"):
            self.check(data)

    def test_failed_partial_or_wrong_storage_semantic_read_is_rejected(self):
        for field, rejected in (("ok", False), ("error", {"code": "READ_FAILED"}),
                                ("storage", "unverified"), ("complete", False), ("truncated", True)):
            data = self.semantic()
            data["semantic_read"][field] = rejected
            with self.subTest(field=field):
                with self.assertRaisesRegex(AssertionError, "verified semantic storage"):
                    self.check(data)

    def test_native_display_diagnostics_must_be_preserved(self):
        for diagnostics in (None, {}, {"value": "native display"}):
            data = self.semantic()
            data["native_display_diagnostics"] = diagnostics
            with self.subTest(diagnostics=diagnostics):
                with self.assertRaisesRegex(AssertionError, "native display diagnostics"):
                    self.check(data)


class StructuredStringAcceptanceTests(unittest.TestCase):
    def complete(self, text):
        units = len(text.encode("utf-16-le")) // 2
        return {"ok": True, "value": text, "complete": True, "truncated": False,
                "code_units": units, "decoded_units": units, "next_index": None}

    def test_embedded_nul_and_empty_remain_values(self):
        for text in ("", "A\0中", "🎮"):
            assert_complete_string(self.complete(text), text)

    def test_partial_prefix_cannot_be_called_full(self):
        value = self.complete("中")
        value.update(complete=False, truncated=True, next_index=1)
        with self.assertRaisesRegex(AssertionError, "Partial"):
            assert_complete_string(value, "中")

    def test_unicode_code_points_cannot_replace_utf16_units(self):
        value = self.complete("🎮")
        value["decoded_units"] = 1
        with self.assertRaisesRegex(AssertionError, "unit count"):
            assert_complete_string(value, "🎮")

    def test_unknown_layout_is_an_error_not_empty_success(self):
        assert_error({"ok": False, "error": {"code": "UNSUPPORTED_LAYOUT"}}, "UNSUPPORTED_LAYOUT")
        with self.assertRaises(AssertionError):
            assert_error({"ok": True, "value": ""}, "UNSUPPORTED_LAYOUT")


class SparseMapAcceptanceTests(unittest.TestCase):
    def page(self, index, key, value, *, end=False):
        return {"ok": True, "count": 2, "items": [{"index": index,
                "key": {"kind": "fname", "type": "FName", "value": key, "complete": True},
                "value": {"kind": "scalar", "type": "int32", "value": value}}],
                "next_index": None if end else index + 1, "truncated": not end}

    def test_physical_holes_are_allowed_but_values_and_final_page_are_required(self):
        merge_map_pages([self.page(0, "first", 11), self.page(131, "last", -7, end=True)],
                        {"first": 11, "last": -7})

    def test_duplicate_physical_slots_cannot_pass_key_value_equality(self):
        with self.assertRaisesRegex(AssertionError, "physical slot"):
            merge_map_pages([self.page(0, "first", 11), self.page(0, "last", -7, end=True)],
                            {"first": 11, "last": -7})

    def test_same_count_but_wrong_key_or_value_is_a_failure(self):
        with self.assertRaisesRegex(AssertionError, "mismatch"):
            merge_map_pages([self.page(0, "first", 11), self.page(2, "wrong", -7, end=True)],
                            {"first": 11, "last": -7})

    def test_null_pointer_is_distinct_from_missing_or_opaque_value(self):
        page = self.page(0, "null", None, end=True)
        page["items"][0]["value"] = {"kind": "pointer", "type": "UFixtureObject *",
                                       "value": None, "address": None}
        self.assertEqual(map_rows(page), {"null": None})
        page["items"][0]["value"]["kind"] = "opaque"
        with self.assertRaisesRegex(AssertionError, "Untyped"):
            map_rows(page)

    def test_partial_string_map_value_is_not_complete(self):
        page = self.page(0, "text", "中", end=True)
        page["items"][0]["value"] = {"kind": "fstring", "value": "中",
                                       "complete": False, "truncated": True}
        with self.assertRaisesRegex(AssertionError, "Partial map value"):
            map_rows(page)


class OriginalChildHandleTests(unittest.TestCase):
    def test_cleanup_closes_the_original_subprocess_handle(self):
        child = MagicMock()
        close_child_handle(child)
        child._handle.Close.assert_called_once_with()
        child.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
