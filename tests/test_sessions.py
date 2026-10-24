"""Controller contracts without launching RAD, opening sockets, or touching targets."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.sessions import DebugSession, SessionManager


def snapshot(**changes):
    state = {
        "running": False, "run_gen": 7, "stop_count": 9,
        "selected_thread_id": 201, "selected_unwind_count": 0,
        "selected_inline_depth": 0, "captured_at": "2026-10-24T00:00:00+00:00",
        "processes": [{"id": 101, "is_dump": 0}],
        "threads": [{"id": 201}], "modules": [], "locals": [], "lines": [],
        "stack": [{"index": 0, "unwind_count": 0, "inline_depth": 0}],
        "targets": [], "breakpoints": [],
    }
    state.update(changes)
    return copy.deepcopy(state)


def wire_state(*, bridge=1, authenticated=1, loopback=1, processes="{}"):
    return (
        "state: { running: 0 run_gen: 7 stop_count: 9 "
        f"mcp_bridge_version: {bridge} mcp_authenticated: {authenticated} "
        f"mcp_loopback_only: {loopback} processes: {processes} "
        "threads: {} modules: {} locals: {} lines: {} stack: {} targets: {} breakpoints: {} }"
    )


EVAL_REPLY = 'eval: { expr: "counter" value: "12" type: "int" msgs: "" }'


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, states=None):
        process = MagicMock()
        process.pid = 9001
        process.poll.return_value = None
        transport = MagicMock()
        transport.command = AsyncMock(return_value=EVAL_REPLY)
        transport.close = AsyncMock()
        session = DebugSession(
            "session-a", "test", Path("unused-session"), Path("unused-backend.exe"),
            process, transport,
        )
        if states is not None:
            session.state = AsyncMock(side_effect=states)
        manager = SessionManager(Path("unused-backend.exe"), Path("unused-session-root"))
        manager.sessions[session.id] = session
        return manager, session, transport, process

    async def test_wait_does_not_report_the_existing_stop_as_new(self):
        _, session, transport, _ = self.make_session([snapshot()])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["state"]["stop_count"], 9)
        transport.command.assert_not_awaited()

    async def test_execution_generation_alone_is_not_a_new_stop(self):
        _, session, _, _ = self.make_session([snapshot(run_gen=8)])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "timeout")

    async def test_stop_count_can_advance_without_a_new_run_generation(self):
        _, session, _, _ = self.make_session([snapshot(stop_count=10)])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "stopped")

    async def test_wait_observes_running_then_the_next_stop(self):
        _, session, _, _ = self.make_session([
            snapshot(), snapshot(running=True, run_gen=8),
            snapshot(run_gen=8, stop_count=10),
        ])
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await session.wait_for_stop(7, 9, timeout=1)
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["state"]["stop_count"], 10)
        self.assertEqual(session.state.await_count, 3)

    async def test_timeout_leaves_running_target_and_backend_untouched(self):
        _, session, transport, process = self.make_session([snapshot(running=True)])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(result["state"]["running"])
        self.assertIn("not cancelled", result["message"])
        transport.command.assert_not_awaited()
        transport.close.assert_not_awaited()
        process.terminate.assert_not_called()
        self.assertFalse(session.closed)

    async def test_empty_idle_session_is_not_reported_as_exited(self):
        _, session, _, _ = self.make_session([snapshot(processes=[])])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "timeout")

    async def test_exit_is_detected_if_target_completed_before_first_poll(self):
        _, session, _, _ = self.make_session([snapshot(processes=[], run_gen=8, stop_count=10)])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "exited")

    async def test_attach_startup_generation_does_not_falsely_report_exit(self):
        _, session, _, _ = self.make_session([snapshot(processes=[], run_gen=8)])
        result = await session.wait_for_stop(7, 9, timeout=0)
        self.assertEqual(result["outcome"], "timeout")

    async def test_dump_close_does_not_attempt_native_detach_or_kill(self):
        manager, session, transport, process = self.make_session([snapshot(processes=[{"id": 101, "is_dump": 1}])])
        result = await manager.close(session.id)
        self.assertEqual(result["outcome"], "closed")
        self.assertEqual([c.args[0] for c in transport.command.await_args_list], ["exit"])
        process.terminate.assert_not_called()

    async def test_exit_is_detected_after_observing_a_process(self):
        _, session, _, _ = self.make_session([
            snapshot(running=True), snapshot(processes=[]),
        ])
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await session.wait_for_stop(7, 9, timeout=1)
        self.assertEqual(result["outcome"], "exited")

    async def test_wrong_backend_handshake_is_rejected(self):
        for options in ({"bridge": 0}, {"authenticated": 0}, {"loopback": 0}):
            with self.subTest(**options):
                _, session, transport, _ = self.make_session()
                transport.command.return_value = wire_state(**options)
                with self.assertRaisesRegex(RadError, "WRONG_BACKEND"):
                    await session.state()

    async def test_state_normalizes_empty_collections(self):
        _, session, transport, _ = self.make_session()
        transport.command.return_value = wire_state()
        state = await session.state()
        self.assertEqual(state["status"], "idle")
        self.assertIs(state["running"], False)
        for key in ("processes", "threads", "modules", "locals", "lines", "stack", "targets", "breakpoints"):
            self.assertEqual(state[key], [])

    async def test_malformed_process_collection_is_rejected(self):
        _, session, transport, _ = self.make_session()
        transport.command.return_value = wire_state(processes="1")
        with self.assertRaisesRegex(RadError, "BAD_REPLY"):
            await session.state()

    async def test_exited_backend_is_not_contacted(self):
        _, session, transport, process = self.make_session()
        process.poll.return_value = 3
        with self.assertRaisesRegex(RadError, "BACKEND_EXITED"):
            await session.state()
        transport.command.assert_not_awaited()

    async def test_running_or_stale_eval_is_rejected_before_eval(self):
        for state, expected, code in (
            (snapshot(running=True), {}, "TARGET_RUNNING"),
            (snapshot(), {"expected_stop_count": 8}, "STALE_SNAPSHOT"),
            (snapshot(), {"expected_run_gen": 6}, "STALE_SNAPSHOT"),
        ):
            with self.subTest(code=code, expected=expected):
                manager, session, transport, _ = self.make_session([state])
                with self.assertRaisesRegex(RadError, code):
                    await manager.evaluate(session.id, "counter", **expected)
                transport.command.assert_not_awaited()

    async def test_eval_rejects_result_if_stop_or_context_changes(self):
        changes = (
            {"running": True}, {"run_gen": 8}, {"stop_count": 10},
            {"selected_thread_id": 202}, {"selected_unwind_count": 1},
            {"selected_inline_depth": 1},
        )
        for change in changes:
            with self.subTest(change=change):
                manager, session, transport, _ = self.make_session([snapshot(), snapshot(**change)])
                with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
                    await manager.evaluate(session.id, "counter")
                self.assertEqual(transport.command.await_count, 1)

    async def test_successful_eval_reports_epoch_and_cache_limit(self):
        manager, session, _, _ = self.make_session([snapshot(), snapshot()])
        result = await manager.evaluate(session.id, "counter", 7, 9)
        self.assertTrue(result["ok"])
        self.assertEqual((result["value"], result["thread_id"]), ("12", 201))
        self.assertEqual((result["run_gen"], result["stop_count"]), (7, 9))
        self.assertEqual(result["memory_freshness"], "backend_cached")

    async def test_eval_propagates_direct_process_read_evidence(self):
        manager, session, transport, _ = self.make_session([snapshot(), snapshot()])
        transport.command.return_value = EVAL_REPLY.replace('msgs: ""', 'msgs: "" memory_freshness: "direct_process_read" read_failed: 0')
        result = await manager.evaluate(session.id, "counter")
        self.assertTrue(result["ok"])
        self.assertEqual(result["memory_freshness"], "direct_process_read")
        self.assertFalse(result["read_failed"])

    async def test_failed_read_cannot_be_success_even_without_eval_messages(self):
        manager, session, transport, _ = self.make_session([snapshot()] * 4)
        transport.command.return_value = EVAL_REPLY.replace('msgs: ""', 'msgs: "" memory_freshness: "direct_process_read" read_failed: 1')
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await manager.evaluate(session.id, "counter")
        self.assertFalse(result["ok"])
        self.assertTrue(result["read_failed"])
        self.assertEqual(transport.command.await_count, 3)

    async def test_interpretation_failure_cannot_be_success_without_messages(self):
        manager, session, transport, _ = self.make_session([snapshot()] * 4)
        transport.command.return_value = EVAL_REPLY.replace('msgs: ""', 'msgs: "" interpretation_code: 7 type_interpretation_code: 0')
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await manager.evaluate(session.id, "counter")
        self.assertFalse(result["ok"])
        self.assertEqual(result["interpretation_code"], 7)

    async def test_attach_rejects_own_service_and_backend_pids(self):
        manager, session, transport, process = self.make_session()
        with patch("raddebugger_mcp.sessions.list_processes") as processes:
            for pid in (os.getpid(), process.pid):
                with self.subTest(pid=pid), self.assertRaisesRegex(RadError, "INVALID_PID"):
                    await manager.attach(session.id, pid)
            processes.assert_not_called()
        transport.command.assert_not_awaited()

    async def test_launch_configures_separate_input_without_overwriting_active_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            program = directory / "debuggee.exe"
            program.write_bytes(b"test executable placeholder")
            manager, session, transport, _ = self.make_session([
                snapshot(processes=[]),
                snapshot(processes=[], targets=[{"executable": program.as_posix(), "enabled": 1}]),
            ])
            session.directory = directory
            active_project = b'// raddbg 0.9.30 project file\nbreakpoint: { address_location: existing_breakpoint }\n'
            session.project.write_bytes(active_project)
            target_input = directory / "target-input.raddbg"

            async def command(text):
                self.assertEqual(session.project.read_bytes(), active_project)
                self.assertTrue(target_input.is_file())
                self.assertNotEqual(target_input, session.project)
                return "target_configured: 1"

            transport.command.side_effect = command
            result = await manager.launch(session.id, str(program), args=["--loop"], stop_at_entry=False)

            self.assertEqual(result["outcome"], "requested")
            self.assertEqual(session.project.read_bytes(), active_project)
            target_text = target_input.read_text(encoding="utf-8")
            self.assertIn(f'executable: "{program.as_posix()}"', target_text)
            self.assertIn('arguments: "--loop"', target_text)
            self.assertEqual(
                [call.args[0] for call in transport.command.await_args_list],
                [f"mcp_configure_target -- {target_input.as_posix()}", "run"],
            )

    async def test_attach_requires_membership_in_current_process_list(self):
        manager, session, transport, _ = self.make_session()
        with patch("raddebugger_mcp.sessions.list_processes", return_value=[{"pid": 404}]):
            with self.assertRaisesRegex(RadError, "PROCESS_NOT_FOUND"):
                await manager.attach(session.id, 405)
        transport.command.assert_not_awaited()

    async def test_attach_verifies_the_backend_attached_requested_pid(self):
        manager, session, transport, _ = self.make_session([snapshot(processes=[])])
        session.wait_for_stop = AsyncMock(return_value={"outcome": "stopped", "state": snapshot()})
        with patch("raddebugger_mcp.sessions.list_processes", return_value=[{"pid": 405}]):
            with self.assertRaisesRegex(RadError, "ATTACH_FAILED"):
                await manager.attach(session.id, 405)
        transport.command.assert_awaited_once_with("attach -- 405")

    async def test_context_selection_requires_thread_membership(self):
        manager, session, transport, _ = self.make_session([snapshot()])
        with self.assertRaisesRegex(RadError, "UNKNOWN_THREAD"):
            await manager.select_context(session.id, thread_id=999)
        transport.command.assert_not_awaited()

    async def test_execution_rejects_a_crash_dump(self):
        manager, session, transport, _ = self.make_session([snapshot(processes=[{"id": 101, "is_dump": 1}])])
        with self.assertRaisesRegex(RadError, "DUMP_READ_ONLY"):
            await manager.execute(session.id, "continue")
        transport.command.assert_not_awaited()

    async def test_execution_rejects_outdated_client_stop_before_resuming(self):
        manager, session, transport, _ = self.make_session([snapshot()])
        with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            await manager.execute(session.id, "continue", expected_stop_count=8)
        transport.command.assert_not_awaited()

    async def test_close_detaches_targets_before_backend_exit(self):
        manager, session, transport, process = self.make_session([
            snapshot(processes=[{"id": 101}, {"id": 102}]), snapshot(processes=[]),
        ])
        result = await manager.close(session.id)
        self.assertEqual(result["debuggee_action"], "detach")
        commands = [args.args[0] for args in transport.command.await_args_list]
        self.assertEqual(commands, ["detach -- 101", "detach -- 102", "exit"])
        transport.close.assert_awaited_once()
        process.terminate.assert_not_called()
        self.assertTrue(session.closed)

    async def test_explicit_termination_uses_kill_instead_of_detach(self):
        manager, session, transport, _ = self.make_session([snapshot(), snapshot(processes=[])])
        result = await manager.close(session.id, terminate_debuggee=True)
        self.assertEqual(result["debuggee_action"], "terminate")
        commands = [args.args[0] for args in transport.command.await_args_list]
        self.assertEqual(commands, ["kill_all", "exit"])

    async def test_failed_pause_keeps_session_available_for_retry(self):
        manager, session, transport, process = self.make_session([snapshot(running=True)])
        session.wait_for_stop = AsyncMock(return_value={"outcome": "timeout", "state": snapshot(running=True)})
        with self.assertRaisesRegex(RadError, "DETACH_FAILED"):
            await manager.close(session.id)
        transport.command.assert_awaited_once_with("halt")
        transport.close.assert_not_awaited()
        process.terminate.assert_not_called()
        self.assertIs(manager.get(session.id), session)

    async def test_failed_native_detach_keeps_session_and_never_exits_backend(self):
        manager, session, transport, process = self.make_session([snapshot(), snapshot()])
        # A native detach failure leaves the process entity attached. Do not
        # accept an IPC acknowledgement or Stopped event as successful detach.
        with patch("raddebugger_mcp.sessions.time.monotonic", side_effect=[10, 16]):
            with self.assertRaisesRegex(RadError, "DETACH_FAILED"):
                await manager.close(session.id)
        transport.command.assert_awaited_once_with("detach -- 101")
        transport.close.assert_not_awaited()
        process.terminate.assert_not_called()
        self.assertFalse(session.closed)
        self.assertIs(manager.get(session.id), session)


if __name__ == "__main__":
    unittest.main()
