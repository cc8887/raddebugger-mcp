from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

from .errors import RadError
from .processes import list_processes
from .protocol import RadTransport, command_text, parse_reply

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKEND = REPO_ROOT / ".build/raddebugger/build/raddbg_non_graphical.exe"
LIST_FIELDS = ("processes", "threads", "modules", "locals", "lines", "stack", "targets", "breakpoints")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch(state: dict[str, Any]) -> tuple[int, int]:
    return int(state["run_gen"]), int(state["stop_count"])


def context(state: dict[str, Any]) -> tuple[int, ...]:
    return (*epoch(state), int(state.get("selected_thread_id", 0)), int(state.get("selected_unwind_count", 0)), int(state.get("selected_inline_depth", 0)))


def check_epoch(state: dict[str, Any], run_gen: int | None, stop_count: int | None) -> None:
    if (run_gen is not None and state["run_gen"] != run_gen) or (stop_count is not None and state["stop_count"] != stop_count):
        raise RadError("STALE_SNAPSHOT", f"Expected run_gen={run_gen}, stop_count={stop_count}; current run_gen={state['run_gen']}, stop_count={state['stop_count']}.")


def require_stopped(state: dict[str, Any]) -> None:
    if state["running"]:
        raise RadError("TARGET_RUNNING", "Pause or wait for a stop before inspecting values or stepping.")
    if not state["processes"]:
        raise RadError("NO_TARGET", "No debuggee is attached. Launch, attach, or open a dump first.")


def compact(state: dict[str, Any], *, modules: bool = False, stack: bool = False) -> dict[str, Any]:
    result = dict(state)
    result["module_count"] = len(result.get("modules", []))
    result["thread_count"] = len(result.get("threads", []))
    if not modules:
        result.pop("modules", None)
    if not stack:
        result.pop("stack", None)
    return result


@dataclass
class DebugSession:
    id: str
    name: str
    directory: Path
    backend: Path
    process: subprocess.Popen
    transport: RadTransport
    created_at: str = field(default_factory=utc_now)
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    program: str | None = None
    last_execution: tuple[int, int] | None = None
    closed: bool = False

    @property
    def project(self) -> Path:
        return self.directory / "session.raddbg_project"

    async def state(self) -> dict[str, Any]:
        if self.closed or self.process.poll() is not None:
            raise RadError("BACKEND_EXITED", f"RAD backend for session {self.id} has exited (code {self.process.poll()}).")
        reply = parse_reply(await self.transport.command("state"))
        state = reply.get("state")
        if not isinstance(state, dict) or not all(key in state for key in ("running", "run_gen", "stop_count")):
            raise RadError("BAD_REPLY", "RAD did not return the expected state object.")
        if state.get("mcp_bridge_version") != 1 or state.get("mcp_authenticated") != 1 or state.get("mcp_loopback_only") != 1:
            raise RadError("WRONG_BACKEND", "Build the authenticated loopback backend with scripts/build-backend.ps1.")
        for key in LIST_FIELDS:
            if state.get(key) == {}:
                state[key] = []
            if not isinstance(state.get(key), list):
                raise RadError("BAD_REPLY", f"RAD state field {key} is not a collection.")
        state.update(session_id=self.id, captured_at=utc_now(), running=bool(state["running"]))
        state["status"] = "running" if state["running"] else "stopped" if state["processes"] else "idle"
        return state

    async def wait_for_stop(self, after_run_gen: int, after_stop_count: int, timeout: float = 10.0) -> dict[str, Any]:
        if not 0 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Wait timeout must be between 0 and 60 seconds.")
        deadline = time.monotonic() + timeout
        saw_process = False
        while True:
            state = await self.state()
            saw_process |= bool(state["processes"])
            if not state["running"] and state["processes"] and state["stop_count"] > after_stop_count:
                return {"outcome": "stopped", "state": compact(state)}
            # Attach/launch can advance run_gen before a Process entity arrives.
            # An empty startup snapshot is not evidence that the application exited.
            if not state["processes"] and (state["stop_count"] > after_stop_count or saw_process):
                return {"outcome": "exited", "state": compact(state)}
            if time.monotonic() >= deadline:
                return {"outcome": "timeout", "state": compact(state), "message": "The target has not reached a new stop or exit; execution was not cancelled."}
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))


