"""Synthetic live-memory schemas, not proof of an Unreal runtime ABI."""

import asyncio
import re
import struct
import unittest
from unittest.mock import patch

from test_value_reader import MemoryEvaluator

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.reflection_reader import ReflectionReader


class ReflectionMemory(MemoryEvaluator):
    LAYOUT = {
        "UObject": {"ClassPrivate": (0, 8), "NamePrivate": (8, 8)},
        "UStruct": {
            "ChildProperties": (16, 8),
            "SuperStruct": (24, 8),
            "PropertiesSize": (32, 4),
        },
        "UClass": {"ClassFlags": (40, 4)},
        "FName": {"ComparisonIndex": (0, 4), "Number": (4, 4)},
        "FNameEntry": {"Header": (0, 2), "AnsiName": (2, 1024)},
        "FFieldClass": {"Name": (0, 8)},
        "FProperty": {
            "ClassPrivate": (0, 8),
            "Next": (8, 8),
            "NamePrivate": (16, 8),
            "ArrayDim": (24, 4),
            "ElementSize": (28, 4),
            "Offset_Internal": (32, 4),
        },
        "FStructProperty": {"Struct": (40, 8)},
        "FArrayProperty": {"Inner": (40, 8), "ArrayFlags": (48, 4)},
        "FMapProperty": {
            "KeyProp": (40, 8),
            "ValueProp": (48, 8),
            "MapLayout": (56, 24),
            "MapFlags": (80, 4),
        },
        "FScriptMapLayout": {"ValueOffset": (0, 4), "SetLayout": (4, 20)},
        "FScriptSetLayout": {"SparseArrayLayout": (12, 8)},
        "FScriptSparseArrayLayout": {"Alignment": (0, 4), "Size": (4, 4)},
        "FBoolProperty": {
            "FieldSize": (40, 1),
            "ByteOffset": (41, 1),
            "FieldMask": (43, 1),
        },
        "FEnumProperty": {"UnderlyingProp": (40, 8)},
        "FScriptArray": {"Data": (0, 8), "ArrayNum": (8, 4), "ArrayMax": (12, 4)},
    }

    def __init__(self):
        super().__init__()
        self.store(0x800000, bytes(131072))
        self.values.update(
            {
                "sizeof(FName)": 8,
                "sizeof(FScriptArray)": 16,
                "sizeof(FScriptMap)": 80,
                "hex(cast(uint64)(actor))": 0x10000,
                "hex(cast(uint64)(GNameBlocksDebug[0]))": 0x800000,
            }
        )
        self.names = {}
        self.name_offset = 0
        self.next_address = 0x20000
        self.classes = {}
        self.store(0x10000, bytes(512))
        self.meta = self.alloc()
        self.obj(self.meta, self.meta, "Class")
        self.native = self.schema("NativeActor", 128, flags=0)
        self.blueprint = self.schema(
            "BP_GameActor_C", 256, flags=0x40000, parent=self.native
        )
        self.obj(0x10000, self.blueprint, "BP_GameActor_C_0")

    def alloc(self):
        p = self.next_address
        self.next_address += 0x1000
        self.store(p, bytes(512))
        return p

    def put(self, p, value, size=8):
        self.store(p, int(value).to_bytes(size, "little", signed=value < 0))

    def name(self, p, value):
        if value not in self.names:
            offset = self.name_offset
            wide = not value.isascii()
            text = value.encode("utf-16-le" if wide else "ascii")
            self.store(
                0x800000 + offset,
                (((len(text) // (2 if wide else 1)) << 6) | 0x2A | int(wide)).to_bytes(
                    2, "little"
                )
                + text,
            )
            self.names[value] = offset // 2
            self.name_offset += (len(text) + 3) & ~1
        self.put(p, self.names[value], 4)
        self.put(p + 4, 0, 4)

    def obj(self, p, cls, name):
        self.put(p, cls)
        self.name(p + 8, name)

    def schema(self, name, size, *, flags=0, parent=0, cls=None):
        p = self.alloc()
        self.obj(p, cls or self.meta, name)
        self.put(p + 24, parent)
        self.put(p + 32, size, 4)
        self.put(p + 40, flags, 4)
        return p

    def prop(self, name, typ, offset, size, *, owner=None, dim=1, extra=None):
        if typ not in self.classes:
            self.classes[typ] = self.alloc()
            self.name(self.classes[typ], typ)
        p = self.alloc()
        self.put(p, self.classes[typ])
        self.name(p + 16, name)
        self.put(p + 24, dim, 4)
        self.put(p + 28, size, 4)
        self.put(p + 32, offset, 4)
        for field, value in (extra or {}).items():
            off, width = self.LAYOUT["F" + typ][field]
            self.put(p + off, value, width)
        if owner:
            self.put(
                p + 8,
                int.from_bytes(
                    bytes(self.memory[owner + 16 + i] for i in range(8)), "little"
                ),
            )
            self.put(owner + 16, p)
        return p

    def array(self, address, values, fmt="i"):
        data = self.alloc()
        self.store(data, struct.pack("<" + fmt * len(values), *values))
        self.put(address, data)
        self.put(address + 8, len(values), 4)
        self.put(address + 12, len(values), 4)
        e = f"(*(cast(FScriptArray *)0x{address:x}))"
        self.values.update(
            {
                f"{e}.ArrayNum": len(values),
                f"{e}.ArrayMax": len(values),
                f"hex(cast(uint64)({e}.AllocatorInstance.Data))": data,
            }
        )
        return data

    def nested(self):
        s = self.schema("BP_Profile", 32)
        self.prop("Score_ABC", "IntProperty", 0, 4, owner=s)
        self.prop("Weight", "DoubleProperty", 8, 8, owner=s)
        p = self.prop(
            "Profile",
            "StructProperty",
            128,
            32,
            owner=self.blueprint,
            extra={"Struct": s},
        )
        self.put(0x10080, 73, 4)
        self.store(0x10088, struct.pack("<d", 2.5))
        return p, s

    def map(self):
        key = self.prop("Key", "IntProperty", 0, 4)
        _, s = self.nested()
        value = self.prop("Value", "StructProperty", 0, 32, extra={"Struct": s})
        p = self.prop(
            "Profiles",
            "MapProperty",
            160,
            80,
            owner=self.blueprint,
            extra={"KeyProp": key, "ValueProp": value},
        )
        self.put(p + 56, 8, 4)
        self.put(p + 72, 8, 4)
        self.put(p + 76, 48, 4)
        sparse = "(cast(FScriptMap *)0x100a0)->Pairs.Elements"
        data = self.alloc()
        bitmap = self.alloc()
        self.addresses[f"{sparse}.Data"] = 0x100A0
        self.put(0x100A0, data)
        self.put(0x100A8, 3, 4)
        self.put(0x100AC, 3, 4)
        self.values.update(
            {
                f"{sparse}.Data.ArrayNum": 3,
                f"{sparse}.Data.ArrayMax": 3,
                f"hex(cast(uint64)({sparse}.Data.AllocatorInstance.Data))": data,
                f"{sparse}.NumFreeIndices": 1,
                f"{sparse}.FirstFreeIndex": 1,
                f"{sparse}.AllocationFlags.NumBits": 3,
                f"{sparse}.AllocationFlags.MaxBits": 128,
                f"hex(cast(uint64)({sparse}.AllocationFlags.AllocatorInstance.Data))": bitmap,
            }
        )
        self.put(bitmap, 5, 4)
        for i, k, v in ((0, 10, 71), (2, 20, 93)):
            self.put(data + i * 48, k, 4)
            self.put(data + i * 48 + 8, v, 4)
            self.store(data + i * 48 + 16, struct.pack("<d", 1.25 + i))
        return p

    async def __call__(self, expression):
        # Resolve only documented PDB address/sizeof probes; all data probes use
        # the byte-memory evaluator, including overread failures and epochs.
        match = re.search(r"\(cast\((\w+) \*\)0x([0-9a-f]+)\)->(\w+)", expression)
        if match:
            typ, base, field = match.groups()
            base = int(base, 16)
            if typ in self.LAYOUT and field in self.LAYOUT[typ]:
                offset, width = self.LAYOUT[typ][field]
                if expression.startswith("sizeof(raw("):
                    self.values[expression] = width
                elif expression.startswith("hex(cast(uint64)(&(raw("):
                    source = expression[len("hex(cast(uint64)(&(raw(") : -4]
                    self.addresses[source] = base + offset
        return await super().__call__(expression)


def fields(result):
    return {x["name"]: x for x in result["value"]["fields"]}


class ReflectionReaderTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, memory, **options):
        return await ReflectionReader(memory).read_object("actor", **options)

    async def test_blueprint_only_and_inherited_provenance(self):
        m = ReflectionMemory()
        m.prop("Health", "IntProperty", 16, 4, owner=m.native)
        m.put(0x10010, 42, 4)
        m.prop("BPScore", "IntProperty", 128, 4, owner=m.blueprint)
        m.put(0x10080, 91, 4)
        r = await self.read(m)
        self.assertTrue(r["ok"], r)
        self.assertEqual(fields(r)["BPScore"]["data"]["value"], 91)
        self.assertTrue(fields(r)["BPScore"]["blueprint_defined"])
        self.assertFalse(fields(r)["Health"]["blueprint_defined"])
        self.assertEqual(
            list(fields(await self.read(m, include_inherited=False))), ["BPScore"]
        )

    async def test_nested_struct_exact_runtime_name_and_path(self):
        m = ReflectionMemory()
        m.nested()
        r = await self.read(m, property_path=["Profile"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(fields(r)["Score_ABC"]["data"]["value"], 73)
        r = await self.read(m, property_path=["Profile", "Weight"])
        self.assertEqual(r["value"]["value"], 2.5)

    async def test_array_of_structs_recurses_and_pages(self):
        m = ReflectionMemory()
        _, s = m.nested()
        inner = m.prop("Inner", "StructProperty", 0, 32, extra={"Struct": s})
        m.prop(
            "ProfilesArray",
            "ArrayProperty",
            160,
            16,
            owner=m.blueprint,
            extra={"Inner": inner},
        )
        p = m.array(0x100A0, [0] * 24)
        m.put(p, 11, 4)
        m.put(p + 32, 22, 4)
        m.put(p + 64, 33, 4)
        # Header counts elements, storage above reserves 3 struct-sized elements.
        m.values["(*(cast(FScriptArray *)0x100a0)).ArrayNum"] = 3
        m.values["(*(cast(FScriptArray *)0x100a0)).ArrayMax"] = 3
        m.put(0x100A8, 3, 4)
        m.put(0x100AC, 3, 4)
        r = await self.read(m, property_path=["ProfilesArray"], count=2)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"]["next_index"], 2)
        scores = [
            {f["name"]: f["data"]["value"] for f in x["data"]["fields"]}["Score_ABC"]
            for x in r["value"]["items"]
        ]
        self.assertEqual(scores, [11, 22])
        self.assertFalse(r["complete"])
        r = await self.read(m, property_path=["ProfilesArray", 2, "Score_ABC"])
        self.assertEqual(r["value"]["value"], 33)

    async def test_sparse_map_struct_values_and_physical_paths(self):
        m = ReflectionMemory()
        m.map()
        r = await self.read(m, property_path=["Profiles"], count=1, max_depth=4)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"]["next_index"], 1)
        self.assertEqual(r["value"]["items"][0]["key"]["value"], 10)
        r = await self.read(
            m, property_path=["Profiles"], start=1, count=8, max_depth=4
        )
        self.assertEqual([x["index"] for x in r["value"]["items"]], [2])
        r = await self.read(m, property_path=["Profiles", 2, "value", "Score_ABC"])
        self.assertEqual(r["value"]["value"], 93)
        r = await self.read(m, property_path=["Profiles", 1, "value"])
        self.assertEqual(r["error"]["code"], "INVALID_ARGUMENT")

    async def test_array_inside_struct_inside_map(self):
        m = ReflectionMemory()
        p = m.map()
        inner = m.prop("Inner", "IntProperty", 0, 4)
        # Each value's existing Profile schema gains an array at offset16.
        schema = int.from_bytes(bytes(m.memory[p + 48 + i] for i in range(8)), "little")
        schema = int.from_bytes(
            bytes(m.memory[schema + 40 + i] for i in range(8)), "little"
        )
        m.prop("Samples", "ArrayProperty", 16, 16, owner=schema, extra={"Inner": inner})
        sparse = "(cast(FScriptMap *)0x100a0)->Pairs.Elements"
        data = m.values[f"hex(cast(uint64)({sparse}.Data.AllocatorInstance.Data))"]
        m.array(data + 8 + 16, [4, 8])
        m.array(data + 96 + 8 + 16, [6, 12])
        r = await self.read(m, property_path=["Profiles"], max_depth=5)
        self.assertTrue(r["ok"], r)
        value = r["value"]["items"][1]["value"]
        samples = {f["name"]: f for f in value["fields"]}["Samples"]["data"]
        self.assertEqual([x["data"]["value"] for x in samples["items"]], [6, 12])

    async def test_fixed_array_of_struct_path(self):
        m = ReflectionMemory()
        p, s = m.nested()
        m.put(p + 24, 2, 4)
        m.put(0x100A0, 99, 4)
        r = await self.read(m, property_path=["Profile", 1, "Score_ABC"])
        self.assertEqual(r["value"]["value"], 99)

    async def test_object_identity_opt_in_and_cycle(self):
        m = ReflectionMemory()
        m.prop("Self", "ObjectProperty", 128, 8, owner=m.blueprint)
        m.put(0x10080, 0x10000)
        r = await self.read(m)
        self.assertEqual(fields(r)["Self"]["data"]["reference_policy"], "identity_only")
        r = await self.read(m, follow_objects=True)
        self.assertTrue(fields(r)["Self"]["data"]["object"]["cycle"])

    async def test_bool_mask_enum_and_numeric_width(self):
        m = ReflectionMemory()
        p = m.prop(
            "Flag",
            "BoolProperty",
            128,
            1,
            owner=m.blueprint,
            extra={"FieldSize": 1, "ByteOffset": 0, "FieldMask": 4},
        )
        m.put(0x10080, 6, 1)
        inner = m.prop("Underlying", "ByteProperty", 0, 1)
        m.prop(
            "Mode",
            "EnumProperty",
            129,
            1,
            owner=m.blueprint,
            extra={"UnderlyingProp": inner},
        )
        m.put(0x10081, 3, 1)
        r = await self.read(m)
        self.assertIs(fields(r)["Flag"]["data"]["value"], True)
        self.assertEqual(fields(r)["Mode"]["data"]["value"], 3)
        m.put(p + 28, 4, 4)
        r = await self.read(m)
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_metadata_only_reports_unsupported_fields_without_reading_them(self):
        m = ReflectionMemory()
        m.prop(
            "Delegate", "MulticastInlineDelegateProperty", 128, 16, owner=m.blueprint
        )
        r = await self.read(m, metadata_only=True)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("data", fields(r)["Delegate"])
        r = await self.read(m)
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["code"], "UNSUPPORTED_PROPERTY")

    async def test_depth_and_node_and_metadata_and_output_budgets(self):
        m = ReflectionMemory()
        m.nested()
        r = await self.read(m, max_depth=1)
        self.assertTrue(r["ok"])
        self.assertFalse(r["complete"])
        self.assertEqual(fields(r)["Profile"]["data"]["reason"], "DEPTH_LIMIT")
        for options, code in (
            (dict(max_nodes=1), "NODE_BUDGET_EXCEEDED"),
            (dict(max_metadata_fields=1), "METADATA_BUDGET_EXCEEDED"),
        ):
            r = await self.read(m, **options)
            self.assertEqual(r["error"]["code"], code)
        with patch.object(ReflectionReader, "MAX_OUTPUT_BYTES", 64):
            r = await self.read(m)
            self.assertEqual(r["error"]["code"], "OUTPUT_BUDGET_EXCEEDED")

    async def test_invalid_arguments_never_probe(self):
        for options in (
            dict(properties=[[]]),
            dict(properties=["x", "x"]),
            dict(property_path=[True]),
            dict(max_depth=-1),
            dict(count=0),
            dict(follow_objects=1),
            dict(properties=["x"], property_path=["y"]),
        ):
            m = ReflectionMemory()
            r = await self.read(m, **options)
            self.assertEqual(r["error"]["code"], "INVALID_ARGUMENT", r)
            self.assertEqual(m.calls, [])

    async def test_cycles_out_of_bounds_and_missing_property(self):
        for corruption, code in (
            ("next", "METADATA_CYCLE"),
            ("super", "METADATA_CYCLE"),
            ("span", "INVALID_LAYOUT"),
            ("missing", "PROPERTY_NOT_FOUND"),
        ):
            m = ReflectionMemory()
            p = m.prop("Value", "IntProperty", 128, 4, owner=m.blueprint)
            if corruption == "next":
                m.put(p + 8, p)
            elif corruption == "super":
                m.put(m.native + 24, m.blueprint)
            elif corruption == "span":
                m.put(p + 32, 255, 4)
            r = await self.read(
                m, properties=["Absent"] if corruption == "missing" else None
            )
            self.assertEqual(r["error"]["code"], code, r)

    async def test_failed_read_stale_epoch_and_cancellation_propagate(self):
        m = ReflectionMemory()
        m.nested()
        m.overrides["hex(*(cast(uint32 *)0x10080))"] = {"read_failed": True}
        r = await self.read(m)
        self.assertEqual(r["error"]["code"], "READ_FAILED")
        m = ReflectionMemory()
        m.overrides["sizeof(void *)"] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.read(m)
        m = ReflectionMemory()
        original = m.__call__

        async def change(e):
            result = await original(e)
            m.epoch += 1
            return result

        with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            await ReflectionReader(change).read_object("actor")

    async def test_map_layout_and_occupancy_rejected(self):
        m = ReflectionMemory()
        p = m.map()
        m.put(p + 76, 16, 4)
        r = await self.read(m, property_path=["Profiles"])
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")
        m = ReflectionMemory()
        m.map()
        m.values["(cast(FScriptMap *)0x100a0)->Pairs.Elements.NumFreeIndices"] = 0
        m.values["(cast(FScriptMap *)0x100a0)->Pairs.Elements.FirstFreeIndex"] = -1
        r = await self.read(m, property_path=["Profiles"])
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_memory_image_allocators_rejected_on_read_and_path(self):
        for typ, field in (
            ("ArrayProperty", "ArrayFlags"),
            ("MapProperty", "MapFlags"),
        ):
            m = ReflectionMemory()
            if typ == "MapProperty":
                p = m.map()
                path = ["Profiles", 0, "value"]
            else:
                inner = m.prop("Inner", "IntProperty", 0, 4)
                p = m.prop(
                    "Values", typ, 128, 16, owner=m.blueprint, extra={"Inner": inner}
                )
                path = ["Values", 0]
            off, _ = m.LAYOUT["F" + typ][field]
            m.put(p + off, 1, 4)
            r = await self.read(m, property_path=path)
            self.assertEqual(r["error"]["code"], "UNSUPPORTED_PROPERTY")

    async def test_unicode_runtime_field_names(self):
        m = ReflectionMemory()
        m.prop("中文变量_😀", "IntProperty", 128, 4, owner=m.blueprint)
        m.put(0x10080, 86, 4)
        r = await self.read(m, property_path=["中文变量_😀"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"]["value"], 86)

    async def test_root_class_change_is_detected_after_cached_reads(self):
        m = ReflectionMemory()
        m.nested()
        original = m.__call__
        reads = 0

        async def change(expression):
            nonlocal reads
            if expression == "hex(*(cast(uint64 *)0x10000))":
                reads += 1
                if reads == 2:
                    m.put(0x10000, m.native)
            return await original(expression)

        r = await ReflectionReader(change).read_object("actor")
        self.assertEqual(r["error"]["code"], "METADATA_CHANGED")

    async def test_probe_timeout_and_pdb_width_budgets(self):
        m = ReflectionMemory()
        with patch.object(ReflectionReader, "MAX_PROBES", 1):
            r = await self.read(m)
            self.assertEqual(r["error"]["code"], "PROBE_BUDGET_EXCEEDED")

        async def slow(expression):
            await asyncio.sleep(0.02)
            return await m(expression)

        with patch.object(ReflectionReader, "TIMEOUT_SECONDS", 0.001):
            r = await ReflectionReader(slow).read_object("actor")
            self.assertEqual(r["error"]["code"], "TIME_BUDGET_EXCEEDED")
        m.overrides["sizeof(raw((cast(UObject *)0x10000)->ClassPrivate))"] = {
            "value": "4"
        }
        r = await self.read(m)
        self.assertEqual(r["error"]["code"], "UNSUPPORTED_LAYOUT")

    async def test_selected_field_does_not_scan_unrelated_super_properties(self):
        m = ReflectionMemory()
        m.prop("Score", "IntProperty", 128, 4, owner=m.blueprint)
        # The unselected native schema is deliberately invalid. Selecting the
        # BP field must not need its bytes; a full metadata traversal must fail.
        m.put(m.native + 32, 1000, 4)
        r = await self.read(m, property_path=["Score"])
        self.assertTrue(r["ok"], r)
        r = await self.read(m, metadata_only=True)
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_opaque_struct_is_not_an_empty_completed_tree(self):
        m = ReflectionMemory()
        s = m.schema("InternalFrame", 8)
        m.prop(
            "Frame", "StructProperty", 128, 8, owner=m.blueprint, extra={"Struct": s}
        )
        r = await self.read(m, property_path=["Frame"])
        self.assertEqual(r["error"]["code"], "UNSUPPORTED_PROPERTY")

    async def test_new_tool_is_readonly_and_discoverable(self):
        from unittest.mock import MagicMock

        from raddebugger_mcp.server import build_server

        server = build_server(MagicMock())
        tools = await server.list_tools()
        tool = next(x for x in tools if x.name == "rad_read_object")
        self.assertEqual(len(tools), 22)
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertIn("property_path", tool.inputSchema["properties"])

    async def test_script_array_uses_inherited_allocator_and_checks_storage_span(self):
        m = ReflectionMemory()
        inner = m.prop("Inner", "IntProperty", 0, 4)
        p = m.prop(
            "Values",
            "ArrayProperty",
            128,
            16,
            owner=m.blueprint,
            extra={"Inner": inner},
        )
        m.array(0x10080, [7, 11, 19])
        r = await self.read(m, property_path=["Values"])
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["data"]["value"] for x in r["value"]["items"]], [7, 11, 19])
        self.assertFalse(
            any("FScriptArray" in x and "AllocatorInstance" in x for x in m.calls)
        )
        m.put(p + 28, 8, 4)
        r = await self.read(m, property_path=["Values", 0])
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_empty_script_array_null_allocation_and_corrupt_capacity(self):
        m = ReflectionMemory()
        inner = m.prop("Inner", "IntProperty", 0, 4)
        m.prop(
            "Values",
            "ArrayProperty",
            128,
            16,
            owner=m.blueprint,
            extra={"Inner": inner},
        )
        m.array(0x10080, [])
        m.put(0x10080, 0)
        r = await self.read(m, property_path=["Values"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"]["items"], [])
        m.put(0x1008C, 4, 4)
        r = await self.read(m, property_path=["Values"])
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_name_pool_prefix_stays_inside_block_and_long_name_continues(self):
        m = ReflectionMemory()
        name = "长名字😀" * 60
        m.prop(name, "IntProperty", 128, 4, owner=m.blueprint)
        m.put(0x10080, 102, 4)
        r = await self.read(m, property_path=[name])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["value"]["value"], 102)
        m = ReflectionMemory()
        p = m.prop("Bad", "IntProperty", 128, 4, owner=m.blueprint)
        m.put(p + 16, 65535, 4)
        m.put(0x800000 + 131070, 2 << 6, 2)
        r = await self.read(m)
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")

    async def test_reflected_map_storage_size_cannot_be_guessed(self):
        m = ReflectionMemory()
        p = m.map()
        m.put(p + 28, 8, 4)
        r = await self.read(m, property_path=["Profiles"])
        self.assertEqual(r["error"]["code"], "INVALID_LAYOUT")


if __name__ == "__main__":
    unittest.main()
