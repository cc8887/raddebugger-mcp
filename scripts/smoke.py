"""Real MCP stdio -> authenticated RAD TCP -> MSVC/PDB process integration test."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import msvcrt
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from raddebugger_mcp.processes import list_processes
from raddebugger_mcp.protocol import HEADER, MAGIC, parse_reply
from raddebugger_mcp.sessions import SessionManager


def write_dump(pid: int, path: Path) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
    dbghelp.MiniDumpWriteDump.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    dbghelp.MiniDumpWriteDump.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x410, False, pid)  # query information + read this test fixture's memory
    assert handle, ctypes.get_last_error()
    try:
        with path.open("wb") as output:
            ok = dbghelp.MiniDumpWriteDump(handle, pid, msvcrt.get_osfhandle(output.fileno()), 2, None, None, None)
            assert ok, f"MiniDumpWriteDump failed: {ctypes.get_last_error()}"
    finally:
        kernel.CloseHandle(handle)


async def main():
    artifact = ROOT / "artifacts" / ("smoke-" + time.strftime("%Y%m%d-%H%M%S"))
    artifact.mkdir(parents=True)
    fixture = ROOT / ".build/debuggee.exe"
    source = ROOT / "tests/fixtures/debuggee.c"
    target_line = next(i for i, line in enumerate(source.read_text().splitlines(), 1) if "MCP_LINE_BREAKPOINT" in line)
    second_call = next(i for i, line in enumerate(source.read_text().splitlines(), 1) if "MCP_SECOND_CALL" in line)
    trace = [{"backend_manifest": json.loads((ROOT / ".build/backend-manifest.json").read_text(encoding="utf-8-sig"))}]
    external = []
    backend_pids = []
    server = StdioServerParameters(command=sys.executable, args=["-m", "raddebugger_mcp", "--session-root", str(artifact / "sessions")],
                                   env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) >= 20
                trace.append({"initialize": initialized.model_dump(mode="json"), "tools": [t.name for t in tools.tools]})

                async def call(tool, *, expect_error=False, **args):
                    result = await client.call_tool(tool, args)
                    data = result.structuredContent
                    trace.append({"tool": tool, "arguments": args, "result": result.model_dump(mode="json")})
                    status = "ERROR" if result.isError else "value_error" if (data or {}).get("ok") is False else (data or {}).get("outcome", "ok")
                    print(tool, status, flush=True)
                    if expect_error:
                        assert result.isError, f"Expected error from {tool}"
                        return result
                    assert not result.isError, result
                    assert isinstance(data, dict), result
                    return data

                created = await call("rad_create_session", name="PDB smoke")
                sid = created["session_id"]
                backend_pids.append(created["backend_pid"])
                # An unauthenticated peer must not receive state or control the backend.
                port = int(created["backend_endpoint"].rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", port), 1) as unauthorized:
                    unauthorized.settimeout(0.25)
                    payload = b"0" * 64 + b"\nexit"
                    unauthorized.sendall(struct.pack("<IQ", 0x72726300, len(payload)) + payload)
                    try:
                        data = unauthorized.recv(128)
                    except TimeoutError:
                        data = b""
                    assert not data, "Unauthenticated peer received a reply"
                listeners = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], creationflags=subprocess.CREATE_NO_WINDOW).decode(errors="replace")
                owned_listeners = [line for line in listeners.splitlines() if line.split() and line.split()[-1] == str(created["backend_pid"]) and f":{port}" in line]
                assert owned_listeners and all("127.0.0.1:" in line for line in owned_listeners), owned_listeners
                bp = await call("rad_set_breakpoint", session_id=sid, kind="function", function="breakpoint_here")
                launched = await call("rad_launch", session_id=sid, program=str(fixture))
                assert launched["outcome"] == "stopped", launched
                await call("rad_continue", session_id=sid)
                hit = await call("rad_wait_for_stop", session_id=sid, timeout=15)
                assert hit["outcome"] == "stopped" and "breakpoint_here" in hit["state"]["ip_voff_symbol"], hit
                stack = await call("rad_get_stack", session_id=sid)
                assert any("breakpoint_here" in f["symbol"] for f in stack["frames"]), stack
                assert any(f["symbol"] == "main" for f in stack["frames"]), stack
                function_address = await call("rad_evaluate", session_id=sid, expression="hex((uint64)breakpoint_here)")
                await call("rad_run_to_line", session_id=sid, file=str(source), line=target_line)
                local = await call("rad_evaluate", session_id=sid, expression="local_value")
                assert local["ok"] and local["value"] == "12", local
                assert local["memory_freshness"] == "direct_process_read" and not local["read_failed"], local
                unreadable = await call("rad_evaluate", session_id=sid, expression="*(cast(int32*)0x1)")
                assert not unreadable["ok"] and unreadable["read_failed"] and unreadable["messages"], unreadable
                divide_zero = await call("rad_evaluate", session_id=sid, expression="1/0")
                assert not divide_zero["ok"] and divide_zero["messages"], divide_zero
                ambiguous_cast = await call("rad_evaluate", session_id=sid, expression="(uint64)&g_counter")
                assert not ambiguous_cast["ok"] and ambiguous_cast["messages"], ambiguous_cast
                old_state = await call("rad_get_state", session_id=sid)
                stepped = await call("rad_step", session_id=sid, kind="over", expected_stop_count=old_state["stop_count"])
                assert stepped["outcome"] == "stopped" and stepped["state"]["stop_count"] > old_state["stop_count"], stepped
                value = await call("rad_evaluate", session_id=sid, expression="g_counter")
                assert value["ok"] and value["value"] == "12", value
                stale = await call("rad_evaluate", session_id=sid, expression="g_counter", expected_stop_count=old_state["stop_count"], expect_error=True)
                assert "STALE_SNAPSHOT" in str(stale)
                mapped = await call("rad_address_to_line", session_id=sid, address=hex(stepped["state"]["ip"]))
                assert mapped["lines"], mapped
                await call("rad_select_context", session_id=sid, frame_index=1)
                argc = await call("rad_evaluate", session_id=sid, expression="argc")
                assert argc["ok"] and argc["value"] == "1", argc
                await call("rad_select_context", session_id=sid, frame_index=0)
                line_bp = await call("rad_set_breakpoint", session_id=sid, kind="line", file=str(source), line=second_call)
                bps = await call("rad_list_breakpoints", session_id=sid)
                assert len(bps["breakpoints"]) == 2
                await call("rad_remove_breakpoint", session_id=sid, breakpoint_id=bp["breakpoint"]["id"])
                await call("rad_continue", session_id=sid)
                line_hit = await call("rad_wait_for_stop", session_id=sid)
                assert line_hit["outcome"] == "stopped" and any(l["line_num"] == second_call for l in line_hit["state"]["lines"]), line_hit
                await call("rad_remove_breakpoint", session_id=sid, breakpoint_id=line_bp["breakpoint"]["id"])
                addr_bp = await call("rad_set_breakpoint", session_id=sid, kind="address", address=function_address["value"])
                await call("rad_continue", session_id=sid)
                addr_hit = await call("rad_wait_for_stop", session_id=sid)
                assert addr_hit["outcome"] == "stopped" and addr_hit["state"]["ip"] == int(function_address["value"], 0), addr_hit
                await call("rad_remove_breakpoint", session_id=sid, breakpoint_id=addr_bp["breakpoint"]["id"])
                await call("rad_evaluate", session_id=sid, expression="g_counter; exit", expect_error=True)
                await call("rad_continue", session_id=sid)
                exited = await call("rad_wait_for_stop", session_id=sid)
                assert exited["outcome"] == "exited", exited
                fixture_output = (artifact / "sessions" / sid / "stdout.txt").read_text(encoding="utf-8")
                assert "answer=24 counter=12" in fixture_output, fixture_output
                await call("rad_close_session", session_id=sid)

                cold = await call("rad_create_session", name="cold function breakpoint without entry stop")
                backend_pids.append(cold["backend_pid"])
                await call("rad_set_breakpoint", session_id=cold["session_id"], kind="function", function="breakpoint_here")
                await call("rad_launch", session_id=cold["session_id"], program=str(fixture), stop_at_entry=False)
                cold_hit = await call("rad_wait_for_stop", session_id=cold["session_id"])
                assert cold_hit["outcome"] == "stopped" and "breakpoint_here" in cold_hit["state"]["ip_voff_symbol"], cold_hit
                cold_pid = cold_hit["state"]["processes"][0]["id"]
                await call("rad_close_session", session_id=cold["session_id"], terminate_debuggee=True)
                assert not any(p["pid"] == cold_pid for p in list_processes()), "Explicit fixture termination failed"

                created2 = await call("rad_create_session", name="pause and detach")
                sid2 = created2["session_id"]
                backend_pids.append(created2["backend_pid"])
                await call("rad_launch", session_id=sid2, program=str(fixture), args=["--loop"], stop_at_entry=False)
                pending = await call("rad_wait_for_stop", session_id=sid2, timeout=0.3)
                assert pending["outcome"] == "timeout" and pending["state"]["running"], pending
                # A requested cold launch may still be creating its first
                # Process entity. Do not confuse this with an existing app
                # that can already be paused, especially under UE/PDB load.
                launch_deadline = time.monotonic() + 15
                pending_state = pending["state"]
                while not pending_state["processes"] and time.monotonic() < launch_deadline:
                    await asyncio.sleep(0.1)
                    pending_state = await call("rad_get_state", session_id=sid2)
                assert pending_state["running"] and pending_state["processes"], pending_state
                await call("rad_evaluate", session_id=sid2, expression="g_counter", expect_error=True)
                paused = await call("rad_pause", session_id=sid2)
                assert paused["outcome"] == "stopped", paused
                launched_pid = paused["state"]["processes"][0]["id"]
                # The handle is only for terminating our own loop fixture after the detach assertion.
                external.append(launched_pid)
                await call("rad_close_session", session_id=sid2)
                assert any(p["pid"] == launched_pid for p in list_processes()), "Closing RAD killed the launched app"
                port2 = created2["backend_endpoint"].rsplit(":", 1)[1]
                after_close = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], creationflags=subprocess.CREATE_NO_WINDOW).decode(errors="replace")
                assert not any(f"127.0.0.1:{port2} " in row and "LISTENING" in row for row in after_close.splitlines()), "Detached fixture inherited the RAD listener"

                proc = subprocess.Popen([str(fixture), "--loop"], creationflags=subprocess.CREATE_NO_WINDOW)
                external.append(proc)
                created3 = await call("rad_create_session", name="attach and shutdown")
                sid3 = created3["session_id"]
                backend_pids.append(created3["backend_pid"])
                attached = await call("rad_attach", session_id=sid3, pid=proc.pid)
                assert attached["outcome"] == "stopped", attached
                live_counter = await call("rad_evaluate", session_id=sid3, expression="g_counter")
                live_counter_address = await call("rad_evaluate", session_id=sid3, expression="hex(cast(uint64)(&g_counter))")
                assert live_counter_address["ok"] and live_counter["ok"], (live_counter_address, live_counter)
                assert int(live_counter_address["value"], 0) > 0 and int(live_counter["value"]) >= 7, (live_counter_address, live_counter)
                dump_path = artifact / "fixture.dmp"
                write_dump(proc.pid, dump_path)
                dump_session = await call("rad_create_session", name="offline dump")
                backend_pids.append(dump_session["backend_pid"])
                loaded = await call("rad_open_crash_dump", session_id=dump_session["session_id"], path=str(dump_path))
                assert loaded["outcome"] == "dump_loaded", loaded
                # Dump symbol discovery is not qualified by this fixture. Read a
                # known address captured from the paused live process instead.
                dump_value = await call("rad_evaluate", session_id=dump_session["session_id"], expression=f"*(cast(int32*){live_counter_address['value']})")
                assert dump_value["ok"] and dump_value["value"] == live_counter["value"], dump_value
                dump_unreadable = await call("rad_evaluate", session_id=dump_session["session_id"], expression="*(cast(int32*)0x1)")
                assert not dump_unreadable["ok"] and dump_unreadable["read_failed"], dump_unreadable
                await call("rad_continue", session_id=dump_session["session_id"], expect_error=True)
                await call("rad_close_session", session_id=dump_session["session_id"])
                # Deliberately leave this session open: EOF must run the server's detach cleanup.
        await asyncio.sleep(0.25)
        assert proc.poll() is None, "MCP shutdown killed the attached app"
        alive = {p["pid"] for p in list_processes()}
        assert not any(pid in alive for pid in backend_pids), "Backend leaked after shutdown"

        # Exercise native TCP reassembly with the token kept only in this test's memory.
        manager = SessionManager(session_root=artifact / "fragmented-sessions")
        try:
            fourth = await manager.create("fragmented TCP")
            session = manager.get(fourth["session_id"])
            reader, writer = await asyncio.open_connection("127.0.0.1", session.transport.port)
            payload = (session.transport.token + "\nstate").encode()
            packet = HEADER.pack(MAGIC, len(payload)) + payload
            for begin, end in ((0, 3), (3, 12), (12, 37), (37, len(packet))):
                writer.write(packet[begin:end])
                await writer.drain()
                await asyncio.sleep(0.03)
            header = await asyncio.wait_for(reader.readexactly(HEADER.size), 5)
            magic, size = HEADER.unpack(header)
            assert magic == MAGIC
            body = await asyncio.wait_for(reader.readexactly(size), 5)
            assert parse_reply(body.decode())["state"]["mcp_authenticated"] == 1
            writer.close()
            await writer.wait_closed()
            trace.append({"native_fragmented_tcp": "passed", "listener_inheritance": "not_inherited", "unauthenticated_command": "rejected"})
        finally:
            await manager.shutdown()
        print("PASS: real MCP, PDB breakpoints, stepping, values, attach, detach and shutdown", flush=True)
        (artifact / "result.json").write_text(json.dumps({"passed": True, "trace": trace}, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:
        (artifact / "result.json").write_text(json.dumps({"passed": False, "trace": trace}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        for process in external:
            if isinstance(process, subprocess.Popen):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            else:
                # Only fixture PIDs captured directly from this test's own launch are eligible.
                subprocess.run(["taskkill", "/PID", str(process), "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        print("Evidence:", artifact / "result.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
