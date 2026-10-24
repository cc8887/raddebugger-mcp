"""Real MCP regression for direct detach with a pending native debug event.

Only launches the repository's existing debuggee.exe fixture. Finite cases must
exit normally after a direct detach; loop controls must remain alive and advance.
Use --backend for an old-build negative control and --cases to select one case.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND = ROOT / ".build/raddebugger/build/raddbg_non_graphical.exe"
CASES = ("source_step", "instruction_step", "function_breakpoint", "line_breakpoint",
         "pause_detach", "running_detach")
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102


def exit_details(code: int) -> dict:
    unsigned = code & 0xFFFFFFFF
    return {"unsigned": unsigned, "hex": f"0x{unsigned:08x}",
            "signed": unsigned if unsigned < 0x80000000 else unsigned - 0x100000000}


def require_normal_exit(code: int) -> dict:
    details = exit_details(code)
    if details["unsigned"] != 0:
        raise AssertionError(f"Detached fixture did not exit normally: {details}")
    return details


def wait_for_detached_counter_progress(process, address: int, timeout: float = 2.0) -> dict:
    """Sample both ends after close returns; earlier running progress cannot pass."""
    def sample():
        if not process.alive():
            code = process.wait_for_exit(0)
            raise AssertionError(f"Detached loop fixture exited: {exit_details(code)}")
        if process.debugging():
            raise AssertionError("Fixture still has a debugger after closing its session.")
        return struct.unpack("<i", process.read(address, 4))[0]

    baseline = sample()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(.025)
        current = sample()
        if current > baseline:
            return {"counter_at_detach": baseline, "counter_after_detach": current}
    raise AssertionError("Detached fixture remained alive without post-detach counter progress.")


class FixtureProcess:
    """Retain and validate the original handle; never terminate by PID lookup."""

    def __init__(self, pid: int, expected_image: Path):
        self.pid = pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "CheckRemoteDebuggerPresent": ([wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "ReadProcessMemory": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.OpenProcess(0x100000 | 0x400 | 0x10 | 0x1, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, image, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(image.value).resolve() != expected_image.resolve():
                raise AssertionError("Observed process image does not match this test's launch target.")
        except BaseException:
            self.close()
            raise

    def alive(self) -> bool:
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == WAIT_TIMEOUT:
            return True
        if result == WAIT_OBJECT_0:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def debugging(self) -> bool:
        flag = wintypes.BOOL()
        if not self.kernel.CheckRemoteDebuggerPresent(self.handle, ctypes.byref(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(flag.value)

    def read(self, address: int, count: int) -> bytes:
        data = ctypes.create_string_buffer(count)
        actual = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, ctypes.c_void_p(address), data, count,
                                             ctypes.byref(actual)) or actual.value != count:
            raise ctypes.WinError(ctypes.get_last_error())
        return data.raw

    def wait_for_exit(self, timeout: float) -> int:
        waited = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
        if waited == WAIT_TIMEOUT:
            raise TimeoutError(f"Owned fixture did not exit within {timeout:g} seconds.")
        if waited != WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def terminate_if_alive(self) -> str:
        if not self.alive():
            return "already_exited"
        if not self.kernel.TerminateProcess(self.handle, 0):
            error = ctypes.get_last_error()
            if self.kernel.WaitForSingleObject(self.handle, 1000) == WAIT_OBJECT_0:
                return "exited_during_cleanup"
            raise ctypes.WinError(error)
        self.wait_for_exit(5)
        return "terminated_owned_process"

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def suppress_crash_dialogs():
    """Applies only to this test and its children, and restores the old mode."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetErrorMode.argtypes = [wintypes.UINT]
    kernel.SetErrorMode.restype = wintypes.UINT
    previous = kernel.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    try:
        yield
    finally:
        kernel.SetErrorMode(previous)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


