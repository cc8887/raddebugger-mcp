"""UE4.26 Win64 runtime FProperty reader, independent of editor RPC/plugins.

PDBs supply field offsets; live UClass/UStruct/FProperty supply runtime schema.
No target functions, Python reflection, or memory writes are used by this module.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import time
from typing import Any

from .value_reader import ValueReader, _ReadError, validate_expression

_NUMERIC = {
    "Int8Property": ("b", 1),
    "Int16Property": ("h", 2),
    "IntProperty": ("i", 4),
    "Int64Property": ("q", 8),
    "UInt16Property": ("H", 2),
    "UInt32Property": ("I", 4),
    "UInt64Property": ("Q", 8),
    "ByteProperty": ("B", 1),
    "FloatProperty": ("f", 4),
    "DoubleProperty": ("d", 8),
}
_OBJECT = {"ObjectProperty", "ClassProperty"}


class ReflectionReader(ValueReader):
    MAX_PROBES = 4096
    TIMEOUT_SECONDS = 60.0
    MAX_OUTPUT_BYTES = 512 * 1024

    async def read_object(
        self,
        expression: str,
        *,
        properties: list[str] | None = None,
        property_path: list[str | int] | None = None,
        include_inherited: bool = True,
        metadata_only: bool = False,
        start: int = 0,
        count: int = 16,
        max_depth: int = 3,
        max_elements: int = 8,
        max_properties: int = 16,
        max_nodes: int = 256,
        max_string_units: int = 256,
        max_metadata_fields: int = 1024,
        follow_objects: bool = False,
    ) -> dict[str, Any]:
        self._probes = 0
        self._epoch = None
        self._started = time.monotonic()
        self._root_expression = expression
        self._result = {
            "ok": False,
            "expression": expression,
            "layout": self.layout,
            "representation": "ue4_26_runtime_properties",
            "source": "live_FProperty_and_PDB_layout",
        }
        self._offsets = {}
        self._sizes = {}
        self._memory = {}
        self._names = {}
        self._blocks = {}
        self._descriptors = {}
        self._field_lists = {}
        self._identities = {}
        self._field_classes = {}
        self._nodes = 0
        self._scanned = 0
        self._active_objects = set()
        self._errors = []
        try:
            validate_expression(expression)
            if self.layout != "ue4_26":
                raise _ReadError(
                    "UNSUPPORTED_LAYOUT",
                    "Only UE4.26 Win64 runtime FProperty layout is supported.",
                )
            for name, value, lo, hi in (
                ("start", start, 0, 2**31 - 1),
                ("count", count, 1, 128),
                ("max_depth", max_depth, 0, 8),
                ("max_elements", max_elements, 1, 128),
                ("max_properties", max_properties, 1, 128),
                ("max_nodes", max_nodes, 1, 1024),
                ("max_string_units", max_string_units, 1, 4096),
                ("max_metadata_fields", max_metadata_fields, 1, 2048),
            ):
                if type(value) is not int or not lo <= value <= hi:
                    raise _ReadError("INVALID_ARGUMENT", f"{name} outside [{lo},{hi}].")
            if any(
                type(v) is not bool
                for v in (include_inherited, metadata_only, follow_objects)
            ):
                raise _ReadError(
                    "INVALID_ARGUMENT", "Boolean options must be booleans."
                )
            path = [] if property_path is None else property_path
            if (
                not isinstance(path, list)
                or len(path) > 32
                or any(
                    type(x) not in (str, int)
                    or (type(x) is str and (not x or len(x) > 1024))
                    or (type(x) is int and not 0 <= x < 2**31)
                    for x in path
                )
            ):
                raise _ReadError(
                    "INVALID_ARGUMENT",
                    "property_path must contain at most 32 field names/indices.",
                )
            if properties is not None and (
                not isinstance(properties, list)
                or len(properties) > 128
                or any(
                    not isinstance(x, str) or not x or len(x) > 1024 for x in properties
                )
                or len(set(properties)) != len(properties)
            ):
                raise _ReadError(
                    "INVALID_ARGUMENT",
                    "properties must be unique runtime field names (at most 128).",
                )
            if properties is not None and path:
                raise _ReadError(
                    "INVALID_ARGUMENT", "Use properties or property_path, not both."
                )
            self._string_limit = max_string_units
            self._element_limit = max_elements
            self._property_limit = max_properties
            self._node_limit = max_nodes
            self._metadata_limit = max_metadata_fields
            self._follow = follow_objects
            self._meta_only = metadata_only
            async with asyncio.timeout(self.TIMEOUT_SECONDS):
                if (
                    await self._int("sizeof(void *)") != 8
                    or await self._int("sizeof(uint16)") != 2
                ):
                    raise _ReadError(
                        "UNSUPPORTED_LAYOUT",
                        "64-bit pointers and 16-bit UTF16 are required.",
                    )
                self._name_size = await self._int("sizeof(FName)")
                if self._name_size not in (8, 12):
                    raise _ReadError("UNSUPPORTED_LAYOUT", "Unknown FName layout.")
                pointer = await self._int(f"hex(cast(uint64)({expression}))")
                self._ptr(pointer)
                identity = await self._identity(pointer)
                self._result["object"] = identity
                if path:
                    desc, address = await self._navigate(
                        pointer, path, include_inherited
                    )
                    value = await self._decode(
                        desc, address, max_depth, path, start, count
                    )
                else:
                    value = await self._object(
                        pointer,
                        max_depth,
                        path,
                        start,
                        count,
                        include_inherited,
                        properties,
                    )
                self._memory.clear()
                if await self._get("UObject", pointer, "ClassPrivate") != int(
                    identity["class"]["address"], 16
                ):
                    raise _ReadError(
                        "METADATA_CHANGED",
                        "Root object's class changed during reading.",
                    )
                self._result.update(
                    value=value,
                    ok=not self._errors,
                    complete=value.get("complete", False),
                    truncated=not value.get("complete", False),
                    errors=self._errors,
                    object_reference_policy="recursive"
                    if follow_objects
                    else "identity_only",
                    limits=dict(
                        max_depth=max_depth,
                        max_elements=max_elements,
                        max_properties=max_properties,
                        max_nodes=max_nodes,
                        max_metadata_fields=max_metadata_fields,
                        max_string_units=max_string_units,
                    ),
                )
        except _ReadError as error:
            self._result.update(
                ok=False,
                complete=False,
                error={"code": error.code, "message": str(error)},
            )
        except TimeoutError:
            self._result.update(
                ok=False,
                complete=False,
                error={
                    "code": "TIME_BUDGET_EXCEEDED",
                    "message": "Runtime property read exceeded 60 seconds.",
                },
            )
        self._result.update(
            probes=self._probes,
            nodes=self._nodes,
            metadata_fields_scanned=self._scanned,
            elapsed_seconds=round(time.monotonic() - self._started, 4),
        )
        if self._epoch:
            self._result["snapshot"] = self._epoch
        if (
            len(json.dumps(self._result, ensure_ascii=False).encode("utf-8"))
            > self.MAX_OUTPUT_BYTES
        ):
            return {
                "ok": False,
                "complete": False,
                "error": {
                    "code": "OUTPUT_BUDGET_EXCEEDED",
                    "message": "Runtime property result exceeds 512 KiB.",
                },
                "expression": expression,
                "probes": self._probes,
                "nodes": self._nodes,
            }
        return self._result

    @staticmethod
    def _ptr(value: int, nullable: bool = False):
        if value == 0 and nullable:
            return
        if value < 0x10000 or value >= 1 << 47 or value % 8:
            raise _ReadError(
                "INVALID_LAYOUT",
                "Noncanonical, unaligned, or null metadata/object pointer.",
            )

    async def _bytes(self, address: int, size: int) -> bytes:
        if not 0 <= size <= 1 << 20 or address < 0x10000 or address + size >= 1 << 47:
            raise _ReadError("INVALID_LAYOUT", "Invalid memory span.")
        if all(address + i in self._memory for i in range(size)):
            return bytes(self._memory[address + i] for i in range(size))
        value = await self._units(address, size, wide=False)
        self._memory.update((address + i, b) for i, b in enumerate(value))
        return value

    async def _layout(self, typ: str, field: str, base: int, size: int) -> int:
        key = (typ, field)
        if key not in self._offsets:
            expr = f"(cast({typ} *)0x{base:x})->{field}"
            offset = int(await self._address(expr), 16) - base
            actual = await self._int(f"sizeof(raw({expr}))")
            if not 0 <= offset <= 65536 or actual != size:
                raise _ReadError(
                    "UNSUPPORTED_LAYOUT",
                    f"PDB field {typ}.{field} has unexpected width or offset.",
                )
            self._offsets[key] = offset
        return self._offsets[key]

    async def _get(
        self, typ: str, base: int, field: str, size: int = 8, *, signed: bool = False
    ) -> int:
        offset = await self._layout(typ, field, base, size)
        return int.from_bytes(
            await self._bytes(base + offset, size), "little", signed=signed
        )

    async def _packed(
        self, typ: str, base: int, fields: dict[str, int]
    ) -> dict[str, int]:
        offsets = {
            k: await self._layout(typ, k, base, size) for k, size in fields.items()
        }
        end = max(offsets[k] + size for k, size in fields.items())
        if end > 1024:
            raise _ReadError("UNSUPPORTED_LAYOUT", "Unexpected reflection header span.")
        raw = await self._bytes(base, end)
        return {
            k: int.from_bytes(raw[offsets[k] : offsets[k] + size], "little")
            for k, size in fields.items()
        }

    async def _name(self, address: int) -> str:
        raw = await self._bytes(address, self._name_size)
        index_field = "DisplayIndex" if self._name_size == 12 else "ComparisonIndex"
        io = await self._layout("FName", index_field, address, 4)
        no = await self._layout("FName", "Number", address, 4)
        identifier = int.from_bytes(raw[io : io + 4], "little")
        number = int.from_bytes(raw[no : no + 4], "little")
        if identifier >= 1 << 29 or number >= 1 << 31:
            raise _ReadError("INVALID_LAYOUT", "Invalid FName id/number.")
        cache = (identifier, number)
        if cache in self._names:
            return self._names[cache]
        block, offset = identifier >> 16, identifier & 65535
        if block not in self._blocks:
            pointer = await self._maybe_int(
                f"hex(cast(uint64)(GNameBlocksDebug[{block}]))"
            )
            if pointer is None:
                pointer = await self._maybe_int(
                    f"hex(cast(uint64)((cast(FNamePool *)(&NamePoolData))->Entries.Blocks[{block}]))"
                )
            if pointer is None:
                raise _ReadError("UNSUPPORTED_LAYOUT", "UE4 name pool is unavailable.")
            self._ptr(pointer)
            self._blocks[block] = pointer
        entry = self._blocks[block] + offset * (4 if self._name_size == 12 else 2)
        ho = await self._layout("FNameEntry", "Header", entry, 2)
        to = await self._layout("FNameEntry", "AnsiName", entry, 1024)
        if ho != (4 if self._name_size == 12 else 0) or to != ho + 2:
            raise _ReadError(
                "UNSUPPORTED_LAYOUT", "FName entry offsets do not match UE4.26."
            )
        # UE4.26 allocates each complete name block (65536 entry strides).
        # A bounded prefix combines header/text probes without crossing that
        # allocation, including entries near its last byte. Long names continue
        # with exact reads. Overlapping prefixes are cached only for this read.
        available = (65536 - offset) * (4 if self._name_size == 12 else 2)
        prefix = await self._bytes(entry, min(128, available))
        if ho + 2 > len(prefix):
            raise _ReadError("INVALID_LAYOUT", "FName header exceeds its block.")
        header = int.from_bytes(prefix[ho : ho + 2], "little")
        wide, length = header & 1, header >> (1 if self._name_size == 12 else 6)
        if not 0 < length <= 1023:
            raise _ReadError("INVALID_LAYOUT", "Invalid FName entry length.")
        if to + length * (2 if wide else 1) > available:
            raise _ReadError("INVALID_LAYOUT", "FName text exceeds its block.")
        try:
            value = (await self._bytes(entry + to, length * (2 if wide else 1))).decode(
                "utf-16-le" if wide else "ascii", "strict"
            )
        except UnicodeDecodeError as error:
            raise _ReadError(
                "INVALID_ENCODING", "Invalid reflected field/type name."
            ) from error
        value += f"_{number - 1}" if number else ""
        self._names[cache] = value
        return value

    async def _identity(self, pointer: int) -> dict[str, Any]:
        self._ptr(pointer)
        if pointer in self._identities:
            return self._identities[pointer]
        c = await self._get("UObject", pointer, "ClassPrivate")
        self._ptr(c)
        no = await self._layout("UObject", "NamePrivate", pointer, self._name_size)
        cn = await self._layout("UObject", "NamePrivate", c, self._name_size)
        result = {
            "address": hex(pointer),
            "name": await self._name(pointer + no),
            "class": {"address": hex(c), "name": await self._name(c + cn)},
        }
        self._identities[pointer] = result
        return result

    async def _descriptor(self, pointer: int) -> dict[str, Any]:
        self._ptr(pointer)
        if pointer in self._descriptors:
            return self._descriptors[pointer]
        self._scanned += 1
        if self._scanned > self._metadata_limit:
            raise _ReadError(
                "METADATA_BUDGET_EXCEEDED", "FProperty traversal limit reached."
            )
        h = await self._packed(
            "FProperty",
            pointer,
            {
                "ClassPrivate": 8,
                "Next": 8,
                "ArrayDim": 4,
                "ElementSize": 4,
                "Offset_Internal": 4,
                "NamePrivate": self._name_size,
            },
        )
        if (
            not 1 <= h["ArrayDim"] <= 1024
            or not 1 <= h["ElementSize"] <= 1 << 24
            or h["Offset_Internal"] >= 1 << 30
        ):
            raise _ReadError(
                "INVALID_LAYOUT", "Invalid FProperty size/array/offset metadata."
            )
        cls = h["ClassPrivate"]
        self._ptr(cls)
        if cls not in self._field_classes:
            off = await self._layout("FFieldClass", "Name", cls, self._name_size)
            self._field_classes[cls] = await self._name(cls + off)
        off = await self._layout("FProperty", "NamePrivate", pointer, self._name_size)
        d = dict(
            pointer=pointer,
            name=await self._name(pointer + off),
            type=self._field_classes[cls],
            offset=h["Offset_Internal"],
            size=h["ElementSize"],
            dim=h["ArrayDim"],
            next=h["Next"],
        )
        self._descriptors[pointer] = d
        return d

    async def _fields(
        self,
        pointer: int,
        inherited: bool,
        is_class: bool,
        wanted: set[str] | None = None,
    ) -> tuple[list[dict], int]:
        key = (pointer, inherited, is_class, tuple(sorted(wanted)) if wanted else None)
        if key in self._field_lists:
            return self._field_lists[key]
        result = []
        seen_classes = set()
        owner = pointer
        root_size = 0
        remaining = set(wanted) if wanted else None
        while owner:
            self._ptr(owner)
            if owner in seen_classes:
                raise _ReadError("METADATA_CYCLE", "UStruct super chain is cyclic.")
            seen_classes.add(owner)
            h = await self._packed(
                "UStruct",
                owner,
                {"ChildProperties": 8, "SuperStruct": 8, "PropertiesSize": 4},
            )
            if not 0 < h["PropertiesSize"] <= 1 << 26:
                raise _ReadError("INVALID_LAYOUT", "Invalid UStruct property size.")
            if not root_size:
                root_size = h["PropertiesSize"]
            elif h["PropertiesSize"] > root_size:
                raise _ReadError(
                    "INVALID_LAYOUT",
                    "Superclass storage exceeds the root object's schema.",
                )
            identity = await self._identity(owner)
            blueprint = (
                bool(await self._get("UClass", owner, "ClassFlags", 4) & 0x40000)
                if is_class
                else identity["class"]["name"] == "UserDefinedStruct"
            )
            field = h["ChildProperties"]
            seen = set()
            while field:
                self._ptr(field)
                if field in seen:
                    raise _ReadError(
                        "METADATA_CYCLE", "FProperty next chain is cyclic."
                    )
                seen.add(field)
                d = dict(await self._descriptor(field))
                if d["offset"] + d["size"] * d["dim"] > h["PropertiesSize"]:
                    raise _ReadError(
                        "INVALID_LAYOUT", "Property span exceeds its declaring UStruct."
                    )
                d.update(
                    declared_by=identity["name"],
                    declaring_address=hex(owner),
                    blueprint_defined=blueprint,
                )
                result.append(d)
                if remaining is not None:
                    remaining.discard(d["name"])
                    if not remaining:
                        self._field_lists[key] = (result, root_size)
                        return result, root_size
                field = d["next"]
            owner = h["SuperStruct"] if inherited else 0
        self._field_lists[key] = (result, root_size)
        return result, root_size

    def _claim(self):
        self._nodes += 1
        if self._nodes > self._node_limit:
            raise _ReadError(
                "NODE_BUDGET_EXCEEDED", "Recursive value node budget reached."
            )

    @staticmethod
    def _limit(kind, address, path, reason):
        return dict(
            kind=kind,
            address=hex(address),
            property_path=path,
            expanded=False,
            complete=False,
            truncated=True,
            reason=reason,
        )

    async def _object(
        self, pointer, depth, path, start, count, inherited=True, selected=None
    ):
        self._claim()
        identity = await self._identity(pointer)
        if pointer in self._active_objects:
            return dict(kind="reference", **identity, cycle=True, complete=True)
        if depth == 0 and not self._meta_only:
            return {**self._limit("uobject", pointer, path, "DEPTH_LIMIT"), **identity}
        self._active_objects.add(pointer)
        try:
            fields, _ = await self._fields(
                int(identity["class"]["address"], 16),
                inherited,
                True,
                set(selected) if selected else None,
            )
            if selected is not None:
                found = {x["name"] for x in fields}
                missing = set(selected) - found
                if missing:
                    raise _ReadError(
                        "PROPERTY_NOT_FOUND",
                        "Requested runtime property not found: "
                        + ", ".join(sorted(missing)),
                    )
                fields = [x for x in fields if x["name"] in selected]
            node = await self._members(pointer, fields, depth, path, start, count)
            return dict(kind="uobject", **identity, **node)
        finally:
            self._active_objects.remove(pointer)

    async def _members(self, address, fields, depth, path, start, count):
        items = []
        for index, d in enumerate(fields[start : start + count], start):
            item = {
                k: d[k]
                for k in (
                    "name",
                    "type",
                    "offset",
                    "size",
                    "dim",
                    "declared_by",
                    "declaring_address",
                    "blueprint_defined",
                )
            }
            item.update(
                index=index,
                property_address=hex(d["pointer"]),
                property_path=path + [d["name"]],
            )
            if not self._meta_only:
                try:
                    item["data"] = await self._decode(
                        d,
                        address + d["offset"],
                        depth - 1,
                        path + [d["name"]],
                        0,
                        self._element_limit,
                    )
                except _ReadError as error:
                    if error.code != "UNSUPPORTED_PROPERTY":
                        raise
                    item["data"] = dict(
                        kind="unsupported",
                        complete=False,
                        error={"code": error.code, "message": str(error)},
                    )
                    self._errors.append(
                        dict(
                            property_path=path + [d["name"]],
                            code=error.code,
                            message=str(error),
                        )
                    )
            items.append(item)
        end = min(start + count, len(fields))
        partial = end < len(fields) or any(
            not x.get("data", {}).get("complete", True) for x in items
        )
        return dict(
            fields=items,
            total_properties=len(fields),
            start=start,
            next_index=end if end < len(fields) else None,
            complete=not partial,
            truncated=partial,
            metadata_only=self._meta_only,
        )

    async def _container_size(self, d, typ):
        if typ not in self._sizes:
            self._sizes[typ] = await self._int(f"sizeof({typ})")
        if not 0 < self._sizes[typ] <= 1024 or d["size"] != self._sizes[typ]:
            raise _ReadError(
                "INVALID_LAYOUT", f"{typ} storage size differs from FProperty metadata."
            )

    async def _script_array(self, address):
        # FScriptArray inherits FHeapAllocator::ForAnyElementType, unlike
        # TArray's AllocatorInstance member. PDB-derived offsets cover that base.
        h = await self._packed(
            "FScriptArray", address, {"Data": 8, "ArrayNum": 4, "ArrayMax": 4}
        )
        n, capacity, pointer = h["ArrayNum"], h["ArrayMax"], h["Data"]
        if not 0 <= n <= capacity <= 2**31 - 1:
            raise _ReadError(
                "INVALID_LAYOUT", "Reflected array count/capacity is invalid."
            )
        self._ptr(pointer, nullable=capacity == 0)
        return n, capacity, pointer

    async def _map_header(self, d, address):
        await self._container_size(d, "FScriptMap")
        meta = d["pointer"]
        if await self._get("FMapProperty", meta, "MapFlags", 4) != 0:
            raise _ReadError(
                "UNSUPPORTED_PROPERTY", "Memory-image map allocator is not supported."
            )
        key = await self._descriptor(await self._get("FMapProperty", meta, "KeyProp"))
        value = await self._descriptor(
            await self._get("FMapProperty", meta, "ValueProp")
        )
        if key["dim"] != 1 or value["dim"] != 1:
            raise _ReadError(
                "INVALID_LAYOUT", "Map key/value metadata has a fixed array dimension."
            )
        lo = await self._layout("FMapProperty", "MapLayout", meta, 24)
        layout = meta + lo
        vo = await self._get("FScriptMapLayout", layout, "ValueOffset", 4)
        so = await self._layout("FScriptMapLayout", "SetLayout", layout, 20)
        ao = await self._layout("FScriptSetLayout", "SparseArrayLayout", layout + so, 8)
        stride = await self._get(
            "FScriptSparseArrayLayout", layout + so + ao, "Size", 4
        )
        alignment = await self._get(
            "FScriptSparseArrayLayout", layout + so + ao, "Alignment", 4
        )
        if (
            not 0 < alignment <= 128
            or alignment & (alignment - 1)
            or not 8 <= stride <= 1 << 24
            or stride % alignment
            or key["size"] > vo
            or vo + value["size"] > stride
        ):
            raise _ReadError(
                "INVALID_LAYOUT",
                "FScriptMapLayout is inconsistent with key/value metadata.",
            )
        sparse = f"(cast(FScriptMap *)0x{address:x})->Pairs.Elements"
        slots, capacity, pointer = await self._script_array(
            int(await self._address(f"{sparse}.Data"), 16)
        )
        free = await self._int(f"{sparse}.NumFreeIndices")
        first = await self._int(f"{sparse}.FirstFreeIndex")
        bits = f"{sparse}.AllocationFlags"
        n = await self._int(f"{bits}.NumBits")
        maximum = await self._int(f"{bits}.MaxBits")
        if (
            not 0 <= free <= slots
            or n != slots
            or not n <= maximum <= 1 << 30
            or (free and not 0 <= first < slots)
            or (not free and first != -1)
        ):
            raise _ReadError(
                "INVALID_LAYOUT", "Reflected map sparse header is invalid."
            )
        bitmap, _ = await self._allocation(bits)
        return dict(
            key=key,
            value=value,
            value_offset=vo,
            stride=stride,
            slots=slots,
            count=slots - free,
            data=pointer,
            bitmap=bitmap,
        )

    async def _decode(self, d, address, depth, path, start, count):
        self._claim()
        typ = d["type"]
        base = dict(type=typ, address=hex(address), property_path=path)
        if d["dim"] > 1:
            if depth == 0:
                return {
                    **base,
                    **self._limit("fixed_array", address, path, "DEPTH_LIMIT"),
                }
            single = {**d, "dim": 1}
            items = []
            for i in range(start, min(d["dim"], start + count)):
                items.append(
                    dict(
                        index=i,
                        data=await self._decode(
                            single,
                            address + i * d["size"],
                            depth - 1,
                            path + [i],
                            0,
                            self._element_limit,
                        ),
                    )
                )
            end = min(d["dim"], start + count)
            complete = end == d["dim"] and all(x["data"].get("complete") for x in items)
            return dict(
                **base,
                kind="fixed_array",
                count=d["dim"],
                items=items,
                next_index=end if end < d["dim"] else None,
                complete=complete,
                truncated=not complete,
            )
        if typ in _NUMERIC:
            fmt, size = _NUMERIC[typ]
            if d["size"] != size:
                raise _ReadError("INVALID_LAYOUT", "Numeric property size mismatch.")
            value = struct.unpack("<" + fmt, await self._bytes(address, size))[0]
            if isinstance(value, float) and not math.isfinite(value):
                raise _ReadError("INVALID_VALUE", "Nonfinite numeric property.")
            return dict(**base, kind="scalar", value=value, complete=True)
        if typ == "BoolProperty":
            meta = d["pointer"]
            size = await self._get("FBoolProperty", meta, "FieldSize", 1)
            offset = await self._get("FBoolProperty", meta, "ByteOffset", 1)
            mask = await self._get("FBoolProperty", meta, "FieldMask", 1)
            if (
                not 1 <= size <= 8
                or size != d["size"]
                or not offset < size
                or not 0 < mask <= 255
            ):
                raise _ReadError(
                    "INVALID_LAYOUT", "Invalid reflected boolean bitfield."
                )
            return dict(
                **base,
                kind="bool",
                value=bool((await self._bytes(address + offset, 1))[0] & mask),
                complete=True,
            )
        if typ == "NameProperty":
            if d["size"] != self._name_size:
                raise _ReadError("INVALID_LAYOUT", "FName property size mismatch.")
            return dict(
                **base, kind="fname", value=await self._name(address), complete=True
            )
        if typ in ("StrProperty", "TextProperty"):
            expected = 16 if typ == "StrProperty" else 24
            if d["size"] != expected:
                raise _ReadError("INVALID_LAYOUT", "Text property size mismatch.")
            e = f"(*(cast({'FString' if typ == 'StrProperty' else 'FText'} *)0x{address:x}))"
            value = await (
                self._fstring(e, start)
                if typ == "StrProperty"
                else self._ftext(e, start)
            )
            return dict(
                **base, kind="fstring" if typ == "StrProperty" else "ftext", **value
            )
        if typ in _OBJECT:
            if d["size"] != 8:
                raise _ReadError(
                    "INVALID_LAYOUT", "Object property pointer width mismatch."
                )
            pointer = int.from_bytes(await self._bytes(address, 8), "little")
            if not pointer:
                return dict(**base, kind="object_reference", value=None, complete=True)
            self._ptr(pointer)
            if self._follow:
                result = await self._object(
                    pointer, depth, path, start, count, True, None
                )
                return dict(
                    **base,
                    kind="object_reference",
                    value=hex(pointer),
                    object=result,
                    complete=result.get("complete", False),
                )
            return dict(
                **base,
                kind="object_reference",
                value=hex(pointer),
                object=await self._identity(pointer),
                expanded=False,
                reference_policy="identity_only",
                complete=True,
            )
        if typ == "EnumProperty":
            inner = await self._descriptor(
                await self._get("FEnumProperty", d["pointer"], "UnderlyingProp")
            )
            if inner["type"] not in _NUMERIC or inner["size"] != d["size"]:
                raise _ReadError("INVALID_LAYOUT", "Enum underlying property mismatch.")
            result = await self._decode(
                {**inner, "dim": 1}, address, depth, path, start, count
            )
            return {**result, "type": typ, "underlying_type": inner["type"]}
        if typ in ("StructProperty", "ArrayProperty", "MapProperty") and depth == 0:
            return {**base, **self._limit(typ, address, path, "DEPTH_LIMIT")}
        if typ == "StructProperty":
            pointer = await self._get("FStructProperty", d["pointer"], "Struct")
            self._ptr(pointer)
            fields, size = await self._fields(pointer, True, False)
            if not fields:
                raise _ReadError(
                    "UNSUPPORTED_PROPERTY",
                    "Struct has no reflected child fields; opaque storage was not decoded.",
                )
            if size > d["size"]:
                raise _ReadError(
                    "INVALID_LAYOUT",
                    "Struct property storage is smaller than its schema.",
                )
            node = await self._members(
                address, fields, depth, path, start, min(count, self._property_limit)
            )
            return dict(
                **base, kind="struct", struct=await self._identity(pointer), **node
            )
        if typ == "ArrayProperty":
            await self._container_size(d, "FScriptArray")
            if await self._get("FArrayProperty", d["pointer"], "ArrayFlags", 4) != 0:
                raise _ReadError(
                    "UNSUPPORTED_PROPERTY",
                    "Memory-image array allocator is not supported.",
                )
            inner = await self._descriptor(
                await self._get("FArrayProperty", d["pointer"], "Inner")
            )
            if inner["dim"] != 1:
                raise _ReadError(
                    "INVALID_LAYOUT",
                    "Array inner metadata has a fixed array dimension.",
                )
            n, capacity, pointer = await self._script_array(address)
            items = []
            for i in range(start, min(n, start + count)):
                items.append(
                    dict(
                        index=i,
                        data=await self._decode(
                            inner,
                            pointer + i * inner["size"],
                            depth - 1,
                            path + [i],
                            0,
                            self._element_limit,
                        ),
                    )
                )
            end = min(n, start + count)
            partial = end < n or any(not x["data"].get("complete") for x in items)
            return dict(
                **base,
                kind="tarray",
                count=n,
                capacity=capacity,
                items=items,
                next_index=end if end < n else None,
                complete=not partial,
                truncated=partial,
            )
        if typ == "MapProperty":
            h = await self._map_header(d, address)
            items = []
            index = min(start, h["slots"])
            scanned = 0
            active = 0
            while index < h["slots"] and scanned < 1024 and len(items) < count:
                word = int.from_bytes(
                    await self._bytes(h["bitmap"] + (index // 32) * 4, 4), "little"
                )
                if word & (1 << (index % 32)):
                    active += 1
                    pointer = h["data"] + index * h["stride"]
                    key = await self._decode(
                        h["key"],
                        pointer,
                        depth - 1,
                        path + [index, "key"],
                        0,
                        self._element_limit,
                    )
                    value = await self._decode(
                        h["value"],
                        pointer + h["value_offset"],
                        depth - 1,
                        path + [index, "value"],
                        0,
                        self._element_limit,
                    )
                    items.append(dict(index=index, key=key, value=value))
                index += 1
                scanned += 1
            if start == 0 and index == h["slots"] and active != h["count"]:
                raise _ReadError(
                    "INVALID_LAYOUT", "Map occupancy differs from live count."
                )
            partial = index < h["slots"] or any(
                not x["key"].get("complete") or not x["value"].get("complete")
                for x in items
            )
            return dict(
                **base,
                kind="tmap",
                count=h["count"],
                slot_count=h["slots"],
                stride=h["stride"],
                value_offset=h["value_offset"],
                items=items,
                start=start,
                next_index=index if index < h["slots"] else None,
                complete=not partial,
                truncated=partial,
            )
        raise _ReadError(
            "UNSUPPORTED_PROPERTY", f"Unsupported reflected property class: {typ}"
        )

    async def _navigate(self, pointer, path, inherited):
        cls = int((await self._identity(pointer))["class"]["address"], 16)
        desc = None
        address = pointer
        schema = cls
        is_class = True
        pos = 0
        while pos < len(path):
            if desc and desc["dim"] > 1:
                i = path[pos]
                if type(i) is not int or not 0 <= i < desc["dim"]:
                    raise _ReadError("INVALID_ARGUMENT", "Invalid fixed array index.")
                address += i * desc["size"]
                desc = {**desc, "dim": 1}
                pos += 1
            elif desc is None or desc["type"] == "StructProperty":
                if desc:
                    schema = await self._get(
                        "FStructProperty", desc["pointer"], "Struct"
                    )
                    is_class = False
                if type(path[pos]) is not str:
                    raise _ReadError(
                        "INVALID_ARGUMENT", "Expected a runtime field name."
                    )
                fields, _ = await self._fields(
                    schema, inherited if is_class else True, is_class, {path[pos]}
                )
                match = [x for x in fields if x["name"] == path[pos]]
                if not match:
                    raise _ReadError(
                        "PROPERTY_NOT_FOUND", f"Runtime field not found: {path[pos]}"
                    )
                desc = match[0]
                address += desc["offset"]
                pos += 1
            elif desc["type"] in _OBJECT:
                pointer = int.from_bytes(await self._bytes(address, 8), "little")
                self._ptr(pointer)
                address = pointer
                schema = int((await self._identity(pointer))["class"]["address"], 16)
                desc = None
                is_class = True
            elif desc["type"] == "ArrayProperty":
                await self._container_size(desc, "FScriptArray")
                if (
                    await self._get("FArrayProperty", desc["pointer"], "ArrayFlags", 4)
                    != 0
                ):
                    raise _ReadError(
                        "UNSUPPORTED_PROPERTY",
                        "Memory-image array allocator is not supported.",
                    )
                i = path[pos]
                n, _, p = await self._script_array(address)
                if type(i) is not int or not 0 <= i < n:
                    raise _ReadError("INVALID_ARGUMENT", "Invalid dynamic array index.")
                desc = await self._descriptor(
                    await self._get("FArrayProperty", desc["pointer"], "Inner")
                )
                if desc["dim"] != 1:
                    raise _ReadError(
                        "INVALID_LAYOUT",
                        "Array inner metadata has a fixed array dimension.",
                    )
                address = p + i * desc["size"]
                pos += 1
            elif desc["type"] == "MapProperty":
                i = path[pos]
                if (
                    type(i) is not int
                    or pos + 1 >= len(path)
                    or path[pos + 1] not in ("key", "value")
                ):
                    raise _ReadError(
                        "INVALID_ARGUMENT",
                        "Map paths require a physical slot and key/value.",
                    )
                h = await self._map_header(desc, address)
                if not 0 <= i < h["slots"] or not int.from_bytes(
                    await self._bytes(h["bitmap"] + i // 32 * 4, 4), "little"
                ) & (1 << (i % 32)):
                    raise _ReadError(
                        "INVALID_ARGUMENT",
                        "Map path selects a free or invalid sparse slot.",
                    )
                part = path[pos + 1]
                desc = h[part]
                address = (
                    h["data"]
                    + i * h["stride"]
                    + (h["value_offset"] if part == "value" else 0)
                )
                pos += 2
            else:
                raise _ReadError(
                    "INVALID_ARGUMENT", "Cannot descend into this property type."
                )
        if desc is None:
            raise _ReadError(
                "INVALID_ARGUMENT", "Property path does not select a property."
            )
        return desc, address
