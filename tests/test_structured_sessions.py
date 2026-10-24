"""Structured reads must retain the original debug context and mutation lock."""
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.sessions import DebugSession, SessionManager


def state(**updates):
    result = dict(running=False, run_gen=7, stop_count=9, selected_process_id=101,
                  selected_thread_id=201, selected_unwind_count=0, selected_inline_depth=0,
                  processes=[dict(id=101)], captured_at="2026-10-24T00:00:00Z")
    result.update(updates)
    return result


class StructuredSessionTests(unittest.IsolatedAsyncioTestCase):
    def setup_session(self, states):
        process = MagicMock()
        transport = MagicMock()
        transport.command = AsyncMock(return_value='eval: { expr: "x" value: "12" type: "int" msgs: "" memory_freshness: "direct_process_read" }')
        session = DebugSession("s", "test", Path("unused"), Path("unused.exe"), process, transport)
        session.state = AsyncMock(side_effect=states)
        manager = SessionManager(Path("unused.exe"), Path("unused"))
        manager.sessions[session.id] = session
        return manager, session, transport

    async def test_running_and_stale_requests_never_probe(self):
        for initial, expected, code in ((state(running=True), {}, "TARGET_RUNNING"),
                                        (state(), dict(expected_stop_count=8), "STALE_SNAPSHOT")):
            with self.subTest(code=code):
                manager, session, transport = self.setup_session([initial])
                with self.assertRaisesRegex(RadError, code):
                    await manager.read_value(session.id, "x", **expected)
                transport.command.assert_not_awaited()

    async def test_injection_is_rejected_before_accessing_target(self):
        manager, session, transport = self.setup_session([])
        with self.assertRaisesRegex(RadError, "INVALID_ARGUMENT"):
            await manager.read_value(session.id, "x; kill_all")
        session.state.assert_not_awaited()
        transport.command.assert_not_awaited()

    async def test_lock_and_original_epoch_cover_each_probe(self):
        manager, session, transport = self.setup_session([state(), state(), state()])

        class Reader:
            def __init__(self, probe, **kwargs):
                self.probe = probe

            async def read(self, expression, **kwargs):
                self.assert_locked = session.mutation_lock.locked()
                if not self.assert_locked:
                    raise AssertionError("The outer read must own mutation_lock")
                value = await self.probe(expression)
                return dict(ok=True, value=int(value["value"]))

        with patch("raddebugger_mcp.sessions.ValueReader", Reader):
            result = await manager.read_value(session.id, "x", expected_run_gen=7, expected_stop_count=9)
        self.assertEqual(result["value"], 12)
        self.assertEqual((result["run_gen"], result["stop_count"], result["process_id"]), (7, 9, 101))
        self.assertFalse(session.mutation_lock.locked())
        transport.command.assert_awaited_once_with("eval -- x")

    async def test_process_change_during_probe_is_stale(self):
        manager, session, _ = self.setup_session([state(), state(selected_process_id=102)])

        class Reader:
            def __init__(self, probe, **kwargs):
                self.probe = probe

            async def read(self, expression, **kwargs):
                await self.probe(expression)
                return dict(ok=True)

        with patch("raddebugger_mcp.sessions.ValueReader", Reader), self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            await manager.read_value(session.id, "x")
        self.assertFalse(session.mutation_lock.locked())

    async def test_final_context_change_is_not_a_completed_read(self):
        manager, session, _ = self.setup_session([state(), state(selected_unwind_count=1)])
        reader = MagicMock()
        reader.read = AsyncMock(return_value=dict(ok=True, value="not accepted"))
        with patch("raddebugger_mcp.sessions.ValueReader", return_value=reader), self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            await manager.read_value(session.id, "x")

    async def test_cancellation_releases_lock_and_never_closes_target(self):
        manager, session, transport = self.setup_session([state()])
        reader = MagicMock()
        reader.read = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("raddebugger_mcp.sessions.ValueReader", return_value=reader), self.assertRaises(asyncio.CancelledError):
            await manager.read_value(session.id, "x")
        self.assertFalse(session.mutation_lock.locked())
        transport.command.assert_not_awaited()

    async def test_native_truncation_metadata_overrides_length_heuristic(self):
        for native_flag, value in ((1, "短"), (0, "x" * 511)):
            manager, session, transport = self.setup_session([state(), state()])
            transport.command.return_value = f'eval: {{ expr: "x" value: "{value}" type: "FString" msgs: "" value_truncated: {native_flag} value_encoding_valid: 1 display_limit_bytes: 512 }}'
            result = await manager.evaluate(session.id, "x")
            self.assertEqual(result["possibly_truncated"], bool(native_flag))
            self.assertEqual(result["display_limit_bytes"], 512)
            self.assertEqual(result["ok"], not bool(native_flag))

    async def test_native_type_truncation_is_not_success_without_messages(self):
        manager, session, transport = self.setup_session([state(), state()])
        transport.command.return_value = 'eval: { expr: "x" value: "12" type: "Some..." msgs: "" value_truncated: 0 type_truncated: 1 }'
        result = await manager.evaluate(session.id, "x")
        self.assertFalse(result["ok"])
        self.assertTrue(result["type_truncated"])
        self.assertFalse(result["possibly_truncated"])

    async def test_structured_read_rejects_missing_cached_or_unknown_freshness(self):
        for marker in ("", 'memory_freshness: "backend_cached"', 'memory_freshness: "unknown"'):
            with self.subTest(marker=marker):
                manager, session, transport = self.setup_session([state(), state()])
                transport.command.return_value = f'eval: {{ expr: "sizeof(void *)" value: "8" type: "uint64" msgs: "" {marker} }}'
                with self.assertRaisesRegex(RadError, "UNSUPPORTED_BACKEND"):
                    await manager.read_value(session.id, "x")
                transport.command.assert_awaited_once_with("eval -- sizeof(void *)")
                self.assertFalse(session.mutation_lock.locked())
                self.assertFalse(session.closed)

    async def test_direct_read_failure_is_not_an_empty_value_or_fallback(self):
        manager, session, transport = self.setup_session([state()] * 5)
        transport.command.return_value = 'eval: { expr: "sizeof(void *)" value: "0" type: "uint64" msgs: "" memory_freshness: "direct_process_read" read_failed: 1 }'
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await manager.read_value(session.id, "x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "READ_FAILED")
        self.assertNotIn("value", result)
        self.assertEqual(transport.command.await_count, 3)
        self.assertTrue(all(call.args == ("eval -- sizeof(void *)",)
                            for call in transport.command.await_args_list))
        self.assertFalse(session.mutation_lock.locked())

    async def test_native_encoding_failure_is_never_success_without_messages(self):
        manager, session, transport = self.setup_session([state()] * 4)
        transport.command.return_value = 'eval: { expr: "x" value: "" type: "FString" msgs: "" value_encoding_valid: 0 invalid_encoding: 1 }'
        with patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await manager.evaluate(session.id, "x")
        self.assertFalse(result["ok"])
        self.assertTrue(result["invalid_encoding"])


if __name__ == "__main__":
    unittest.main()