class SessionManager:
    def __init__(self, backend: Path = DEFAULT_BACKEND, session_root: Path | None = None):
        self.backend = backend.resolve()
        self.session_root = (session_root or REPO_ROOT / "sessions").resolve()
        self.sessions: dict[str, DebugSession] = {}
        self._creation_lock = asyncio.Lock()

    def get(self, session_id: str) -> DebugSession:
        if session_id not in self.sessions or self.sessions[session_id].closed:
            raise RadError("UNKNOWN_SESSION", "Unknown or closed session ID. Use rad_list_sessions.")
        return self.sessions[session_id]

    async def create(self, name: str = "native-debug", startup_timeout: float = 30.0) -> dict[str, Any]:
        if os.name != "nt":
            raise RadError("UNSUPPORTED_HOST", "This release supports Windows x64 hosts.")
        if not 1 <= startup_timeout <= 120:
            raise RadError("INVALID_ARGUMENT", "Startup timeout must be between 1 and 120 seconds.")
        if not self.backend.is_file():
            raise RadError("BACKEND_MISSING", f"Build the RAD backend first: {self.backend}")
        async with self._creation_lock:
            if sum(not s.closed for s in self.sessions.values()) >= 8:
                raise RadError("SESSION_LIMIT", "Close a session before creating more than eight backends.")
            session_id = uuid.uuid4().hex
            directory = self.session_root / session_id
            directory.mkdir(parents=True)
            (directory / "logs").mkdir()
            project = directory / "session.raddbg_project"
            project.write_text("// raddbg 0.9.30 project file\n", encoding="utf-8")
            user = directory / "session.raddbg_user"
            # Let RAD create its default logical window/panels, even with the stub renderer.
            # RAD rejects pre-existing files without its versioned config header.
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            token = secrets.token_hex(32)
            env = os.environ.copy()
            env["RAD_MCP_TOKEN"] = token
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            process = subprocess.Popen(
                [str(self.backend), f"--ipc_port:{port}", "--async_thread_count:2", "--no_focus_on_stop",
                 f"--user:{user.as_posix()}", f"--project:{project.as_posix()}", f"--logs:{(directory / 'logs').as_posix()}"],
                cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup,
            )
            session = DebugSession(session_id, name[:128], directory, self.backend, process, RadTransport(port, token))
            self.sessions[session_id] = session
            try:
                deadline = time.monotonic() + startup_timeout
                last_error: Exception | None = None
                while time.monotonic() < deadline:
                    try:
                        state = await session.state()
                        return {"session_id": session_id, "name": session.name, "backend_pid": process.pid,
                                "backend_endpoint": f"127.0.0.1:{port}", "state": compact(state)}
                    except RadError as exc:
                        last_error = exc
                        if process.poll() is not None or exc.code in ("BAD_REPLY", "WRONG_BACKEND"):
                            raise
                        await asyncio.sleep(0.1)
                raise RadError("STARTUP_TIMEOUT", f"Backend did not become ready. Last error: {last_error}")
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await session.transport.close()
                    if process.poll() is None:
                        process.terminate()  # No target can have been attached before startup completed.
                        await asyncio.to_thread(process.wait, 5)
                    session.closed = True
                raise

    async def list(self) -> dict[str, Any]:
        return {"sessions": [
            {"session_id": s.id, "name": s.name, "backend_pid": s.process.pid, "program": s.program,
             "created_at": s.created_at, "backend_alive": s.process.poll() is None}
            for s in self.sessions.values() if not s.closed
        ]}

    async def launch(self, session_id: str, program: str, args: list[str] | None = None,
                     working_directory: str | None = None, stop_at_entry: bool = True,
                     timeout: float = 30.0, debug_subprocesses: bool = False) -> dict[str, Any]:
        session = self.get(session_id)
        path = Path(program).expanduser().resolve()
        if not path.is_file():
            raise RadError("FILE_NOT_FOUND", f"Executable does not exist: {path}")
        cwd = Path(working_directory).expanduser().resolve() if working_directory else path.parent
        if not cwd.is_dir():
            raise RadError("DIRECTORY_NOT_FOUND", f"Working directory does not exist: {cwd}")
        if not 1 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Launch timeout must be between 1 and 60 seconds.")
        command_text(path.as_posix(), "program path")
        # Arguments are serialized to private target input, never to an IPC command or shell.
        for arg in args or []:
            if "\0" in arg:
                raise RadError("INVALID_ARGUMENT", "Target arguments cannot contain NUL.")
        quote = lambda value: json.dumps(value, ensure_ascii=False)
        target = ("// raddbg 0.9.30 project file\ntarget:\n{\n"
                  f" executable: {quote(path.as_posix())}\n"
                  f" arguments: {quote(subprocess.list2cmdline(args or []))}\n"
                  f" working_directory: {quote(cwd.as_posix())}\n"
                  f" stdout_path: {quote((session.directory / 'stdout.txt').as_posix())}\n"
                  f" stderr_path: {quote((session.directory / 'stderr.txt').as_posix())}\n"
                  f" debug_subprocesses: {int(debug_subprocesses)}\n enabled: 1\n}}\n")
        async with session.mutation_lock:
            before = await session.state()
            if before["processes"]:
                raise RadError("TARGET_EXISTS", "Use a new session or detach the current target before launching.")
            # RAD autosaves the active project. Give it a separate immutable input and
            # apply the target natively, preserving existing config IDs and breakpoints.
            target_input = session.directory / "target-input.raddbg"
            target_input.write_text(target, encoding="utf-8")
            configured = parse_reply(await session.transport.command(f"mcp_configure_target -- {command_text(target_input.as_posix())}"))
            if configured.get("target_configured") != 1:
                raise RadError("TARGET_CONFIG_FAILED", "RAD could not apply the target configuration.")
            deadline = time.monotonic() + timeout
            while True:
                state = await session.state()
                if any(t.get("executable", "").replace("\\", "/").casefold() == path.as_posix().casefold() and t.get("enabled") for t in state["targets"]):
                    break
                if time.monotonic() > deadline:
                    raise RadError("TARGET_CONFIG_TIMEOUT", "RAD did not load the target configuration.")
                await asyncio.sleep(0.05)
            session.program = str(path)
            session.last_execution = epoch(before)
            await session.transport.command("step_into" if stop_at_entry else "run")
        if stop_at_entry:
            return await session.wait_for_stop(*epoch(before), timeout=timeout)
        return {"outcome": "requested", "wait_after_run_gen": before["run_gen"], "wait_after_stop_count": before["stop_count"]}

    async def attach(self, session_id: str, pid: int, timeout: float = 15.0) -> dict[str, Any]:
        session = self.get(session_id)
        if pid <= 0 or pid in {os.getpid(), *(s.process.pid for s in self.sessions.values())}:
            raise RadError("INVALID_PID", "Choose an application PID, not the MCP service or a debugger backend.")
        if not any(p["pid"] == pid for p in list_processes()):
            raise RadError("PROCESS_NOT_FOUND", f"PID {pid} is not running.")
        if not 1 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Attach timeout must be between 1 and 60 seconds.")
        async with session.mutation_lock:
            before = await session.state()
            if before["processes"]:
                raise RadError("TARGET_EXISTS", "Attach a new process in its own session.")
            session.last_execution = epoch(before)
            await session.transport.command(f"attach -- {pid}")
        result = await session.wait_for_stop(*epoch(before), timeout=timeout)
        if result["outcome"] == "stopped" and not any(p["id"] == pid for p in result["state"]["processes"]):
            raise RadError("ATTACH_FAILED", "RAD stopped without attaching the requested PID.")
        return result

    async def execute(self, session_id: str, action: str, *, expected_run_gen: int | None = None,
                      expected_stop_count: int | None = None, timeout: float = 10.0) -> dict[str, Any]:
        actions = {"continue": "continue", "pause": "halt", "step_into": "step_into_line",
                   "step_over": "step_over_line", "step_out": "step_out",
                   "step_into_instruction": "step_into_inst", "step_over_instruction": "step_over_inst"}
        if action not in actions or not 0 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Unknown execution action or timeout outside 0..60 seconds.")
        session = self.get(session_id)
        async with session.mutation_lock:
            before = await session.state()
            check_epoch(before, expected_run_gen, expected_stop_count)
            if not before["processes"]:
                raise RadError("NO_TARGET", "No debuggee is attached.")
            if any(p.get("is_dump") for p in before["processes"]):
                raise RadError("DUMP_READ_ONLY", "A crash dump cannot execute.")
            if action == "pause" and not before["running"]:
                return {"outcome": "already_stopped", "state": compact(before)}
            if action != "pause":
                require_stopped(before)
            session.last_execution = epoch(before)
            await session.transport.command(actions[action])
        if action == "continue" or timeout == 0:
            return {"outcome": "requested", "wait_after_run_gen": before["run_gen"], "wait_after_stop_count": before["stop_count"]}
        return await session.wait_for_stop(*epoch(before), timeout=timeout)

    async def set_breakpoint(self, session_id: str, kind: str, file: str | None = None, line: int | None = None,
                             function: str | None = None, address: str | None = None) -> dict[str, Any]:
        session = self.get(session_id)
        if kind == "line" and file and line and line > 0 and not function and not address:
            path = Path(file).expanduser().resolve().as_posix()
            command = f"add_breakpoint -- {command_text(path, 'source path')}:{line}:1"
            matches = lambda bp: bp.get("file", "").casefold().replace("\\", "/") == path.casefold() and bp.get("line") == line
        elif kind == "function" and function and not file and not address and line is None:
            command = f"add_function_breakpoint -- {command_text(function, 'function')}"
            matches = lambda bp: bp.get("expression") == function
        elif kind == "address" and address and not file and not function and line is None:
            try:
                value = int(address, 0)
            except ValueError as exc:
                raise RadError("INVALID_ARGUMENT", "Address must be an integer string such as 0x140001000.") from exc
            if not 0 < value < 2 ** 64:
                raise RadError("INVALID_ARGUMENT", "Address is outside the 64-bit address range.")
            expression = f"0x{value:x}"
            command = f"add_address_breakpoint -- {expression}"
            matches = lambda bp: bp.get("expression") == expression
        else:
            raise RadError("INVALID_ARGUMENT", "Specify exactly file+line, function, or address for the selected kind.")
        async with session.mutation_lock:
            await session.transport.command(command)
            state = await session.state()
            found = [bp for bp in state["breakpoints"] if matches(bp)]
            if not found:
                raise RadError("BREAKPOINT_NOT_CONFIGURED", "RAD did not create the requested breakpoint.")
            return {"session_id": session_id, "breakpoint": found[0], "resolution": "configured", "note": "Configured is not proof of address resolution. A hit or a resolved-address probe is required."}

    async def remove_breakpoint(self, session_id: str, breakpoint_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        async with session.mutation_lock:
            before = await session.state()
            if not any(bp["id"] == breakpoint_id for bp in before["breakpoints"]):
                raise RadError("UNKNOWN_BREAKPOINT", "Breakpoint ID does not belong to this session.")
            await session.transport.command(f"--cfg:{command_text(breakpoint_id)} remove_cfg")
            state = await session.state()
            if any(bp["id"] == breakpoint_id for bp in state["breakpoints"]):
                raise RadError("BREAKPOINT_NOT_REMOVED", "RAD still reports the breakpoint.")
            return {"session_id": session_id, "removed": breakpoint_id}

    async def evaluate(self, session_id: str, expression: str, expected_run_gen: int | None = None,
                       expected_stop_count: int | None = None) -> dict[str, Any]:
        command_text(expression, "expression")
        if len(expression.encode("utf-8")) > 2048:
            raise RadError("INVALID_ARGUMENT", "Expressions are limited to 2048 UTF-8 bytes.")
        session = self.get(session_id)
        async with session.mutation_lock:
            before = await session.state()
            require_stopped(before)
            check_epoch(before, expected_run_gen, expected_stop_count)
            value: dict[str, Any] = {}
            for attempt in range(3):
                reply = parse_reply(await session.transport.command(f"eval -- {expression}"))
                value = reply.get("eval", {})
                after = await session.state()
                if after["running"] or context(before) != context(after):
                    raise RadError("STALE_SNAPSHOT", "The debug stop, thread, or stack frame changed during evaluation.")
                if not isinstance(value, dict) or not all(k in value for k in ("expr", "value", "type", "msgs")):
                    raise RadError("BAD_REPLY", "RAD did not return the expected eval object.")
                interpretation_failed = any(value.get(key) not in (None, 0) for key in ("interpretation_code", "type_interpretation_code"))
                if not value["msgs"] and not value.get("read_failed") and not interpretation_failed:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.05)
            return {"session_id": session_id, "expression": expression, "value": value["value"], "type": value["type"],
                    "messages": value["msgs"], "ok": not bool(value["msgs"] or value.get("read_failed") or interpretation_failed), "captured_at": after["captured_at"],
                    "run_gen": after["run_gen"], "stop_count": after["stop_count"],
                    "thread_id": after["selected_thread_id"], "unwind_count": after["selected_unwind_count"],
                    "inline_depth": after["selected_inline_depth"], "consistency": "same_stop_epoch",
                    "memory_freshness": value.get("memory_freshness", "backend_cached"),
                    "read_failed": bool(value.get("read_failed", False)), "display_limit_chars": 512,
                    "interpretation_code": value.get("interpretation_code"),
                    "type_interpretation_code": value.get("type_interpretation_code"),
                    "possibly_truncated": len(value["value"]) >= 510}

    async def select_context(self, session_id: str, thread_id: int | None = None,
                             frame_index: int | None = None, expected_stop_count: int | None = None) -> dict[str, Any]:
        session = self.get(session_id)
        async with session.mutation_lock:
            state = await session.state()
            require_stopped(state)
            check_epoch(state, None, expected_stop_count)
            if thread_id is not None:
                if not any(t["id"] == thread_id for t in state["threads"]):
                    raise RadError("UNKNOWN_THREAD", "Thread does not belong to this session.")
                await session.transport.command(f"select_thread -- {thread_id}")
                state = await session.state()
                if state["selected_thread_id"] != thread_id:
                    raise RadError("CONTEXT_NOT_SELECTED", "RAD did not select the requested thread.")
            if frame_index is not None:
                frames = state["stack"]
                if not 0 <= frame_index < len(frames):
                    raise RadError("UNKNOWN_FRAME", "Frame index is outside the reported stack.")
                frame = frames[frame_index]
                await session.transport.command(f"--unwind_count:{frame['unwind_count']} --inline_depth:{frame['inline_depth']} select_unwind")
                state = await session.state()
                if (state["selected_unwind_count"], state["selected_inline_depth"]) != (frame["unwind_count"], frame["inline_depth"]):
                    raise RadError("CONTEXT_NOT_SELECTED", "RAD did not select the requested stack frame.")
            return compact(state)

    async def run_to_line(self, session_id: str, file: str, line: int, timeout: float = 10.0) -> dict[str, Any]:
        session = self.get(session_id)
        if line <= 0 or not 0 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Line must be positive and timeout within 0..60 seconds.")
        path = command_text(Path(file).expanduser().resolve().as_posix(), "source path")
        async with session.mutation_lock:
            before = await session.state()
            require_stopped(before)
            if any(p.get("is_dump") for p in before["processes"]):
                raise RadError("DUMP_READ_ONLY", "A crash dump cannot execute.")
            session.last_execution = epoch(before)
            await session.transport.command(f"run_to_line -- {path}:{line}:1")
        return await session.wait_for_stop(*epoch(before), timeout=timeout)

    async def open_dump(self, session_id: str, path: str, timeout: float = 30.0) -> dict[str, Any]:
        session = self.get(session_id)
        dump = Path(path).expanduser().resolve()
        if not dump.is_file():
            raise RadError("FILE_NOT_FOUND", f"Crash dump does not exist: {dump}")
        if not 1 <= timeout <= 60:
            raise RadError("INVALID_ARGUMENT", "Dump timeout must be between 1 and 60 seconds.")
        async with session.mutation_lock:
            state = await session.state()
            if state["processes"]:
                raise RadError("TARGET_EXISTS", "Open the crash dump in a separate empty session.")
            await session.transport.command(f"open_crash_dump -- {command_text(dump.as_posix())}")
            deadline = time.monotonic() + timeout
            while True:
                state = await session.state()
                if any(p.get("is_dump") for p in state["processes"]):
                    return {"outcome": "dump_loaded", "state": compact(state)}
                if time.monotonic() >= deadline:
                    raise RadError("DUMP_LOAD_TIMEOUT", "RAD did not load a dump process within the timeout.")
                await asyncio.sleep(0.05)

    async def close(self, session_id: str, terminate_debuggee: bool = False) -> dict[str, Any]:
        session = self.get(session_id)
        async with session.mutation_lock:
            if session.process.poll() is None:
                state = await session.state()
                if state["running"]:
                    await session.transport.command("halt")
                    stopped = await session.wait_for_stop(*epoch(state), timeout=5)
                    state = stopped["state"]
                    if state["running"]:
                        raise RadError("DETACH_FAILED", "Target did not pause; session is retained for retry.")
                live_processes = [p for p in state["processes"] if not p.get("is_dump")]
                if terminate_debuggee and live_processes:
                    await session.transport.command("kill_all")
                else:
                    for process in live_processes:
                        await session.transport.command(f"detach -- {process['id']}")
                deadline = time.monotonic() + 5
                while any(not p.get("is_dump") for p in state["processes"]):
                    state = await session.state()
                    if not any(not p.get("is_dump") for p in state["processes"]):
                        break
                    if time.monotonic() >= deadline:
                        raise RadError("DETACH_FAILED", "RAD still has attached processes; session is retained for retry.")
                    await asyncio.sleep(0.05)
                try:
                    await session.transport.command("exit", timeout=1)
                except RadError:
                    # The exit reply can race the backend's socket shutdown. Targets are detached.
                    pass
                try:
                    await asyncio.to_thread(session.process.wait, 3)
                except subprocess.TimeoutExpired:
                    session.process.terminate()  # All target processes were already detached.
                    await asyncio.to_thread(session.process.wait, 3)
            await session.transport.close()
            session.closed = True
            return {"session_id": session_id, "outcome": "closed", "debuggee_action": "terminate" if terminate_debuggee else "detach"}

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            if session.closed:
                continue
            try:
                await self.close(session.id)
            except Exception:
                # Kill-on-exit is explicitly disabled in our backend. Never kill an application here.
                await session.transport.close()
                if session.process.poll() is None:
                    session.process.terminate()
                    await asyncio.to_thread(session.process.wait, 5)
                session.closed = True
