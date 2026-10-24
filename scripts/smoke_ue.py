"""Real UE project acceptance through MCP stdio only; never uses Editor MCP.

Requires an existing UE 5.8 RmlUiUnrealTest build with its matching plugin PDBs.
Does not build, edit, or save project sources/assets. Only the UE PID created by
this test is eligible for final cleanup; existing editor processes are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ue_pe import exported_rva

ROOT = Path(__file__).resolve().parents[1]
TEST_NAME = "RmlUi.Animation.MovieSceneRuntime.Lifecycle"
FUNCTION = "FRmlUiAnimationRuntimeLifecycleTest::RunTest"
TICK_EXPORT = "?Tick@UEditorEngine@@UEAAXM_N@Z"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


class OwnedProcess:
    """Keep the original process handle: cleanup cannot kill a reused PID."""

    def __init__(self, pid: int, expected_executable: Path):
        self.pid = pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CheckRemoteDebuggerPresent.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x400 | 0x1, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        if Path(buffer.value).resolve() != expected_executable.resolve():
            self.close()
            raise AssertionError(f"Unexpected launched process image: {buffer.value}")

    def alive(self) -> bool:
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0x102:
            return True
        if result == 0:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def debugging(self) -> bool:
        flag = wintypes.BOOL()
        if not self.kernel.CheckRemoteDebuggerPresent(self.handle, ctypes.byref(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(flag.value)

    def cpu_ticks(self) -> int:
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in (creation, exit_time, kernel, user))):
            raise ctypes.WinError(ctypes.get_last_error())
        return sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in (kernel, user))

    def terminate(self):
        if self.alive():
            if not self.kernel.TerminateProcess(self.handle, 0):
                error = ctypes.get_last_error()
                # The process can finish between the liveness check and this
                # call. Windows reports ACCESS_DENIED for an exiting process;
                # accept only a signaled original handle, never the error alone.
                if self.kernel.WaitForSingleObject(self.handle, 1000) == 0:
                    return "already_exited_during_cleanup"
                raise ctypes.WinError(error)
            assert self.kernel.WaitForSingleObject(self.handle, 10000) == 0, "Owned UE process did not exit"
            return "terminated_owned_process"
        return "already_exited_before_cleanup"

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def module(state: dict, name: str) -> dict:
    matches = [item for item in state["modules"] if Path(item["name"]).name.casefold() == name.casefold()]
    assert len(matches) == 1, f"Expected one loaded {name}, found {len(matches)}"
    return matches[0]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Existing external RmlUiUnrealTest .uproject")
    parser.add_argument("--engine", type=Path, required=True, help="Matching UE 5.8.1 engine installation")
    parser.add_argument("--startup-timeout", type=float, default=300)
    args = parser.parse_args()
    project, engine = args.project.resolve(), args.engine.resolve()
    executable = engine / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
    plugin = project.parent / "Plugins/RmlUiUnreal"
    test_source = plugin / "Source/RmlUiUnrealEditor/Private/RmlUiAnimationRuntimeTests.cpp"
    binary_dir = plugin / "Binaries/Win64"
    inputs = [project, test_source,
              plugin / "Source/RmlUiUnreal/Private/RmlUiAnimationRuntime.cpp",
              binary_dir / "UnrealEditor-RmlUiUnrealEditor.dll",
              binary_dir / "UnrealEditor-RmlUiUnrealEditor.pdb",
              binary_dir / "UnrealEditor-RmlUiUnreal.dll",
              binary_dir / "UnrealEditor-RmlUiUnreal.pdb"]
    for path in [executable, *inputs]:
        assert path.is_file(), f"Missing required UE build input: {path}"
    descriptor = json.loads(project.read_text(encoding="utf-8-sig"))
    assert not any("mcp" in item["Name"].casefold() and item.get("Enabled", True) for item in descriptor.get("Plugins", [])), "Test project declares an Editor MCP plugin"
    hashes_before = {str(path): sha256(path) for path in inputs}
    lines = test_source.read_text(encoding="utf-8-sig").splitlines()
    function_line = next(i for i, line in enumerate(lines, 1) if f"bool {FUNCTION}(" in line)
    call_line = next(i for i, line in enumerate(lines, 1) if i > function_line and line.strip() == "Runtime.PlayFloat(")
    advance_line = next(i for i, line in enumerate(lines, 1) if i > function_line and line.strip() == "Runtime.Advance(0.0f);")
    completed_line = next(i for i, line in enumerate(lines, 1) if i > advance_line and 'TestEqual(TEXT("initial, repeated midpoint and terminal values emitted")' in line)
    artifact = ROOT / "artifacts" / ("ue-smoke-" + time.strftime("%Y%m%d-%H%M%S"))
    artifact.mkdir(parents=True)
    log_path = artifact / "ue.log"
    record = {
        "passed": False, "scope": "existing UE project native debugging; no rendering qualification",
        "project": str(project), "engine": json.loads((engine / "Engine/Build/Build.version").read_text()),
        "backend": json.loads((ROOT / ".build/backend-manifest.json").read_text(encoding="utf-8-sig")),
        "inputs_sha256": hashes_before, "trace": [],
    }

    def save():
        temporary = artifact / "result.partial.json"
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(artifact / "result.json")

    owned = None
    launch_requested = False
    started = time.monotonic()
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "raddebugger_mcp", "--session-root", str(artifact / "sessions")],
                                      env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as client:
                await client.initialize()

                async def call(tool, **kwargs):
                    response = await client.call_tool(tool, kwargs)
                    data = response.structuredContent
                    record["trace"].append({"elapsed_seconds": round(time.monotonic() - started, 3), "tool": tool,
                                             "arguments": kwargs, "is_error": response.isError,
                                             "data": data, "error": str(response.content)[:3000] if response.isError else None})
                    save()
                    print(tool, "ERROR" if response.isError else (data or {}).get("outcome", "ok"), flush=True)
                    assert not response.isError, str(response.content)
                    assert isinstance(data, dict)
                    return data

                async def wait(sid, baseline, expected_symbol=None):
                    nonlocal owned
                    deadline = time.monotonic() + args.startup_timeout
                    while time.monotonic() < deadline:
                        result = await call("rad_wait_for_stop", session_id=sid,
                                            after_run_gen=baseline[0], after_stop_count=baseline[1], timeout=10)
                        if owned is None and result["state"]["processes"]:
                            assert len(result["state"]["processes"]) == 1, result
                            owned = OwnedProcess(result["state"]["processes"][0]["id"], executable)
                            record["owned_ue_pid"] = owned.pid
                        if result["outcome"] == "timeout":
                            continue
                        assert result["outcome"] == "stopped", result
                        if expected_symbol:
                            assert expected_symbol in result["state"]["ip_voff_symbol"], result
                        return result["state"]
                    raise AssertionError("UE startup/debug stop deadline exceeded; target was not retried")

                async def value(sid, expression, expected=None):
                    result = await call("rad_evaluate", session_id=sid, expression=expression)
                    assert result["ok"] and not result["read_failed"], result
                    assert result["memory_freshness"] == "direct_process_read", result
                    if expected is not None:
                        assert float(result["value"]) == expected, result
                    return result["value"]

                async def full_state(sid):
                    state = await call("rad_get_state", session_id=sid, include_modules=True, include_stack=True)
                    forbidden = [entry["name"] for entry in state["modules"] if "mcp" in Path(entry["name"]).name.casefold()]
                    assert not forbidden, f"Unexpected MCP module in UE: {forbidden}"
                    return state

                created = await call("rad_create_session", name="UE project launch acceptance")
                sid = created["session_id"]
                bp = await call("rad_set_breakpoint", session_id=sid, kind="function", function=FUNCTION)
                launch_requested = True
                launched = await call("rad_launch", session_id=sid, program=str(executable), args=[
                    str(project), "/Engine/Maps/Entry", "-unattended", "-nosplash", "-nullrhi", "-nosound",
                    "-NoLiveCoding", "-nop4", "-NoRestoreOpenAssetTabs", f"-abslog={log_path}",
                    f"-ExecCmds=Automation RunTests {TEST_NAME}", f"-ReportOutputPath={artifact / 'automation'}",
                ], stop_at_entry=False)
                state = await wait(sid, (launched["wait_after_run_gen"], launched["wait_after_stop_count"]), "FRmlUiAnimationRuntimeLifecycleTest")
                assert len(state["processes"]) == 1, state
                assert owned is not None and owned.pid == state["processes"][0]["id"]
                assert owned.alive() and owned.debugging()
                assert any(entry["hit_count"] > 0 for entry in state["breakpoints"] if entry["id"] == bp["breakpoint"]["id"]), state
                await call("rad_remove_breakpoint", session_id=sid, breakpoint_id=bp["breakpoint"]["id"])
                stack = await call("rad_get_stack", session_id=sid)
                assert any("FRmlUiAnimationRuntimeLifecycleTest" in frame["symbol"] for frame in stack["frames"]), stack
                line_bp = await call("rad_set_breakpoint", session_id=sid, kind="line", file=str(test_source), line=call_line)
                line_run = await call("rad_continue", session_id=sid)
                configured = await wait(sid, (line_run["wait_after_run_gen"], line_run["wait_after_stop_count"]), "FRmlUiAnimationRuntimeLifecycleTest")
                assert any(item["line_num"] == call_line for item in configured["lines"]), configured
                assert any(item["hit_count"] > 0 for item in configured["breakpoints"] if item["id"] == line_bp["breakpoint"]["id"]), configured
                await call("rad_remove_breakpoint", session_id=sid, breakpoint_id=line_bp["breakpoint"]["id"])
                for expression, expected in [("Desc.From", 0), ("Desc.To", 10), ("Desc.DelaySeconds", .25),
                                              ("Desc.DurationSeconds", .5), ("Desc.Iterations", 2), ("Desc.BindingId", 7), ("CompletedCount", 0)]:
                    await value(sid, expression, expected)
                positioned = await call("rad_run_to_line", session_id=sid, file=str(test_source), line=advance_line, timeout=30)
                assert positioned["outcome"] == "stopped", positioned
                before_step = positioned["state"]
                stepped = await call("rad_step", session_id=sid, kind="over", expected_stop_count=before_step["stop_count"], timeout=30)
                assert stepped["outcome"] == "stopped" and stepped["state"]["ip"] != before_step["ip"], stepped
                assert any(item["line_num"] == advance_line for item in before_step["lines"]), before_step
                assert any(item["line_num"] == advance_line + 1 for item in stepped["state"]["lines"]), stepped
                await value(sid, "CompletedCount", 0)
                completed = await call("rad_run_to_line", session_id=sid, file=str(test_source), line=completed_line, timeout=30)
                assert completed["outcome"] == "stopped", completed
                await value(sid, "CompletedCount", 1)
                state = await full_state(sid)
                core = module(state, "UnrealEditor-Core.dll")
                core_path = engine / "Engine/Binaries/Win64/UnrealEditor-Core.dll"
                counter_rva = exported_rva(core_path, "?GFrameCounter@@3_KA")
                counter_address = core["vaddr_range"]["min"] + counter_rva
                counter_expression = f"*(cast(uint64*)0x{counter_address:x})"
                before_counter = int(await value(sid, counter_expression), 0)
                record["core_export"] = {"file_sha256": sha256(core_path), "name": "?GFrameCounter@@3_KA", "rva": counter_rva}
                await call("rad_continue", session_id=sid)
                deadline = time.monotonic() + 60
                while True:
                    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
                    success_lines = [line for line in log.splitlines() if "Test Completed." in line and "Success" in line and "Lifecycle" in line]
                    if success_lines:
                        record["automation_success"] = success_lines
                        break
                    assert owned.alive(), "UE exited before automation success"
                    assert time.monotonic() < deadline, "UE automation did not report success"
                    await asyncio.sleep(.25)
                await asyncio.sleep(.5)
                pause = await call("rad_pause", session_id=sid)
                assert pause["outcome"] == "stopped", pause
                paused_counter = int(await value(sid, counter_expression), 0)
                assert paused_counter > before_counter, (before_counter, paused_counter)
                await call("rad_close_session", session_id=sid)
                assert owned.alive() and not owned.debugging(), "Launch session did not detach UE"
                cpu_before = owned.cpu_ticks()
                await asyncio.sleep(1)
                assert owned.alive() and owned.cpu_ticks() > cpu_before, "Detached UE did not resume CPU work"

                # Attach to the same, already initialized and genuinely running UE PID.
                attached_session = await call("rad_create_session", name="UE running PID attach acceptance")
                sid2 = attached_session["session_id"]
                attached = await call("rad_attach", session_id=sid2, pid=owned.pid, timeout=60)
                assert attached["outcome"] == "stopped" and owned.debugging(), attached
                state2 = await full_state(sid2)
                core2 = module(state2, "UnrealEditor-Core.dll")
                counter_address2 = core2["vaddr_range"]["min"] + counter_rva
                counter_expression2 = f"*(cast(uint64*)0x{counter_address2:x})"
                attached_counter = int(await value(sid2, counter_expression2), 0)
                assert attached_counter > paused_counter, (paused_counter, attached_counter)
                unreal_ed = module(state2, "UnrealEditor-UnrealEd.dll")
                unreal_ed_path = engine / "Engine/Binaries/Win64/UnrealEditor-UnrealEd.dll"
                tick_rva = exported_rva(unreal_ed_path, TICK_EXPORT)
                tick_address = unreal_ed["vaddr_range"]["min"] + tick_rva
                tick_bp = await call("rad_set_breakpoint", session_id=sid2, kind="address", address=hex(tick_address))
                running = await call("rad_continue", session_id=sid2)
                tick_hit = await wait(sid2, (running["wait_after_run_gen"], running["wait_after_stop_count"]))
                assert tick_hit["ip"] == tick_address, tick_hit
                await call("rad_remove_breakpoint", session_id=sid2, breakpoint_id=tick_bp["breakpoint"]["id"])
                instruction = await call("rad_step", session_id=sid2, kind="over_instruction", expected_stop_count=tick_hit["stop_count"], timeout=30)
                assert instruction["outcome"] == "stopped" and instruction["state"]["ip"] != tick_address, instruction
                await value(sid2, counter_expression2)
                await call("rad_close_session", session_id=sid2)
                assert owned.alive() and not owned.debugging(), "Attach session did not detach UE"
                cpu_before = owned.cpu_ticks()
                await asyncio.sleep(1)
                assert owned.alive() and owned.cpu_ticks() > cpu_before, "Second detach did not resume UE"
                record["tick_export"] = {"file_sha256": sha256(unreal_ed_path), "name": TICK_EXPORT, "rva": tick_rva,
                                          "stepping": "instruction; engine PDB unavailable"}
                record["frame_counters"] = [before_counter, paused_counter, attached_counter]
                record["detach_checks"] = {"launch": "alive, no debugger, CPU progresses", "attach": "alive, no debugger, CPU progresses"}
                record["backend_pids"] = [created["backend_pid"], attached_session["backend_pid"]]
        record["sources_and_symbols_unchanged"] = hashes_before == {str(path): sha256(path) for path in inputs}
        assert record["sources_and_symbols_unchanged"], "UE project source or symbol inputs changed during acceptance"
        record["workflow_passed"] = True
    except BaseException as error:
        record["failure"] = f"{type(error).__name__}: {error}"
        if launch_requested and owned is None:
            record["cleanup_limitation"] = (
                "Launch was requested but no verified target handle was captured. The MCP backend may have detached "
                "the UE process during shutdown. Inspect this session's logs/processes; this driver will not kill an unverified PID."
            )
            print(record["cleanup_limitation"], file=sys.stderr, flush=True)
        raise
    finally:
        try:
            if owned:
                record["alive_before_owned_cleanup"] = owned.alive()
                record["owned_process_cleanup"] = owned.terminate()
            record["passed"] = bool(record.get("workflow_passed"))
        except BaseException as error:
            record["passed"] = False
            record["cleanup_failure"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            if owned:
                owned.close()
            save()
            print("Evidence:", artifact / "result.json", flush=True)
    print("PASS: existing UE project launch, source/function breakpoints, stepping, locals, running-PID attach and detach", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
