"""Mock-only UE layout readers: no backend, engine, or target process."""

import asyncio
import json
import re
import unittest

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.value_reader import ValueReader


class MemoryEvaluator:
    """Small expression oracle; memory is byte-addressed and overreads fail."""

    def __init__(self):
        self.values = {"sizeof(void *)": 8, "sizeof(uint16)": 2}
        self.types = {}
        self.addresses = {}
        self.memory = {}
        self.calls = []
        self.overrides = {}
        self.slot_strides = {}
        self.slot_bases = {}
        self.scalar_fields = {}
        self.epoch = 7

    def store(self, address, data):
        self.memory.update({address + i: value for i, value in enumerate(data)})

    def string(self, expression="s", text="hello", *, address=0x1000, data_address=0x2000, units=None):
        self.types[expression] = "FString"
        self.addresses[expression] = address
        data = text.encode("utf-16-le") + b"\0\0" if units is None else b"".join(unit.to_bytes(2, "little") for unit in units)
        self.store(data_address, data)
        base = f"raw({expression}).Data"
        self.values.update({f"{base}.ArrayNum": len(data) // 2,
                            f"{base}.ArrayMax": len(data) // 2,
                            f"hex(cast(uint64)({base}.AllocatorInstance.Data))": data_address})
        return self

    def map(self, *, slots=5, active=(0, 2, 4), secondary=False, stride=40):
        self.types["m"] = "TMap<int32,int32,FDefaultSetAllocator>"
        self.addresses["m"] = 0x3000
        pairs = "raw(m).Pairs"
        self.types[pairs] = "TSet<TTuple<int32,int32>,KeyFuncs,FDefaultSetAllocator>"
        sparse = f"{pairs}.Elements"
        array = f"{sparse}.Data"
        flags = f"{sparse}.AllocationFlags"
        self.values.update({f"{array}.ArrayNum": slots, f"{array}.ArrayMax": slots,
                            f"hex(cast(uint64)({array}.AllocatorInstance.Data))": 0x4000 if slots else 0,
                            f"{sparse}.NumFreeIndices": slots - len(active),
                            f"{sparse}.FirstFreeIndex": next((i for i in range(slots) if i not in active), -1),
                            f"{flags}.NumBits": slots, f"{flags}.MaxBits": max(128, (slots + 31) // 32 * 32),
                            f"hex(cast(uint64)({flags}.AllocatorInstance.SecondaryData.Data))": 0x6000 if secondary else 0})
        self.addresses[f"{flags}.AllocatorInstance.InlineData"] = 0x5000
        bitmap = 0x6000 if secondary else 0x5000
        words = [0] * max(1, (slots + 31) // 32)
        for index in active:
            words[index // 32] |= 1 << (index % 32)
        self.store(bitmap, b"".join(word.to_bytes(4, "little") for word in words))
        self.slot_bases[array], self.slot_strides[array] = 0x4000, stride
        for index in active:
            pair_address = 0x4000 + index * stride
            pair_expr = f"(*(cast(TTuple<int32,int32> *){hex(pair_address)}))"
            for name, offset, value in (("Key", 0, index + 10), ("Value", 4, -index)):
                field = f"{pair_expr}.{name}"
                self.types[field], self.addresses[field] = "int32", pair_address + offset
                self.values[f"raw({field})"] = value
        return self

    async def __call__(self, expression):
        self.calls.append(expression)
        epoch = {"run_gen": 3, "stop_count": self.epoch, "thread_id": 17,
                 "unwind_count": 0, "inline_depth": 0}
        if expression in self.overrides:
            override = self.overrides[expression]
            if isinstance(override, BaseException):
                raise override
            return {"ok": True, "type": "uint64", "value": "0", **epoch, **override}
        try:
            if expression.startswith("typeof(raw(") and expression.endswith("))"):
                source = expression[len("typeof(raw("):-2]
                value = self.types[source]
                return {"ok": True, "type": value, "value": value, **epoch}
            if expression.startswith("hex(cast(uint64)(&(raw(") and expression.endswith("))))"):
                source = expression[len("hex(cast(uint64)(&(raw("):-4]
                if source in self.addresses:
                    value = self.addresses[source]
                else:
                    slot = re.fullmatch(r"(.+)\[(\d+)\]\.ElementData", source)
                    if not slot:
                        raise KeyError(source)
                    value = self.slot_bases[slot[1]] + int(slot[2]) * self.slot_strides[slot[1]]
                return {"ok": True, "type": "uint64", "value": hex(value), **epoch}
            batch = re.fullmatch(r"array\(cast\(uint64 \*\)0x([0-9a-f]+),(\d+)\)", expression)
            if batch:
                address, count = int(batch[1], 16), int(batch[2])
                values = [int.from_bytes(bytes(self.memory[address + i * 8 + j] for j in range(8)), "little")
                          for i in range(count)]
                return {"ok": True, "type": "uint64[]", "value": json.dumps(values), **epoch}
            scalar = re.fullmatch(r"hex\(\*\(cast\(uint(8|16|32|64) \*\)0x([0-9a-f]+)\)\)", expression)
            if scalar:
                width, address = int(scalar[1]) // 8, int(scalar[2], 16)
                value = int.from_bytes(bytes(self.memory[address + j] for j in range(width)), "little")
            else:
                value = self.values[expression]
            return {"ok": True, "type": "int32", "value": str(value), **epoch}
        except KeyError:
            return {"ok": False, "type": "", "value": "", "messages": ["Unknown field"], **epoch}


class StringReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_non_bmp_and_embedded_nul_are_exact(self):
        for text in ("", "限帧", "播放 🎮🚀", "A\0中"):
            with self.subTest(text=repr(text)):
                model = MemoryEvaluator().string(text=text)
                result = await ValueReader(model).read("s")
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["value"], text)
                self.assertTrue(result["complete"])
                self.assertFalse(result["truncated"])

    async def test_zero_length_null_storage_is_verified_empty(self):
        model = MemoryEvaluator().string(units=[])
        model.values["hex(cast(uint64)(raw(s).Data.AllocatorInstance.Data))"] = 0
        result = await ValueReader(model).read("s")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "")
        self.assertEqual(result["storage_units"], 0)

    async def test_full_4096_units_fit_probe_budget_and_never_display_raw_string(self):
        model = MemoryEvaluator().string(text="中" * 4096)
        result = await ValueReader(model).read("s")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "中" * 4096)
        self.assertLess(result["probes"], 100)
        batches = [call for call in model.calls if call.startswith("array(")]
        self.assertTrue(batches)
        self.assertTrue(all(int(call.rsplit(",", 1)[1][:-1]) <= 16 for call in batches))

    async def test_bounded_prefix_does_not_split_surrogate_pair(self):
        result = await ValueReader(MemoryEvaluator().string(text="播放 🎮🚀")).read("s", max_string_units=4)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "播放 ")
        self.assertEqual(result["decoded_units"], 3)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])

    async def test_string_pages_resume_at_utf16_offset_without_repeating_prefix(self):
        model = MemoryEvaluator().string(text="AB🎮中")
        first = await ValueReader(model).read("s", max_string_units=3)
        self.assertEqual(first["value"], "AB")
        self.assertEqual(first["next_index"], 2)
        second = await ValueReader(model).read("s", start=first["next_index"], max_string_units=3)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["value"], "🎮中")
        self.assertIsNone(second["next_index"])
        self.assertTrue(second["complete"])

    async def test_string_start_inside_surrogate_and_too_small_budget_rejected(self):
        model = MemoryEvaluator().string(text="🎮中")
        for options, code in (({"start": 1}, "INVALID_ARGUMENT"),
                              ({"start": 4}, "INVALID_ARGUMENT"),
                              ({"max_string_units": 1}, "STRING_BUDGET_TOO_SMALL")):
            result = await ValueReader(model).read("s", **options)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], code)

    async def test_empty_string_and_end_page_reject_out_of_range_start(self):
        empty = await ValueReader(MemoryEvaluator().string(text="")).read("s", start=1)
        self.assertEqual(empty["error"]["code"], "INVALID_ARGUMENT")
        result = await ValueReader(MemoryEvaluator().string(text="中")).read("s", start=1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], "")
        self.assertIsNone(result["next_index"])

    async def test_invalid_utf16_and_missing_nul_are_not_empty(self):
        for units, code in (([0xD800, 0], "INVALID_UTF16"), ([65, 66], "INVALID_LAYOUT")):
            with self.subTest(units=units):
                result = await ValueReader(MemoryEvaluator().string(units=units)).read("s")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("value", result)

    async def test_invalid_array_header_and_null_nonempty_storage_rejected(self):
        for field, value in (("raw(s).Data.ArrayNum", -1), ("raw(s).Data.ArrayMax", 0),
                             ("hex(cast(uint64)(raw(s).Data.AllocatorInstance.Data))", 0)):
            model = MemoryEvaluator().string()
            model.values[field] = value
            result = await ValueReader(model).read("s")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_LAYOUT")

    async def test_numeric_batch_requires_complete_integer_array(self):
        for value in ("[1]", "[true,true,true,true]", "[1.5,2,3,4]", "[", "[18446744073709551616,0,0,0]"):
            model = MemoryEvaluator().string(text="中" * 16)
            model.overrides["array(cast(uint64 *)0x2000,4)"] = {"value": value}
            result = await ValueReader(model).read("s")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "BAD_REPLY")


class MapReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_map_is_not_confused_with_unknown_allocator(self):
        result = await ValueReader(MemoryEvaluator().map(slots=0, active=())).read("m")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])

    async def test_sparse_holes_are_skipped_and_slot_addresses_use_pdb_stride(self):
        model = MemoryEvaluator().map(stride=56)
        result = await ValueReader(model).read("m")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 3)
        self.assertEqual([item["index"] for item in result["items"]], [0, 2, 4])
        self.assertEqual([item["key"]["value"] for item in result["items"]], [10, 12, 14])
        self.assertEqual(result["items"][1]["key"]["address"], hex(0x4000 + 2 * 56))
        self.assertTrue(any(".Data[2].ElementData" in call for call in model.calls))
        self.assertFalse(any(".AllocatorInstance.Data[" in call for call in model.calls))

    async def test_pagination_resumes_from_physical_slot_and_scans_holes(self):
        model = MemoryEvaluator().map()
        first = await ValueReader(model).read("m", count=1)
        self.assertEqual(first["next_index"], 1)
        self.assertTrue(first["truncated"])
        second = await ValueReader(model).read("m", start=first["next_index"], count=1)
        self.assertEqual([item["index"] for item in second["items"]], [2])
        self.assertEqual(second["next_index"], 3)
        self.assertEqual(second["scanned_slots"], 2)

    async def test_secondary_bitmap_and_scan_budget_cross_word_boundaries(self):
        model = MemoryEvaluator().map(slots=132, active=(0, 31, 128, 131), secondary=True)
        page = await ValueReader(model).read("m", count=8, slot_scan_budget=32)
        self.assertTrue(page["ok"], page)
        self.assertEqual([item["index"] for item in page["items"]], [0, 31])
        self.assertEqual(page["next_index"], 32)
        self.assertTrue(page["scan_budget_exhausted"])
        last = await ValueReader(model).read("m", start=128, count=8)
        self.assertEqual([item["index"] for item in last["items"]], [128, 131])
        self.assertEqual(last["bitmap_allocator"], "secondary")

    async def test_free_count_and_bitmap_inconsistency_rejected(self):
        model = MemoryEvaluator().map()
        model.values["raw(m).Pairs.Elements.NumFreeIndices"] = 1
        result = await ValueReader(model).read("m")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_LAYOUT")


