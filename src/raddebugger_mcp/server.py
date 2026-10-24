from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .errors import RadError
from .processes import list_processes
from .protocol import parse_reply
from .sessions import DEFAULT_BACKEND, SessionManager, compact, require_stopped


def build_server(manager: SessionManager) -> FastMCP:
    @asynccontextmanager
    async def lifespan(server: FastMCP):
        try:
            yield {"manager": manager}
        finally:
            with anyio.CancelScope(shield=True):
                await manager.shutdown()

    mcp = FastMCP(
        "raddebugger-mcp", lifespan=lifespan, log_level="WARNING",
        instructions=(
            "Native Windows x64 debugger backed by RAD Debugger. No Unreal Editor or editor MCP is required. "
            "Create a session, set breakpoints, then launch or attach by an explicitly chosen PID. "
            "Execution commands return wait_after_run_gen and wait_after_stop_count; pass those to rad_wait_for_stop. "
            "Inspect stack and locals only while stopped. Pass expected_run_gen/expected_stop_count when acting on a snapshot. "
            "Expression displays are limited to 512 UTF-8 bytes; FText uses verified UE4.26 display storage. Use rad_read_value for bounded "
            "structured UE4.26 TMap/FText/TArray/FString reads and explicit paging. The patched backend reads process "
            "memory directly and reports read failures; registers/unwind and symbol metadata remain backend-derived. "
            "same_stop_epoch is not a whole-process atomic snapshot. A configured breakpoint is not a proven hit. "
            "Close sessions to detach and resume applications; terminate_debuggee=true explicitly terminates them. "
            "Blueprint, Actor, PIE, rendering, and asset semantics are outside this native debugger's scope."
        ),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    control = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=control)
    async def rad_create_session(name: str = "native-debug", startup_timeout: float = 30.0) -> dict[str, Any]:
        """Create an isolated, authenticated headless RAD backend. Does not start an application."""
        return await manager.create(name, startup_timeout)

    @mcp.tool(annotations=read)
    async def rad_list_sessions() -> dict[str, Any]:
        """List this MCP service's active debug sessions."""
        return await manager.list()

    @mcp.tool(annotations=read)
    async def rad_list_processes(name_contains: str = "") -> dict[str, Any]:
        """List local Windows PIDs, executable names and parent PIDs. Select the exact application PID before attaching."""
        return {"processes": list_processes(name_contains)}

    @mcp.tool(annotations=control)
    async def rad_launch(session_id: str, program: str, args: list[str] | None = None,
                         working_directory: str | None = None, stop_at_entry: bool = True,
                         timeout: float = 30.0, debug_subprocesses: bool = False) -> dict[str, Any]:
        """Launch a local x64 executable with PDBs. Arguments are a list; no shell is used. Stops at entry by default."""
        return await manager.launch(session_id, program, args, working_directory, stop_at_entry, timeout, debug_subprocesses)

    @mcp.tool(annotations=control)
    async def rad_attach(session_id: str, pid: int, timeout: float = 15.0) -> dict[str, Any]:
        """Attach to an existing local application PID and wait for a debug stop. May pause the application."""
        return await manager.attach(session_id, pid, timeout)

    @mcp.tool(annotations=read)
    async def rad_get_state(session_id: str, include_modules: bool = False, include_stack: bool = False) -> dict[str, Any]:
        """Return current run_gen, stop_count, stop reason, processes, threads and local names. Optional full module/stack lists."""
        return compact(await manager.get(session_id).state(), modules=include_modules, stack=include_stack)

    @mcp.tool(annotations=read)
    async def rad_wait_for_stop(session_id: str, after_run_gen: int | None = None,
                                after_stop_count: int | None = None, timeout: float = 10.0) -> dict[str, Any]:
        """Wait at most 60 seconds for a NEW stop/exit. Timeout does not cancel execution. Defaults to the last execution baseline."""
        session = manager.get(session_id)
        if (after_run_gen is None) != (after_stop_count is None):
            raise RadError("INVALID_ARGUMENT", "Supply both after_run_gen and after_stop_count, or omit both.")
        if after_run_gen is None:
            if session.last_execution is None:
                raise RadError("NO_BASELINE", "Pass a baseline from rad_get_state or issue an execution command first.")
            after_run_gen, after_stop_count = session.last_execution
        return await session.wait_for_stop(after_run_gen, after_stop_count, timeout)

    @mcp.tool(annotations=control)
    async def rad_set_breakpoint(session_id: str, kind: Literal["line", "function", "address"],
                                 file: str | None = None, line: int | None = None,
                                 function: str | None = None, address: str | None = None) -> dict[str, Any]:
        """Configure one breakpoint: file+line, function name, or address string. Configuration does not prove symbol resolution."""
        return await manager.set_breakpoint(session_id, kind, file, line, function, address)

    @mcp.tool(annotations=read)
    async def rad_list_breakpoints(session_id: str) -> dict[str, Any]:
        """Read actual RAD breakpoint configuration and observed hit counts."""
        state = await manager.get(session_id).state()
        return {"session_id": session_id, "breakpoints": state["breakpoints"], "run_gen": state["run_gen"], "stop_count": state["stop_count"]}

    @mcp.tool(annotations=control)
    async def rad_remove_breakpoint(session_id: str, breakpoint_id: str) -> dict[str, Any]:
        """Remove a breakpoint ID returned by this session. Does not accept arbitrary config IDs."""
        return await manager.remove_breakpoint(session_id, breakpoint_id)

    @mcp.tool(annotations=control)
    async def rad_continue(session_id: str, expected_run_gen: int | None = None,
                           expected_stop_count: int | None = None) -> dict[str, Any]:
        """Resume attached processes and return a baseline for rad_wait_for_stop."""
        return await manager.execute(session_id, "continue", expected_run_gen=expected_run_gen, expected_stop_count=expected_stop_count)

    @mcp.tool(annotations=control)
    async def rad_pause(session_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Pause running processes and wait for the resulting stop."""
        return await manager.execute(session_id, "pause", timeout=timeout)

    @mcp.tool(annotations=control)
    async def rad_step(session_id: str, kind: Literal["into", "over", "out", "into_instruction", "over_instruction"] = "over",
                       expected_run_gen: int | None = None, expected_stop_count: int | None = None,
                       timeout: float = 10.0) -> dict[str, Any]:
        """Step the selected stopped thread at source/instruction level. Returns a new stop, exit or timeout."""
        return await manager.execute(session_id, "step_" + kind, expected_run_gen=expected_run_gen, expected_stop_count=expected_stop_count, timeout=timeout)

    @mcp.tool(annotations=control)
    async def rad_run_to_line(session_id: str, file: str, line: int, timeout: float = 10.0) -> dict[str, Any]:
        """Run from the current stop to a source line, another stop, or exit."""
        return await manager.run_to_line(session_id, file, line, timeout)

    @mcp.tool(annotations=read)
    async def rad_evaluate(session_id: str, expression: str, expected_run_gen: int | None = None,
                           expected_stop_count: int | None = None) -> dict[str, Any]:
        """Evaluate while stopped, with type/value and epoch evidence. FText uses verified UE4.26 display storage; explicit raw/typeof keep native behavior."""
        return await manager.evaluate(session_id, expression, expected_run_gen, expected_stop_count)

    @mcp.tool(annotations=read)
    async def rad_read_value(session_id: str, expression: str,
                             kind: Literal["auto", "fstring", "ftext", "tarray", "tmap"] = "auto",
                             start: int = 0, count: int = 16, max_string_units: int = 4096,
                             slot_scan_budget: int = 256, max_depth: int = 2,
                             layout: Literal["ue4_26"] = "ue4_26",
                             expected_run_gen: int | None = None,
                             expected_stop_count: int | None = None) -> dict[str, Any]:
        """Read bounded native UE values as JSON, without executing target functions.

        The initial verified layout is UE4.26 Win64 with matching PDBs. TMap
        paging uses sparse slot indices, including allocation flags; start and
        next_index are not ordinal item numbers. Unknown layouts/text histories,
        invalid Unicode, read failures and exhausted budgets are explicit errors.
        Pointer values are identities, not full reflected Actor property trees.
        """
        return await manager.read_value(session_id, expression, kind, start, count,
                                        max_string_units, slot_scan_budget, max_depth, layout,
                                        expected_run_gen, expected_stop_count)

    @mcp.tool(annotations=read)
    async def rad_get_stack(session_id: str, start: int = 0, count: int = 32) -> dict[str, Any]:
        """Read the selected thread's native call stack, symbol names and source locations. At most 128 backend frames."""
        if start < 0 or not 1 <= count <= 128:
            raise RadError("INVALID_ARGUMENT", "Start must be nonnegative; count must be within 1..128.")
        state = await manager.get(session_id).state()
        require_stopped(state)
        return {"session_id": session_id, "thread_id": state["selected_thread_id"], "frames": state["stack"][start:start + count],
                "total_count": state["stack_total_count"], "backend_truncated": bool(state["stack_truncated"]),
                "captured_at": state["captured_at"], "run_gen": state["run_gen"], "stop_count": state["stop_count"]}

    @mcp.tool(annotations=control)
    async def rad_select_context(session_id: str, thread_id: int | None = None, frame_index: int | None = None,
                                  expected_stop_count: int | None = None) -> dict[str, Any]:
        """Select a thread and/or reported stack frame before evaluating locals. Frame indices come from rad_get_stack."""
        return await manager.select_context(session_id, thread_id, frame_index, expected_stop_count)

    @mcp.tool(annotations=read)
    async def rad_address_to_line(session_id: str, address: str) -> dict[str, Any]:
        """Resolve a virtual address to source lines in the selected process."""
        try:
            value = int(address, 0)
        except ValueError as exc:
            raise RadError("INVALID_ARGUMENT", "Use a numeric address such as 0x140001000.") from exc
        if not 0 < value < 2 ** 64:
            raise RadError("INVALID_ARGUMENT", "Address is outside the 64-bit address range.")
        session = manager.get(session_id)
        async with session.mutation_lock:
            before = await session.state()
            require_stopped(before)
            reply = parse_reply(await session.transport.command(f"line_from_vaddr -- 0x{value:x}"))
            lines = reply.get("lines", [])
            if lines == {}:
                lines = []
            return {"session_id": session_id, "address": f"0x{value:x}", "lines": lines,
                    "run_gen": before["run_gen"], "stop_count": before["stop_count"]}

    @mcp.tool(annotations=control)
    async def rad_open_crash_dump(session_id: str, path: str, timeout: float = 30.0) -> dict[str, Any]:
        """Load a local native crash dump in an empty session. Symbol and memory coverage depend on the dump and matching PDBs."""
        return await manager.open_dump(session_id, path, timeout)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def rad_close_session(session_id: str, terminate_debuggee: bool = False) -> dict[str, Any]:
        """Close RAD and detach/resume applications by default. terminate_debuggee=true explicitly terminates attached processes."""
        return await manager.close(session_id, terminate_debuggee)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone RAD Debugger MCP server (Windows x64, stdio).")
    parser.add_argument("--backend", type=Path, default=Path(os.getenv("RAD_MCP_BACKEND", str(DEFAULT_BACKEND))))
    parser.add_argument("--session-root", type=Path, default=None)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    build_server(SessionManager(args.backend, args.session_root)).run(transport="stdio")


if __name__ == "__main__":
    main()