async def run(args) -> Path:
    fixture = (ROOT / ".build/debuggee.exe").resolve()
    source = (ROOT / "tests/fixtures/debuggee.c").resolve()
    backend = args.backend.resolve()
    for path in (fixture, fixture.with_suffix(".pdb"), source, backend):
        if not path.is_file():
            raise FileNotFoundError(f"Required existing regression input is missing: {path}")
    target_line = next(i for i, line in enumerate(source.read_text().splitlines(), 1)
                       if "MCP_LINE_BREAKPOINT" in line)
    artifact = ROOT / "artifacts" / ("detach-regression-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    artifact.mkdir(parents=True)
    record = {"passed": False, "backend": str(backend), "backend_sha256": digest(backend),
              "fixture": str(fixture), "fixture_sha256": digest(fixture),
              "created_at": datetime.now(timezone.utc).isoformat(), "cases": [], "cleanup": []}
    targets, backends = [], []

    def save():
        temporary = artifact / "result.partial.json"
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(artifact / "result.json")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-u", "-X", "utf8", "-m", "raddebugger_mcp", "--backend", str(backend),
              "--session-root", str(artifact / "sessions")],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=45)) as client:
                await client.initialize()
                for repetition in range(1, args.repeat + 1):
                    for kind in args.cases:
                        case = {"name": kind, "repetition": repetition, "passed": False, "trace": []}
                        record["cases"].append(case)
                        session_id, target, closed = None, None, False
                        started = time.monotonic()
                        print(f"CASE {kind} repeat={repetition}", flush=True)

                        async def call(tool, **arguments):
                            async with asyncio.timeout(45):
                                response = await client.call_tool(tool, arguments)
                            data = response.structuredContent
                            case["trace"].append({"tool": tool, "arguments": arguments,
                                                  "is_error": response.isError, "data": data,
                                                  "error": str(response.content) if response.isError else None})
                            save()
                            if response.isError or not isinstance(data, dict):
                                raise AssertionError(f"{tool} failed: {response.content}")
                            return data

                        async def capture(state):
                            nonlocal target
                            processes = state["processes"]
                            if len(processes) != 1:
                                raise AssertionError("Expected exactly this case's one launched fixture process.")
                            if target is None:
                                target = FixtureProcess(processes[0]["id"], fixture)
                                targets.append(target)
                                case["fixture_pid"] = target.pid
                            if processes[0]["id"] != target.pid:
                                raise AssertionError("The backend's target PID changed during this case.")
                            return state

                        try:
                            created = await call("rad_create_session", name=f"detach-{kind}-{repetition}")
                            session_id = created["session_id"]
                            backends.append(FixtureProcess(created["backend_pid"], backend))
                            case.update(session_id=session_id, backend_pid=created["backend_pid"])
                            bp = None
                            if kind == "function_breakpoint":
                                bp = await call("rad_set_breakpoint", session_id=session_id,
                                                kind="function", function="breakpoint_here")
                            elif kind == "line_breakpoint":
                                bp = await call("rad_set_breakpoint", session_id=session_id,
                                                kind="line", file=str(source), line=target_line)
                            loop = kind in ("pause_detach", "running_detach")
                            case["launch_requested"] = True
                            launched = await call("rad_launch", session_id=session_id, program=str(fixture),
                                                  args=["--loop"] if loop else [], stop_at_entry=True)
                            if launched["outcome"] != "stopped":
                                raise AssertionError(f"Launch did not stop at entry: {launched}")
                            state = await capture(launched["state"])
                            if not target.alive() or not target.debugging():
                                raise AssertionError("Owned fixture is not alive under this debugger.")
                            counter_address = None
                            if loop:
                                evaluated = await call("rad_evaluate", session_id=session_id,
                                                       expression="hex(cast(uint64)(&g_counter))")
                                if not evaluated["ok"] or evaluated["read_failed"]:
                                    raise AssertionError(f"Cannot locate the fixture counter: {evaluated}")
                                counter_address = int(evaluated["value"], 0)
                                await call("rad_continue", session_id=session_id)
                                deadline = time.monotonic() + 5
                                while True:
                                    state = await call("rad_get_state", session_id=session_id)
                                    if state["running"]:
                                        break
                                    if time.monotonic() >= deadline:
                                        raise AssertionError("Loop fixture did not begin running.")
                                    await asyncio.sleep(.02)
                                if kind == "pause_detach":
                                    paused = await call("rad_pause", session_id=session_id, timeout=10)
                                    if paused["outcome"] != "stopped":
                                        raise AssertionError(f"Loop fixture did not pause: {paused}")
                                    state = await capture(paused["state"])
                            elif bp:
                                requested = await call("rad_continue", session_id=session_id)
                                hit = await call("rad_wait_for_stop", session_id=session_id,
                                                 after_run_gen=requested["wait_after_run_gen"],
                                                 after_stop_count=requested["wait_after_stop_count"], timeout=10)
                                if hit["outcome"] != "stopped":
                                    raise AssertionError(f"Fixture did not hit the configured breakpoint: {hit}")
                                state = await capture(hit["state"])
                                if not any(item["id"] == bp["breakpoint"]["id"] and item["hit_count"] > 0
                                           for item in state["breakpoints"]):
                                    raise AssertionError("Configured breakpoint did not record an actual hit.")
                                # These variants intentionally retain their user breakpoint at detach.
                            else:
                                positioned = await call("rad_run_to_line", session_id=session_id,
                                                        file=str(source), line=target_line, timeout=10)
                                if positioned["outcome"] != "stopped":
                                    raise AssertionError(f"Fixture did not reach source line: {positioned}")
                                before = await capture(positioned["state"])
                                if not any(line["line_num"] == target_line for line in before["lines"]):
                                    raise AssertionError("Run-to-line stopped at another source location.")
                                stepped = await call("rad_step", session_id=session_id,
                                                     kind="over" if kind == "source_step" else "over_instruction",
                                                     expected_stop_count=before["stop_count"], timeout=10)
                                if stepped["outcome"] != "stopped":
                                    raise AssertionError(f"Fixture did not finish the requested step: {stepped}")
                                state = await capture(stepped["state"])
                                if state["stop_count"] <= before["stop_count"] or state["ip"] == before["ip"]:
                                    raise AssertionError("The step did not produce a new instruction/stop.")
                                if kind == "source_step" and not any(line["line_num"] == target_line + 1
                                                                      for line in state["lines"]):
                                    raise AssertionError("Source step did not reach the fixture return line.")
                                if state["breakpoints"]:
                                    raise AssertionError("Step-detach cases require zero configured user breakpoints.")
                            case["before_detach"] = state
                            if not loop:
                                case["byte_at_stop_ip"] = target.read(state["ip"], 1).hex()
                            if loop:
                                case["counter_before_detach"] = struct.unpack("<i", target.read(counter_address, 4))[0]
                            # Critical regression boundary: no continue/pause between the step and this close.
                            result = await call("rad_close_session", session_id=session_id, terminate_debuggee=False)
                            if result["outcome"] != "closed" or result["debuggee_action"] != "detach":
                                raise AssertionError(f"Fixture was not detached: {result}")
                            closed = True
                            if loop:
                                case.update(await asyncio.to_thread(
                                    wait_for_detached_counter_progress, target, counter_address, 2))
                            else:
                                code = await asyncio.to_thread(target.wait_for_exit, args.exit_timeout)
                                case["exit"] = exit_details(code)
                                require_normal_exit(code)
                                output = (artifact / "sessions" / session_id / "stdout.txt").read_text(encoding="utf-8")
                                case["stdout"] = output
                                if "answer=24 counter=12" not in output:
                                    raise AssertionError("Normal exit did not include the fixture's expected execution result.")
                            case["passed"] = True
                        except BaseException as error:
                            case["failure"] = f"{type(error).__name__}: {error}"
                            raise
                        finally:
                            if case.get("launch_requested") and target is None:
                                limitation = ("Launch was requested but no original fixture handle was acquired. "
                                              "A started fixture may remain running after safe session detach; "
                                              "no PID was guessed or terminated. Inspect this failed run manually.")
                                case["cleanup_limitation"] = limitation
                                case["passed"] = False
                                record.setdefault("cleanup_limitations", []).append({
                                    "case": kind, "repetition": repetition, "message": limitation,
                                })
                            if session_id and not closed:
                                try:
                                    async with asyncio.timeout(15):
                                        response = await client.call_tool("rad_close_session", {
                                            "session_id": session_id, "terminate_debuggee": False})
                                    case["session_cleanup"] = {"is_error": response.isError,
                                                                "data": response.structuredContent}
                                except BaseException as error:
                                    case["session_cleanup_failure"] = f"{type(error).__name__}: {error}"
                            if target:
                                try:
                                    case["fixture_cleanup"] = await asyncio.to_thread(target.terminate_if_alive)
                                except BaseException as error:
                                    case["passed"] = False
                                    case["fixture_cleanup_failure"] = f"{type(error).__name__}: {error}"
                                    raise
                            case["elapsed_seconds"] = round(time.monotonic() - started, 3)
                            save()
                            print(f"{'PASS' if case['passed'] else 'FAIL'} {kind} repeat={repetition}", flush=True)
                remaining = await client.call_tool("rad_list_sessions", {})
                if remaining.isError or remaining.structuredContent.get("sessions"):
                    raise AssertionError("Regression left an open session in its private MCP server.")
        record["workflow_passed"] = True
    except BaseException as error:
        record["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for process in [*targets, *backends]:
            try:
                cleanup = process.terminate_if_alive()
                record["cleanup"].append({"pid": process.pid, "outcome": cleanup})
            except BaseException as error:
                record.setdefault("cleanup_failures", []).append(f"PID {process.pid}: {type(error).__name__}: {error}")
            finally:
                try:
                    process.close()
                except BaseException as error:
                    record.setdefault("cleanup_failures", []).append(f"Close handle: {type(error).__name__}: {error}")
        cleanup_failed = bool(record.get("cleanup_failures") or record.get("cleanup_limitations"))
        record["passed"] = bool(record.get("workflow_passed")) and not cleanup_failed
        save()
        print("Evidence:", artifact / "result.json", flush=True)
        if record.get("workflow_passed") and cleanup_failed:
            raise AssertionError("Owned process cleanup failed; see result.json.")
    return artifact / "result.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--exit-timeout", type=float, default=10)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This regression requires Windows x64.")
    if not 1 <= args.repeat <= 10 or not 0 < args.exit_timeout <= 30:
        parser.error("Repeat must be 1..10 and exit timeout must be within 0..30 seconds.")
    with suppress_crash_dialogs():
        asyncio.run(run(args))
    print("PASS: pending-event direct detach regression and owned fixture lifecycle", flush=True)


if __name__ == "__main__":
    main()
