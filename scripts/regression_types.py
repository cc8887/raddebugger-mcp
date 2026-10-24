"""Real MCP stdio regression against an independent UE4.26 layout fixture.

This is NOT Unreal runtime acceptance: ue_types.exe has no Unreal dependency.
Build inputs separately with build-types-fixture.ps1. --backend may select an
older executable as a negative control; wrong direct Unicode output must fail.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from regression_detach import FixtureProcess, digest, require_normal_exit, suppress_crash_dialogs

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND = ROOT / ".build/raddebugger/build/raddbg_non_graphical.exe"
DIRECT_STRINGS = {"g_chinese": "限帧", "g_mixed": "FPS 帧率 / test", "g_emoji": "播放 🎮🚀"}
NUMBERS = [7, -2, 0, 12345, 7]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_direct_string(data: dict, expected: str) -> None:
    require(data.get("ok") is True and not data.get("read_failed"), f"Direct read failed: {data}")
    require(not data.get("value_truncated", data.get("possibly_truncated", False)),
            f"Short direct string was truncated: {data}")
    require(data.get("value_encoding_valid", True) is True, f"Invalid direct encoding: {data}")
    value = json.loads(data["value"])
    require(isinstance(value, str), f"Expected quoted string, got {value!r}")
    value.encode("utf-8", errors="strict")
    require(value == expected, f"Direct Unicode mismatch: expected {expected!r}, got {value!r}")


def assert_truncated_direct_string(data: dict, expected: str) -> None:
    require(not data.get("read_failed") and not data.get("invalid_encoding")
            and data.get("value_encoding_valid") is True,
            f"Truncation cannot conceal a failed read or invalid encoding: {data}")
    display = data.get("value")
    require(isinstance(display, str), f"Missing direct string display: {data}")
    require(len(display.encode("utf-8", errors="strict")) <= 512,
            f"Direct string exceeds its 512-byte UTF8 display cap: {data}")
    require(data.get("value_truncated", data.get("possibly_truncated", False)) is True
            and data.get("display_limit_bytes") == 512,
            f"Long direct value concealed its display limit: {data}")
    # RAD may stop before a string's closing display quote. Do not JSON-decode
    # that intentionally incomplete display, or count its opener as source text.
    content = display
    if content.startswith('"'):
        content = content[1:]
        if content.endswith('"'):
            content = content[:-1]
    require(content.endswith("..."), f"Truncated display lacks its explicit ellipsis: {data}")
    prefix = content[:-3]
    require(0 < len(prefix) < len(expected) and expected.startswith(prefix),
            f"Truncated display is not a nonempty proper prefix of the source string: {data}")
    messages = data.get("messages")
    require(isinstance(messages, str) and "truncat" in messages.lower(),
            f"Partial direct value has no explicit truncation message: {data}")


def assert_direct_ftext_protocol(data: dict) -> None:
    """Check the bounded formatted view, not FText display-string semantics."""
    require(not data.get("read_failed") and not data.get("invalid_encoding")
            and data.get("value_encoding_valid") is True
            and data.get("type_encoding_valid", True) is True,
            f"FText display concealed a failed read or invalid encoding: {data}")
    display = data.get("value")
    require(isinstance(display, str), f"Missing formatted FText display: {data}")
    require(len(display.encode("utf-8", errors="strict")) <= 512,
            f"Formatted FText exceeds its 512-byte UTF8 display cap: {data}")
    require(data.get("display_limit_bytes") == 512,
            f"Formatted FText omitted its display limit: {data}")
    messages = data.get("messages", "")
    require(isinstance(messages, str), f"Invalid formatted FText messages: {data}")
    messages.encode("utf-8", errors="strict")
    truncated = data.get("value_truncated", data.get("possibly_truncated", False))
    if truncated:
        require(truncated is True and data.get("ok") is False and "truncat" in messages.lower(),
                f"Partial FText display needs a truncation flag/message and ok=false: {data}")
    else:
        require(data.get("ok") is True and "truncat" not in messages.lower(),
                f"Complete formatted FText cannot conceal an error or truncation: {data}")


def assert_direct_ftext_semantic(data: dict, expected: str, storage: str) -> None:
    assert_direct_ftext_protocol(data)
    assert_direct_string(data, expected)
    require(data.get("representation") == "ue4_26_ftext_display_string",
            f"Direct FText omitted its semantic representation: {data}")
    semantic = data.get("semantic_read")
    require(isinstance(semantic, dict) and "value" not in semantic,
            f"Direct FText must not duplicate an uncapped semantic value: {data}")
    require(semantic.get("ok") is True and not semantic.get("error")
            and semantic.get("storage") == storage and semantic.get("complete") is True
            and semantic.get("truncated") is False,
            f"Direct FText lacks complete verified semantic storage: {data}")
    diagnostics = data.get("native_display_diagnostics")
    # Private binary history/controller expansion may fail to format as text;
    # preserve that diagnostic without confusing it with the semantic read.
    require(isinstance(diagnostics, dict)
            and all(key in diagnostics for key in ("value", "ok", "read_failed", "invalid_encoding")),
            f"Direct FText discarded its native display diagnostics: {data}")


def assert_complete_string(data: dict, expected: str) -> None:
    require(data.get("ok") is True, f"Structured string read failed: {data}")
    require(data.get("value") == expected, f"Structured string differs: {data}")
    require(data.get("complete") is True and data.get("truncated") is False,
            f"Partial string cannot be accepted as complete: {data}")
    units = len(expected.encode("utf-16-le")) // 2
    require(data.get("code_units") == units and data.get("decoded_units") == units,
            f"UTF16 unit count differs: {data}")
    require(data.get("next_index") is None, f"Complete string retained a continuation: {data}")


def assert_error(data: dict, code: str) -> None:
    require(data.get("ok") is False, f"Expected explicit {code}, got success: {data}")
    require(data.get("error", {}).get("code") == code, f"Expected {code}: {data}")


def map_rows(data: dict) -> dict:
    require(data.get("ok") is True, f"Map read failed: {data}")
    rows = {}
    for item in data.get("items", []):
        key, value = item["key"], item["value"]
        require(key.get("kind") in ("fname", "fstring") and key.get("complete") is True,
                f"Map key was not completely decoded: {item}")
        require(value.get("kind") in ("pointer", "scalar", "fstring"), f"Untyped map value: {item}")
        if value["kind"] == "fstring":
            require(value.get("complete") is True and value.get("truncated") is False,
                    f"Partial map value cannot count as complete: {item}")
        if value["kind"] == "pointer":
            require(value.get("address") == value.get("value"), f"Pointer identity disagrees: {item}")
        require(key["value"] not in rows, f"Repeated decoded map key: {item}")
        rows[key["value"]] = value["value"]
    return rows


def merge_map_pages(pages: list[dict], expected: dict) -> None:
    rows, indices = {}, set()
    for page in pages:
        for item in page["items"]:
            require(item["index"] not in indices, f"Repeated physical slot across pages: {item}")
            indices.add(item["index"])
        for key, value in map_rows(page).items():
            require(key not in rows, f"Repeated key across pages: {key}")
            rows[key] = value
    require(rows == expected, f"Map key/value mismatch: expected {expected!r}, got {rows!r}")
    require(all(page["count"] == len(expected) for page in pages), "Map count changed across pages")
    require(pages[-1].get("next_index") is None and not pages[-1].get("truncated"),
            "Map traversal did not reach its final physical slot")


def close_child_handle(child) -> None:
    """The subprocess owns this exact Windows handle, including startup failures."""
    handle = getattr(child, "_handle", None)
    if handle is not None:
        handle.Close()


async def run(args) -> Path:
    fixture = (ROOT / ".build/ue_types.exe").resolve()
    source = ROOT / "tests/fixtures/ue_types.cpp"
    backend = args.backend.resolve()
    for path in (fixture, fixture.with_suffix(".pdb"), source, backend):
        if not path.is_file():
            raise FileNotFoundError(f"Build the existing regression input first: {path}")
    artifact = ROOT / "artifacts" / ("types-layout-regression-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    artifact.mkdir(parents=True)
    record = {"passed": False, "scope": "Independent UE4.26 Win64 layout fixture; NOT Unreal runtime acceptance",
              "backend": str(backend), "backend_sha256": digest(backend),
              "fixture": str(fixture), "fixture_sha256": digest(fixture),
              "created_at": datetime.now(timezone.utc).isoformat(), "trace": [], "checks": [], "cleanup": []}
    owners, child, session_id = [], None, None

    def save():
        partial = artifact / "result.partial.json"
        partial.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        partial.replace(artifact / "result.json")

    server = StdioServerParameters(
        command=sys.executable,
        args=["-u", "-X", "utf8", "-m", "raddebugger_mcp", "--backend", str(backend),
              "--session-root", str(artifact / "sessions")],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as client:
                await client.initialize()

                async def call(tool, **arguments):
                    async with asyncio.timeout(90):
                        reply = await client.call_tool(tool, arguments)
                    data = reply.structuredContent
                    record["trace"].append({"tool": tool, "arguments": arguments,
                                            "is_error": reply.isError, "data": data,
                                            "error": str(reply.content) if reply.isError else None})
                    save()
                    require(not reply.isError and isinstance(data, dict), f"{tool} failed: {reply.content}")
                    return data

                async def evaluate(expression):
                    return await call("rad_evaluate", session_id=session_id, expression=expression)

                async def read_value(expression, kind="auto", **options):
                    return await call("rad_read_value", session_id=session_id, expression=expression,
                                      kind=kind, layout="ue4_26", **options)

                async def check(name, operation):
                    entry = {"name": name, "passed": False}
                    record["checks"].append(entry)
                    try:
                        await operation()
                        entry["passed"] = True
                    except BaseException as error:
                        entry["failure"] = f"{type(error).__name__}: {error}"
                        raise
                    finally:
                        save()
                        print(f"{'PASS' if entry['passed'] else 'FAIL'} {name}", flush=True)

                created = await call("rad_create_session", name="Independent UE4 layout types")
                session_id = created["session_id"]
                owners.append(FixtureProcess(created["backend_pid"], backend))
                # Launch outside RAD so the original subprocess handle is retained
                # before any attach/IPC operation can fail. Cleanup never guesses a PID.
                gate_path = artifact / "fixture-ready.gate"
                require(not gate_path.exists(), "The private fixture gate must not exist before attach")
                record["fixture_gate"] = str(gate_path)
                with (artifact / "fixture-stdout.txt").open("wb") as stdout, \
                        (artifact / "fixture-stderr.txt").open("wb") as stderr:
                    child = subprocess.Popen([str(fixture), "--wait-for-debugger", str(gate_path)], cwd=ROOT,
                                             stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                             close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                             env={key: value for key, value in os.environ.items()
                                                  if key.upper() != "RAD_MCP_TOKEN"})
                owners.append(FixtureProcess(child.pid, fixture))
                record["fixture_pid"] = child.pid
                attached = await call("rad_attach", session_id=session_id, pid=child.pid, timeout=15)
                require(attached["outcome"] == "stopped", f"Attach did not stop: {attached}")
                ready_address = await evaluate("hex(cast(uint64)(&raw(types_ready)))")
                require(ready_address.get("ok") is True and not ready_address.get("read_failed"),
                        f"Cannot resolve the ready function before releasing its gate: {ready_address}")
                resolved_address = int(ready_address["value"], 0)
                require(resolved_address != 0, "Ready function resolved to a null address")
                record["ready_function_address"] = hex(resolved_address)
                bp = await call("rad_set_breakpoint", session_id=session_id,
                                kind="address", address=hex(resolved_address))
                gate_path.write_text("types_ready address breakpoint installed\n", encoding="utf-8")
                record["fixture_gate_released_at"] = datetime.now(timezone.utc).isoformat()
                requested = await call("rad_continue", session_id=session_id)
                ready = await call("rad_wait_for_stop", session_id=session_id,
                                   after_run_gen=requested["wait_after_run_gen"],
                                   after_stop_count=requested["wait_after_stop_count"], timeout=15)
                # Windows may deliver its initial attach breakpoint after the
                # earlier process-created stop (IP=0). This finite fixture has
                # no application exception before types_ready. Continue that
                # one explicitly classified ntdll attach trap, never arbitrary
                # stops or application faults, and retain its evidence.
                if (ready["outcome"] == "stopped"
                        and not any(item["id"] == bp["breakpoint"]["id"] and item["hit_count"] > 0
                                    for item in ready["state"]["breakpoints"])):
                    initial = ready["state"]
                    require(initial.get("ip_module", "").casefold() == "ntdll.dll"
                            and initial.get("stop_event", {}).get("exception_code") == 0x80000003,
                            f"Unexpected stop before fixture ready: {ready}")
                    record["initial_attach_trap"] = initial
                    requested = await call("rad_continue", session_id=session_id)
                    ready = await call("rad_wait_for_stop", session_id=session_id,
                                       after_run_gen=requested["wait_after_run_gen"],
                                       after_stop_count=requested["wait_after_stop_count"], timeout=15)
                require(ready["outcome"] == "stopped", f"Fixture did not reach initialized data: {ready}")
                require(any(item["id"] == bp["breakpoint"]["id"] and item["hit_count"] > 0
                            for item in ready["state"]["breakpoints"]), "Ready breakpoint did not record a real hit")
                await call("rad_remove_breakpoint", session_id=session_id, breakpoint_id=bp["breakpoint"]["id"])
                record["stop_count"] = ready["state"]["stop_count"]

                for expression, expected in DIRECT_STRINGS.items():
                    async def operation(expression=expression, expected=expected):
                        assert_direct_string(await evaluate(expression), expected)
                    await check("direct_full_" + expression, operation)

                async def long_direct():
                    data = await evaluate("g_long")
                    assert_truncated_direct_string(data, "中" * 600)
                await check("direct_long_explicit_truncation", long_direct)

                async def direct_no_nul():
                    assert_direct_string(await evaluate("g_no_nul"), "无结尾")
                await check("direct_nonterminated_bounded_storage", direct_no_nul)

                async def direct_invalid():
                    data = await evaluate("g_invalid_utf16")
                    require(data.get("ok") is False and data.get("value_encoding_valid") is False,
                            f"Invalid UTF16 was silently accepted: {data}")
                await check("direct_invalid_utf16_rejected", direct_invalid)

                strings = {**DIRECT_STRINGS, "g_empty": "", "g_embedded_nul": "A\0中", "g_long": "中" * 600}
                for expression, expected in strings.items():
                    async def operation(expression=expression, expected=expected):
                        assert_complete_string(await read_value(expression, "fstring", max_string_units=4096), expected)
                    await check("structured_full_" + expression, operation)

                for expression, code in (("g_no_nul", "INVALID_LAYOUT"), ("g_invalid_utf16", "INVALID_UTF16"),
                                         ("g_unknown_text", "UNSUPPORTED_LAYOUT")):
                    async def operation(expression=expression, code=code):
                        assert_error(await read_value(expression), code)
                    await check("explicit_error_" + expression, operation)

                async def bounded_strings():
                    long_value = await read_value("g_long", max_string_units=8)
                    require(long_value.get("ok") is True and long_value["value"] == "中" * 8
                            and long_value["complete"] is False and long_value["truncated"] is True
                            and long_value["next_index"] == 8, f"Incorrect bounded string result: {long_value}")
                    continuation = await read_value("g_long", start=8, max_string_units=8)
                    require(continuation.get("ok") is True and continuation["start"] == 8
                            and continuation["value"] == "中" * 8 and continuation["next_index"] == 16
                            and continuation["complete"] is False, f"String continuation ignored start: {continuation}")
                    final_page = await read_value("g_long", start=592, max_string_units=8)
                    require(final_page.get("ok") is True and final_page["value"] == "中" * 8
                            and final_page["code_units"] == 600 and final_page["decoded_units"] == 8
                            and final_page["complete"] is True and final_page["truncated"] is False
                            and final_page["next_index"] is None, f"String final page differs: {final_page}")
                    emoji = await read_value("g_emoji", max_string_units=4)
                    require(emoji.get("ok") is True and emoji["value"] == "播放 "
                            and emoji["complete"] is False and emoji["truncated"] is True
                            and emoji["decoded_units"] == 3, f"A bound split a surrogate pair: {emoji}")
                    assert_error(await read_value("g_emoji", start=4), "INVALID_ARGUMENT")
                    assert_error(await read_value("g_emoji", start=3, max_string_units=1), "STRING_BUDGET_TOO_SMALL")
                await check("bounded_strings_do_not_claim_completeness", bounded_strings)

                for expression, expected, storage in (("g_localized_text", "限帧", "localized_string"),
                                                       ("g_generated_text", "播放 🎮🚀", "generated_display_string")):
                    async def direct_semantic(expression=expression, expected=expected, storage=storage):
                        assert_direct_ftext_semantic(await evaluate(expression), expected, storage)
                    await check("direct_ftext_semantic_" + expression, direct_semantic)

                    async def operation(expression=expression, expected=expected, storage=storage):
                        value = await read_value(expression, "ftext")
                        assert_complete_string(value, expected)
                        require(value["storage"] == storage, f"Wrong verified text storage: {value}")
                    await check("ftext_" + expression, operation)

                async def arrays():
                    full = await read_value("g_numbers", "tarray")
                    require(full.get("ok") is True and full["count"] == len(NUMBERS)
                            and [item["value"] for item in full["items"]] == NUMBERS
                            and [item["index"] for item in full["items"]] == list(range(len(NUMBERS)))
                            and not full["truncated"], f"Array content/order differs: {full}")
                    page = await read_value("g_numbers", start=1, count=2)
                    require([item["value"] for item in page["items"]] == NUMBERS[1:3]
                            and page["next_index"] == 3 and page["truncated"], f"Bad array page: {page}")
                    empty = await read_value("g_empty_array")
                    require(empty.get("ok") is True and empty["count"] == 0 and empty["items"] == []
                            and not empty["truncated"], f"Empty array became an error/null: {empty}")
                await check("array_values_order_pagination_and_empty", arrays)

                addresses = {}
                for index in range(4):
                    data = await evaluate(f"hex(cast(uint64)(&g_objects[{index}]))")
                    require(data.get("ok") is True, f"Cannot locate owned fixture object: {data}")
                    addresses[index] = hex(int(data["value"], 0))
                require(all(int(address, 0) != 0 for address in addresses.values())
                        and len(set(addresses.values())) == 4,
                        f"Fixture object pointers must be nonzero and distinct: {addresses}")
                comparison = await evaluate("g_case_preserving_name.ComparisonIndex.Value")
                display = await evaluate("g_case_preserving_name.DisplayIndex.Value")
                require(comparison.get("ok") is True and display.get("ok") is True
                        and int(comparison["value"], 0) != int(display["value"], 0),
                        "The FName fixture must distinguish comparison and display indices")
                maps = {
                    "g_name_map_inline": ({"Alpha": addresses[0], "Gamma": None, "Delta": addresses[3]}, [0, 2, 3], "inline"),
                    "g_name_map_secondary": ({"Delta": addresses[3], "中名": addresses[1],
                                               "Player_1": addresses[2], "CrossBlock": addresses[0]}, [0, 31, 128, 131], "secondary"),
                    "g_string_map": ({"alpha": 11, "中文": -7, "rocket 🚀": 42}, [0, 2, 3], "inline"),
                    "g_string_string_map": ({"first": "中英 mixed", "second": "🎮"}, [0, 2], "inline"),
                }
                for expression, (expected, indices, bitmap) in maps.items():
                    async def operation(expression=expression, expected=expected, indices=indices, bitmap=bitmap):
                        full = await read_value(expression, "tmap", slot_scan_budget=256)
                        require(map_rows(full) == expected and full["count"] == len(expected)
                                and [item["index"] for item in full["items"]] == indices
                                and full["bitmap_allocator"] == bitmap and not full["truncated"],
                                f"Sparse map traversal differs: {full}")
                        pages, start = [], 0
                        while True:
                            page = await read_value(expression, start=start, count=1, slot_scan_budget=32)
                            pages.append(page)
                            require(len(pages) <= 10, "Map pagination did not terminate")
                            if page.get("next_index") is None:
                                break
                            require(page["next_index"] > start, f"Map page made no progress: {page}")
                            start = page["next_index"]
                        merge_map_pages(pages, expected)
                    await check("typed_map_" + expression, operation)

                async def map_stride_and_empty():
                    sizes = []
                    for expression in ("g_name_map_inline", "g_string_string_map"):
                        data = await evaluate(f"sizeof(raw({expression}).Pairs.Elements.Data[0])")
                        require(data.get("ok") is True, f"Cannot query PDB sparse-slot size: {data}")
                        sizes.append(int(data["value"], 0))
                    require(sizes[0] != sizes[1], "Fixture failed to exercise different PDB slot strides")
                    record["pdb_slot_sizes"] = sizes
                    empty = await read_value("g_empty_map")
                    require(empty.get("ok") is True and empty["count"] == 0 and empty["items"] == []
                            and not empty["truncated"], f"Empty map became an error/null: {empty}")
                    limited = await read_value("g_name_map_secondary", count=16, slot_scan_budget=2)
                    require(limited.get("ok") is True and limited["scanned_slots"] == 2
                            and limited["scan_budget_exhausted"] is True and limited["next_index"] == 2
                            and limited["truncated"] is True, f"Map scan budget was not explicit: {limited}")
                await check("pdb_stride_empty_map_and_scan_budget", map_stride_and_empty)

                closed = await call("rad_close_session", session_id=session_id, terminate_debuggee=False)
                require(closed.get("outcome") == "closed" and closed.get("debuggee_action") == "detach",
                        f"Fixture was not detached: {closed}")
                session_id = None
                code = await asyncio.to_thread(child.wait, 10)
                record["fixture_exit"] = require_normal_exit(code)
                require("UE4_LAYOUT_FIXTURE_OK" in (artifact / "fixture-stdout.txt").read_text(encoding="utf-8"),
                        "Fixture did not execute its finite completion path")
                remaining = await call("rad_list_sessions")
                require(not remaining.get("sessions"), "Private MCP server retained a session")
                record["workflow_passed"] = True
    except BaseException as error:
        record["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # The fixture's original subprocess handle also covers attach/startup failure.
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                record["cleanup"].append({"pid": child.pid, "outcome": "owned_subprocess_exited"})
            except BaseException as error:
                record.setdefault("cleanup_failures", []).append(f"Original child handle: {error}")
            finally:
                try:
                    close_child_handle(child)
                except BaseException as error:
                    record.setdefault("cleanup_failures", []).append(f"Close original child handle: {error}")
        for owner in owners:
            try:
                record["cleanup"].append({"pid": owner.pid, "outcome": owner.terminate_if_alive()})
            except BaseException as error:
                record.setdefault("cleanup_failures", []).append(f"Owned handle {owner.pid}: {error}")
            finally:
                try:
                    owner.close()
                except BaseException as error:
                    record.setdefault("cleanup_failures", []).append(f"Close owned handle: {error}")
        record["passed"] = bool(record.get("workflow_passed")) and not record.get("cleanup_failures")
        save()
        print("Evidence:", artifact / "result.json", flush=True)
        if record.get("workflow_passed") and not record["passed"]:
            raise AssertionError("Owned process cleanup failed; see the saved evidence")
    return artifact / "result.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND,
                        help="Existing backend executable; an old backend is a negative control expected to fail")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This independent layout regression requires Windows x64")
    with suppress_crash_dialogs():
        asyncio.run(run(args))
    print("PASS: independent UE4.26 layout fixture through real MCP stdio (not Unreal runtime acceptance)", flush=True)


if __name__ == "__main__":
    main()
