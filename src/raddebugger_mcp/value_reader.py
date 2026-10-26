"""Bounded, expression-only readers for the explicitly selected UE4.26 layout.

The caller owns the session mutation lock and validates the full stopped context
before/after *each* callback. No expression generated here calls target code.
Unknown layouts are errors, never an empty value. This is not a UE ABI promise.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import math
import re
import time
from typing import Any

from .errors import RadError


EvalRaw = Callable[[str], Awaitable[dict[str, Any]]]
_INTEGER = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)")
_TYPE = re.compile(r"[A-Za-z_][A-Za-z_0-9\s:*,<>&\[\]]*")
_SCALAR = re.compile(r"(?:bool|char|wchar_t|float|double|(?:u?int|uint|__int)(?:8|16|32|64)?(?:_t)?|(?:signed |unsigned )?(?:char|short|int|long(?: long)?|__int64))")
_EPOCH_FIELDS = ("run_gen", "stop_count", "process_id", "thread_id", "unwind_count", "inline_depth")


class _ReadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_expression(expression: str) -> None:
    """Accept read expressions, not command separators, writes, or target calls."""
    if (not isinstance(expression, str) or not expression.strip()
            or len(expression.encode("utf-8")) > 2048
            or any(ord(c) < 32 for c in expression)
            or any(c in expression for c in ";={}\"'`\\")
            or "++" in expression or "--" in expression
            or not re.fullmatch(r"[A-Za-z_0-9\s:.><*&,()[\]+/%!|^-]+", expression)):
        raise _ReadError("INVALID_ARGUMENT", "Expected a bounded read-only expression without writes or command separators.")
    # RAD's cast/raw/typeof/hex are evaluator operators, not debuggee calls.
    for match in re.finditer(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(", expression):
        if match.group(1) not in ("cast", "raw", "typeof", "hex", "sizeof", "alignof"):
            raise _ReadError("INVALID_ARGUMENT", "Calling target functions is not allowed by this reader.")
    if re.search(r"\)\s*\(", expression):
        raise _ReadError("INVALID_ARGUMENT", "Indirect function calls are not allowed by this reader.")


def _template(type_name: str, expected: str) -> list[str]:
    """Split an exact PDB template spelling without rewriting spaces in types."""
    type_name = re.sub(r"^(?:class|struct)\s+", "", type_name.strip())
    if not type_name.startswith(expected + "<") or not type_name.endswith(">"):
        raise _ReadError("UNSUPPORTED_LAYOUT", f"Expected {expected} PDB template metadata.")
    result, depth, start = [], 0, len(expected) + 1
    for index in range(start, len(type_name) - 1):
        char = type_name[index]
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(type_name[start:index].strip())
            start = index + 1
        if depth < 0:
            raise _ReadError("UNSUPPORTED_LAYOUT", "Unbalanced PDB template metadata.")
    if depth != 0:
        raise _ReadError("UNSUPPORTED_LAYOUT", "Unbalanced PDB template metadata.")
    result.append(type_name[start:-1].strip())
    if any(not part or not _TYPE.fullmatch(part) for part in result):
        raise _ReadError("UNSUPPORTED_LAYOUT", "Unsafe or incomplete PDB type metadata.")
    return result


class ValueReader:
    """One read operation at a time; callbacks must enforce a stopped snapshot."""

    MAX_PROBES = 512
    TIMEOUT_SECONDS = 60.0

    def __init__(self, eval_raw: EvalRaw, *, layout: str = "ue4_26"):
        self.eval_raw = eval_raw
        self.layout = layout

    async def read(self, expression: str, *, kind: str = "auto", start: int = 0,
                   count: int = 16, max_string_units: int = 4096,
                   slot_scan_budget: int = 256, max_depth: int = 2) -> dict[str, Any]:
        self._probes = 0
        self._epoch: dict[str, Any] | None = None
        self._started = time.monotonic()
        result: dict[str, Any] = {"ok": False, "expression": expression, "kind": kind,
                                  "layout": self.layout}
        self._root_expression, self._result = expression, result
        try:
            validate_expression(expression)
            if self.layout != "ue4_26":
                raise _ReadError("UNSUPPORTED_LAYOUT", "Only the explicit ue4_26 Windows x64 profile is available.")
            for name, value, minimum, maximum in (
                    ("start", start, 0, 2**31 - 1), ("count", count, 1, 128),
                    ("max_string_units", max_string_units, 1, 4096),
                    ("slot_scan_budget", slot_scan_budget, 1, 4096),
                    ("max_depth", max_depth, 0, 4)):
                if type(value) is not int or not minimum <= value <= maximum:
                    raise _ReadError("INVALID_ARGUMENT", f"{name} must be an integer in [{minimum}, {maximum}].")
            if kind not in ("auto", "fstring", "ftext", "tarray", "tmap"):
                raise _ReadError("INVALID_ARGUMENT", "Unknown structured value kind.")
            self._string_limit, self._depth = max_string_units, max_depth
            async with asyncio.timeout(self.TIMEOUT_SECONDS):
                if await self._int("sizeof(void *)") != 8 or await self._int("sizeof(uint16)") != 2:
                    raise _ReadError("UNSUPPORTED_LAYOUT", "The selected profile requires 64-bit pointers and 16-bit UTF-16 units.")
                type_name = await self._type(expression)
                result["type"] = type_name
                result["address"] = await self._address(expression)
                actual = self._kind(type_name)
                if kind == "auto":
                    kind = actual
                if kind != actual or kind == "unknown":
                    raise _ReadError("UNSUPPORTED_LAYOUT", "Requested kind does not match verified PDB type metadata.")
                result["kind"] = kind
                if kind == "fstring":
                    result.update(await self._fstring(expression, start))
                elif kind == "ftext":
                    result.update(await self._ftext(expression, start))
                elif kind == "tarray":
                    result.update(await self._array(expression, type_name, start, count))
                elif kind == "tmap":
                    result.update(await self._map(expression, start, count, slot_scan_budget))
                result["ok"] = True
        except _ReadError as error:
            result["error"] = {"code": error.code, "message": str(error)}
            result["complete"] = False
        except TimeoutError:
            result["error"] = {"code": "TIME_BUDGET_EXCEEDED", "message": "Structured read exceeded its time budget."}
        # RadError (especially STALE_SNAPSHOT) and cancellation intentionally
        # propagate immediately. The caller must not continue on an old epoch.
        result["probes"] = self._probes
        result["elapsed_seconds"] = round(time.monotonic() - self._started, 4)
        if self._epoch:
            result["snapshot"] = self._epoch
        return result

    async def _probe(self, expression: str) -> dict[str, Any]:
        if self._probes >= self.MAX_PROBES:
            raise _ReadError("PROBE_BUDGET_EXCEEDED", f"Structured read exhausted its {self.MAX_PROBES}-probe budget.")
        if time.monotonic() - self._started >= self.TIMEOUT_SECONDS:
            raise _ReadError("TIME_BUDGET_EXCEEDED", "Structured read exceeded its time budget.")
        self._probes += 1
        reply = await self.eval_raw(expression)
        if not isinstance(reply, dict):
            raise _ReadError("BAD_REPLY", "Evaluator did not return an object.")
        epoch = {field: reply[field] for field in _EPOCH_FIELDS if field in reply}
        if self._epoch is None:
            self._epoch = epoch
        elif epoch != self._epoch:
            raise RadError("STALE_SNAPSHOT", "The debug context changed between structured-read probes.")
        if reply.get("read_failed"):
            raise _ReadError("READ_FAILED", "Target memory could not be read; no empty value was inferred.")
        if (reply.get("invalid_encoding") or reply.get("type_encoding_valid") is False
                or reply.get("value_encoding_valid") is False):
            raise _ReadError("INVALID_ENCODING", "Evaluator returned invalid value/type encoding.")
        if not reply.get("ok") or reply.get("messages"):
            raise _ReadError("EVALUATION_FAILED", "A required read expression could not be evaluated.")
        if reply.get("possibly_truncated") or reply.get("type_truncated"):
            raise _ReadError("TRUNCATED_PROBE", "Evaluator truncated a required value or type probe.")
        return reply

    async def _int(self, expression: str) -> int:
        value = (await self._probe(expression)).get("value")
        text = str(value).strip()
        if not _INTEGER.fullmatch(text):
            raise _ReadError("BAD_REPLY", "Expected an untruncated numeric evaluator value.")
        return int(text, 16 if "x" in text.lower() else 10)

    async def _maybe_int(self, expression: str) -> int | None:
        try:
            return await self._int(expression)
        except _ReadError as error:
            if error.code != "EVALUATION_FAILED":
                raise
            return None

    async def _type(self, expression: str) -> str:
        reply = await self._probe(f"typeof(raw({expression}))")
        text = str(reply.get("value", "")).strip()
        if text in ("", "type", "<type>"):
            text = str(reply.get("type", "")).strip()
        if not _TYPE.fullmatch(text) or len(text) > 1536:
            raise _ReadError("UNSUPPORTED_LAYOUT", "Missing, unsafe, or truncated PDB type name.")
        return re.sub(r"^(?:class|struct)\s+", "", text)

    async def _address(self, expression: str) -> str:
        # Disable views before taking an address: FString/TArray autohooks can
        # otherwise return the data pointer (including null for an empty value)
        # instead of the address of the actual C++ object.
        address = await self._int(f"hex(cast(uint64)(&(raw({expression}))))")
        self._check_pointer(address)
        return hex(address)

    @staticmethod
    def _check_pointer(address: int, *, nullable: bool = False) -> None:
        if (address == 0 and not nullable) or address < 0 or address >= 2**64:
            raise _ReadError("INVALID_LAYOUT", "Invalid or null target pointer.")

    @staticmethod
    def _kind(type_name: str) -> str:
        clean = re.sub(r"\b(?:const|volatile|class|struct)\b", "", type_name).strip()
        if clean == "FString":
            return "fstring"
        if clean == "FText":
            return "ftext"
        if clean.startswith("TArray<"):
            return "tarray"
        if clean.startswith("TMap<"):
            return "tmap"
        return "unknown"

    async def _allocation(self, expression: str) -> tuple[int, str]:
        direct = await self._maybe_int(f"hex(cast(uint64)({expression}.AllocatorInstance.Data))")
        if direct is not None:
            return direct, "heap"
        secondary = await self._maybe_int(f"hex(cast(uint64)({expression}.AllocatorInstance.SecondaryData.Data))")
        if secondary is None:
            raise _ReadError("UNSUPPORTED_LAYOUT", "Unknown allocator layout; no allocation was guessed.")
        if secondary:
            return secondary, "secondary"
        return int(await self._address(f"{expression}.AllocatorInstance.InlineData"), 16), "inline"

    async def _array_header(self, expression: str) -> tuple[int, int, int, str]:
        num = await self._int(f"{expression}.ArrayNum")
        capacity = await self._int(f"{expression}.ArrayMax")
        if not 0 <= num <= capacity <= 2**31 - 1:
            raise _ReadError("INVALID_LAYOUT", "ArrayNum/ArrayMax are inconsistent.")
        data, allocator = await self._allocation(expression)
        self._check_pointer(data, nullable=num == 0)
        return num, capacity, data, allocator

    async def _units(self, address: int, count: int, *, wide: bool = True) -> bytes:
        """Read exact-sized numeric scalars, then recover individual LE units.

        A batch of at most 16 numeric uint64 values stays below RAD's 512-byte
        display limit even at UINT64_MAX. Final scalars never cross the span.
        """
        remaining = count * (2 if wide else 1)
        output = bytearray()
        while remaining:
            batch = min(16, remaining // 8)
            if batch >= 2:
                reply = await self._probe(f"array(cast(uint64 *)0x{address:x},{batch})")
                try:
                    values = json.loads(reply.get("value", ""))
                except (TypeError, ValueError) as error:
                    raise _ReadError("BAD_REPLY", "Numeric memory batch was not a complete JSON array.") from error
                if (not isinstance(values, list) or len(values) != batch
                        or any(type(value) is not int or not 0 <= value < 2**64 for value in values)):
                    raise _ReadError("BAD_REPLY", "Numeric memory batch has invalid width or element count.")
                output.extend(b"".join(value.to_bytes(8, "little") for value in values))
                address += batch * 8
                remaining -= batch * 8
                continue
            width = next(size for size in (8, 4, 2, 1) if size <= remaining)
            value = await self._int(f"hex(*(cast(uint{width * 8} *)0x{address:x}))")
            if not 0 <= value < 1 << (width * 8):
                raise _ReadError("BAD_REPLY", "Numeric memory probe exceeded its scalar width.")
            output.extend(value.to_bytes(width, "little"))
            remaining -= width
            address += width
        return bytes(output)

    async def _fstring(self, expression: str, start: int = 0, *, report_root: bool = False) -> dict[str, Any]:
        data_expr = f"raw({expression}).Data"
        num, capacity, data, allocator = await self._array_header(data_expr)
        if num == 0:
            if start:
                raise _ReadError("INVALID_ARGUMENT", "String start exceeds its UTF-16 length.")
            return {"value": "", "count": 0, "code_units": 0, "storage_units": 0,
                    "decoded_units": 0, "start": 0, "capacity": capacity, "complete": True,
                    "truncated": False, "next_index": None}
        if await self._int(f"hex(*(cast(uint16 *)0x{data + (num - 1) * 2:x}))") != 0:
            raise _ReadError("INVALID_LAYOUT", "FString storage is missing its required terminating UTF-16 NUL.")
        length = num - 1
        if report_root or expression == self._root_expression:
            self._result.update(count=length, code_units=length, storage_units=num,
                                capacity=capacity, data_address=hex(data))
        if start > length:
            raise _ReadError("INVALID_ARGUMENT", "String start exceeds its UTF-16 length.")
        take = min(length - start, self._string_limit)
        raw = await self._units(data + start * 2, take)
        if start and raw and 0xDC00 <= int.from_bytes(raw[:2], "little") <= 0xDFFF:
            raise _ReadError("INVALID_ARGUMENT", "String start points inside a UTF-16 surrogate pair.")
        # Bounded prefixes must not split a surrogate pair. A complete string
        # with an unpaired surrogate is an encoding error, not replacement text.
        if start + take < length and raw and 0xD800 <= int.from_bytes(raw[-2:], "little") <= 0xDBFF:
            raw, take = raw[:-2], take - 1
            if not take:
                raise _ReadError("STRING_BUDGET_TOO_SMALL", "max_string_units cannot fit the next complete Unicode code point.")
        try:
            value = raw.decode("utf-16-le", errors="strict")
        except UnicodeDecodeError as error:
            raise _ReadError("INVALID_UTF16", "FString contains an invalid UTF-16 sequence.") from error
        return {"value": value, "count": length, "code_units": length,
                "storage_units": num, "decoded_units": take, "start": start, "capacity": capacity,
                "data_address": hex(data), "allocator": allocator,
                "complete": start + take == length, "truncated": start + take < length,
                "next_index": start + take if start + take < length else None,
                "index_unit": "utf16_code_unit"}

    async def _ftext(self, expression: str, start: int = 0) -> dict[str, Any]:
        base = f"raw({expression}).TextData.Object"
        pointer = await self._int(f"hex(cast(uint64)({base}))")
        self._check_pointer(pointer)
        localized = await self._maybe_int(f"hex(cast(uint64)({base}->LocalizedString.Object))")
        if localized:
            path = f"*({base}->LocalizedString.Object)"
            storage = "localized_string"
        else:
            path = f"{base}->DisplayString"
            try:
                display_type = await self._type(path)
            except _ReadError as error:
                if error.code != "EVALUATION_FAILED":
                    raise
                raise _ReadError("UNSUPPORTED_LAYOUT", "FText history has neither a verified localized string nor generated display storage.") from error
            if display_type != "FString":
                raise _ReadError("UNSUPPORTED_LAYOUT", "FText generated display storage is not an FString.")
            storage = "generated_display_string"
        if await self._type(path) != "FString":
            raise _ReadError("UNSUPPORTED_LAYOUT", "FText localized storage is not an FString.")
        result = await self._fstring(path, start, report_root=expression == self._root_expression)
        result.update(text_data_address=hex(pointer), storage=storage)
        return result

    async def _leaf(self, expression: str, depth: int) -> dict[str, Any]:
        type_name = await self._type(expression)
        address = await self._address(expression)
        result: dict[str, Any] = {"type": type_name, "address": address}
        kind = self._kind(type_name)
        if depth > 0 and kind in ("fstring", "ftext"):
            result.update(kind=kind, **await (self._fstring(expression) if kind == "fstring" else self._ftext(expression)))
        elif type_name.strip().endswith("*"):
            pointer = await self._int(f"hex(cast(uint64)({expression}))")
            self._check_pointer(pointer, nullable=True)
            result.update(kind="pointer", value=hex(pointer) if pointer else None,
                          address=hex(pointer) if pointer else None)
        elif type_name.strip() == "FName" and depth > 0:
            result.update(kind="fname", **await self._fname(expression))
        elif kind != "unknown" or depth == 0:
            result.update(kind=kind if kind != "unknown" else "opaque", value=None, expanded=False)
        elif _SCALAR.fullmatch(type_name.strip()) or type_name.startswith("enum "):
            reply = await self._probe(f"raw({expression})")
            text = str(reply.get("value", "")).strip()
            value: Any = text
            if _INTEGER.fullmatch(text):
                value = int(text, 16 if "x" in text.lower() else 10)
            elif text in ("true", "false"):
                value = text == "true"
            elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
                value = float(text)
                if not math.isfinite(value):
                    raise _ReadError("BAD_REPLY", "Non-finite scalar cannot be represented as a JSON number.")
            result.update(kind="scalar", value=value, display=text)
        else:
            result.update(kind="opaque", value=None, expanded=False)
        return result

    async def _array(self, expression: str, type_name: str, start: int, count: int) -> dict[str, Any]:
        element_type = _template(type_name, "TArray")[0]
        base = f"raw({expression})"
        num, capacity, data, allocator = await self._array_header(base)
        items = []
        self._result.update(count=num, capacity=capacity, items=items)
        for index in range(min(start, num), min(start + count, num)):
            item = await self._leaf(f"(cast({element_type} *)0x{data:x})[{index}]", self._depth)
            items.append({"index": index, **item})
        end = min(start + count, num)
        return {"count": num, "capacity": capacity, "data_address": hex(data), "allocator": allocator,
                "start": start, "items": items, "truncated": end < num,
                "next_index": end if end < num else None}

    async def _map(self, expression: str, start: int, count: int, scan_budget: int) -> dict[str, Any]:
        pairs = f"raw({expression}).Pairs"
        pair_type = _template(await self._type(pairs), "TSet")[0]
        if len(_template(pair_type, "TTuple")) != 2:
            raise _ReadError("UNSUPPORTED_LAYOUT", "Map PDB metadata does not contain a key/value tuple.")
        sparse = f"{pairs}.Elements"
        slots, capacity, data, allocator = await self._array_header(f"{sparse}.Data")
        free = await self._int(f"{sparse}.NumFreeIndices")
        first_free = await self._int(f"{sparse}.FirstFreeIndex")
        bits = f"{sparse}.AllocationFlags"
        num_bits = await self._int(f"{bits}.NumBits")
        max_bits = await self._int(f"{bits}.MaxBits")
        if (not 0 <= free <= slots or num_bits != slots or not num_bits <= max_bits <= 2**31 - 1
                or (free and not 0 <= first_free < slots) or (not free and first_free != -1)):
            raise _ReadError("INVALID_LAYOUT", "Sparse slots, allocation bitmap, and free-list metadata disagree.")
        self._result.update(count=slots - free, slot_count=slots, capacity=capacity, free_count=free)
        bitmap, bitmap_allocator = await self._allocation(bits)
        self._check_pointer(bitmap, nullable=slots == 0)
        # Address Data[i].ElementData through its exact PDB element type. The
        # allocation stride is never inferred from sizeof(Key)+sizeof(Value).
        # The heap allocator's Data member may be void*. Index the PDB-typed
        # TArray through RAD's array view, which knows each sparse-slot stride.
        slot_data = f"{sparse}.Data"
        items: list[dict[str, Any]] = []
        self._result["items"] = items
        index, scanned, allocated = min(start, slots), 0, 0
        words: dict[int, int] = {}
        while index < slots and scanned < scan_budget and len(items) < count:
            word_index = index // 32
            if word_index not in words:
                words[word_index] = await self._int(f"hex(*(cast(uint32 *)0x{bitmap + word_index * 4:x}))")
                if not 0 <= words[word_index] < 2**32:
                    raise _ReadError("BAD_REPLY", "Allocation bitmap word exceeds uint32 width.")
            active = bool(words[word_index] & (1 << (index % 32)))
            scanned += 1
            if active:
                allocated += 1
                address = await self._address(f"{slot_data}[{index}].ElementData")
                tuple_expr = f"(*(cast({pair_type} *){address}))"
                key = await self._leaf(f"{tuple_expr}.Key", self._depth)
                value = await self._leaf(f"{tuple_expr}.Value", self._depth)
                items.append({"index": index, "key": key, "value": value})
            index += 1
        if start == 0 and index == slots and allocated != slots - free:
            raise _ReadError("INVALID_LAYOUT", "Bitmap occupancy does not match the sparse-array live count.")
        return {"count": slots - free, "slot_count": slots, "capacity": capacity,
                "free_count": free, "data_address": hex(data), "allocator": allocator,
                "bitmap_address": hex(bitmap), "bitmap_allocator": bitmap_allocator,
                "pair_type": pair_type, "start": start, "items": items,
                "occupancy_validated": start == 0 and index == slots,
                "scanned_slots": scanned, "truncated": index < slots,
                "next_index": index if index < slots else None,
                "scan_budget_exhausted": scanned == scan_budget and index < slots}

    async def _fname(self, expression: str) -> dict[str, Any]:
        """UE4.26-only name-pool decoding, validated against PDB field layout."""
        size = await self._int(f"sizeof(raw({expression}))")
        header_size = await self._int("sizeof(FNameEntryHeader)")
        if size not in (8, 12) or header_size != 2:
            raise _ReadError("UNSUPPORTED_LAYOUT", "FName/header size does not match the ue4_26 profile.")
        field = "DisplayIndex" if size == 12 else "ComparisonIndex"
        identifier = await self._int(f"raw({expression}).{field}.Value")
        number = await self._int(f"raw({expression}).Number")
        if not 0 <= identifier < 1 << 29 or not 0 <= number < 1 << 31:
            raise _ReadError("INVALID_LAYOUT", "FName identifier/number exceeds the selected profile.")
        block, offset = identifier >> 16, identifier & 0xFFFF
        block_pointer = await self._maybe_int(f"hex(cast(uint64)(GNameBlocksDebug[{block}]))")
        if block_pointer is None:
            block_pointer = await self._maybe_int(
                f"hex(cast(uint64)((cast(FNamePool *)(&NamePoolData))->Entries.Blocks[{block}]))")
        if block_pointer is None:
            raise _ReadError("UNSUPPORTED_LAYOUT", "A directly readable UE4 name-pool block table was not found.")
        self._check_pointer(block_pointer)
        stride = 4 if size == 12 else 2
        entry = block_pointer + offset * stride
        entry_expr = f"(cast(FNameEntry *)0x{entry:x})"
        header_address = int(await self._address(f"{entry_expr}->Header"), 16)
        text_address = int(await self._address(f"{entry_expr}->AnsiName"), 16)
        expected_header_offset = 4 if size == 12 else 0
        if header_address != entry + expected_header_offset or text_address != header_address + 2:
            raise _ReadError("UNSUPPORTED_LAYOUT", "FName entry PDB offsets do not match the selected profile.")
        length = await self._int(f"{entry_expr}->Header.Len")
        wide = await self._int(f"{entry_expr}->Header.bIsWide")
        if not 0 < length <= 1023 or wide not in (0, 1):
            raise _ReadError("INVALID_LAYOUT", "Invalid UE4 name-entry header.")
        take = min(length, self._string_limit)
        raw = await self._units(text_address, take, wide=bool(wide))
        if wide and take < length and raw and 0xD800 <= int.from_bytes(raw[-2:], "little") <= 0xDBFF:
            raw, take = raw[:-2], take - 1
        try:
            name = raw.decode("utf-16-le" if wide else "ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise _ReadError("INVALID_ENCODING", "UE4 name-entry bytes do not match the selected encoding profile.") from error
        complete = take == length
        return {"value": name + (f"_{number - 1}" if number and complete else ""),
                "name_id": identifier, "number": number, "entry_address": hex(entry),
                "code_units": length, "decoded_units": take, "complete": complete,
                "truncated": not complete}