class NameReaderTests(unittest.IsolatedAsyncioTestCase):
    def model(self, name="Alpha", *, wide=False, number=0, header_size=2, entry_offset=4):
        model = MemoryEvaluator()
        model.types["a"], model.addresses["a"] = "TArray<FName,Allocator>", 0x1000
        model.values.update({"raw(a).ArrayNum": 1, "raw(a).ArrayMax": 1,
                             "hex(cast(uint64)(raw(a).AllocatorInstance.Data))": 0x9000})
        item = "(cast(FName *)0x9000)[0]"
        model.types[item], model.addresses[item] = "FName", 0x9000
        model.values.update({f"sizeof(raw({item}))": 12, "sizeof(FNameEntryHeader)": header_size,
                             f"raw({item}).DisplayIndex.Value": 16, f"raw({item}).Number": number,
                             "hex(cast(uint64)(GNameBlocksDebug[0]))": 0xA000})
        entry = "(cast(FNameEntry *)0xa040)"
        model.addresses[f"{entry}->Header"] = 0xA040 + entry_offset
        model.addresses[f"{entry}->AnsiName"] = 0xA046
        raw = name.encode("utf-16-le" if wide else "ascii")
        model.store(0xA046, raw)
        model.values.update({f"{entry}->Header.Len": len(raw) // (2 if wide else 1),
                             f"{entry}->Header.bIsWide": int(wide)})
        return model

    async def test_profile_validates_name_header_and_decodes_ascii_wide_suffix(self):
        for name, wide, number, expected in (("Alpha", False, 0, "Alpha"),
                                              ("中名🎮", True, 2, "中名🎮_1")):
            result = await ValueReader(self.model(name, wide=wide, number=number)).read("a")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["items"][0]["kind"], "fname")
            self.assertEqual(result["items"][0]["value"], expected)

    async def test_unknown_name_header_size_or_offset_is_not_guessed(self):
        for options in ({"header_size": 4}, {"entry_offset": 0}):
            result = await ValueReader(self.model(**options)).read("a")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "UNSUPPORTED_LAYOUT")

    async def test_name_pool_read_failure_stops_without_trying_other_pool(self):
        model = self.model()
        model.overrides["hex(cast(uint64)(GNameBlocksDebug[0]))"] = {"read_failed": True}
        result = await ValueReader(model).read("a")
        self.assertEqual(result["error"]["code"], "READ_FAILED")
        self.assertFalse(any("NamePoolData" in call for call in model.calls))


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_injection_and_target_calls_are_rejected_without_probes(self):
        for expression in ("s;exit", "s\nexit", "s=3", "s++", "s.Num()", "s.ToString()", "(s.f)(1)"):
            model = MemoryEvaluator()
            result = await ValueReader(model).read(expression)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
            self.assertEqual(model.calls, [])

    async def test_bad_bounds_are_rejected_without_probes(self):
        for options in ({"start": -1}, {"count": 0}, {"count": True}, {"max_string_units": 4097},
                        {"slot_scan_budget": 0}, {"max_depth": 5}):
            model = MemoryEvaluator()
            result = await ValueReader(model).read("s", **options)
            self.assertFalse(result["ok"])
            self.assertEqual(model.calls, [])

    async def test_unknown_kind_and_layout_are_not_empty(self):
        model = MemoryEvaluator()
        model.types["s"], model.addresses["s"] = "FUnknown", 0x1000
        for reader, options in ((ValueReader(model), {}), (ValueReader(model, layout="ue5"), {}),
                                (ValueReader(model), {"kind": "ftext"})):
            result = await reader.read("s", **options)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "UNSUPPORTED_LAYOUT")

    async def test_read_failure_and_encoding_metadata_stop_further_probes(self):
        for metadata, code in (({"read_failed": True}, "READ_FAILED"),
                               ({"invalid_encoding": True}, "INVALID_ENCODING"),
                               ({"value_encoding_valid": False}, "INVALID_ENCODING"),
                               ({"type_encoding_valid": False}, "INVALID_ENCODING"),
                               ({"type_truncated": True}, "TRUNCATED_PROBE"),
                               ({"possibly_truncated": True}, "TRUNCATED_PROBE")):
            model = MemoryEvaluator().string()
            model.overrides["sizeof(void *)"] = metadata
            result = await ValueReader(model).read("s")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(len(model.calls), 1)

    async def test_callback_stale_snapshot_and_cancellation_propagate_immediately(self):
        for error in (RadError("STALE_SNAPSHOT", "changed"), asyncio.CancelledError()):
            model = MemoryEvaluator().string()
            model.overrides["sizeof(void *)"] = error
            with self.assertRaises(type(error)):
                await ValueReader(model).read("s")
            self.assertEqual(len(model.calls), 1)

    async def test_cross_probe_epoch_change_is_never_accepted(self):
        model = MemoryEvaluator().string()
        model.overrides["sizeof(uint16)"] = {"value": "2", "stop_count": 8}
        with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            await ValueReader(model).read("s")
        self.assertEqual(len(model.calls), 2)

    async def test_probe_budget_is_bounded(self):
        model = MemoryEvaluator().string()
        reader = ValueReader(model)
        reader.MAX_PROBES = 3
        result = await reader.read("s")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PROBE_BUDGET_EXCEEDED")
        self.assertEqual(len(model.calls), 3)

    async def test_time_budget_is_bounded(self):
        async def slow(expression):
            await asyncio.sleep(.1)
            return {"ok": True, "value": "8", "type": "uint64"}
        reader = ValueReader(slow)
        reader.TIMEOUT_SECONDS = .001
        result = await reader.read("s")
        self.assertEqual(result["error"]["code"], "TIME_BUDGET_EXCEEDED")

    async def test_unknown_struct_leaf_is_opaque_and_not_pretty_printed_as_scalar(self):
        model = MemoryEvaluator()
        model.types["a"] = "TArray<FUnknown,Allocator>"
        model.addresses["a"] = 0x1000
        model.values.update({"raw(a).ArrayNum": 1, "raw(a).ArrayMax": 1,
                             "hex(cast(uint64)(raw(a).AllocatorInstance.Data))": 0x2000})
        element = "(cast(FUnknown *)0x2000)[0]"
        model.types[element], model.addresses[element] = "FUnknown", 0x2000
        result = await ValueReader(model).read("a")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["items"][0]["kind"], "opaque")
        self.assertFalse(result["items"][0]["expanded"])
        self.assertNotIn(f"raw({element})", model.calls)

    async def test_unknown_ftext_history_is_not_empty(self):
        model = MemoryEvaluator()
        model.types["t"], model.addresses["t"] = "FText", 0x1000
        model.values["hex(cast(uint64)(raw(t).TextData.Object))"] = 0x7000
        result = await ValueReader(model).read("t")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSUPPORTED_LAYOUT")

    async def test_localized_and_generated_ftext_verified_storage_and_pagination(self):
        for storage in ("localized_string", "generated_display_string"):
            model = MemoryEvaluator()
            model.types["t"], model.addresses["t"] = "FText", 0x1000
            pointer = "raw(t).TextData.Object"
            model.values[f"hex(cast(uint64)({pointer}))"] = 0x7000
            if storage == "localized_string":
                model.values[f"hex(cast(uint64)({pointer}->LocalizedString.Object))"] = 0x8000
                string_path = f"*({pointer}->LocalizedString.Object)"
            else:
                model.values[f"hex(cast(uint64)({pointer}->LocalizedString.Object))"] = 0
                string_path = f"{pointer}->DisplayString"
            model.string(string_path, "A🎮中", address=0x8000)
            result = await ValueReader(model).read("t", start=1, max_string_units=2)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["storage"], storage)
            self.assertEqual(result["value"], "🎮")
            self.assertEqual(result["next_index"], 3)

    async def test_ftext_read_failure_does_not_try_alternative_history(self):
        model = MemoryEvaluator()
        model.types["t"], model.addresses["t"] = "FText", 0x1000
        model.values["hex(cast(uint64)(raw(t).TextData.Object))"] = 0x7000
        model.overrides["hex(cast(uint64)(raw(t).TextData.Object->LocalizedString.Object))"] = {"read_failed": True}
        result = await ValueReader(model).read("t")
        self.assertEqual(result["error"]["code"], "READ_FAILED")
        self.assertFalse(any("DisplayString" in call for call in model.calls))


if __name__ == "__main__":
    unittest.main()
