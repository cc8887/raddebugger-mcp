"""Runtime reflection shares the guarded, read-only stopped-session contract."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_structured_sessions as structured
from test_structured_sessions import state

from raddebugger_mcp.errors import RadError


class ReflectionSessionTests(unittest.IsolatedAsyncioTestCase):
    setup_session = structured.StructuredSessionTests.setup_session

    async def test_running_stale_and_injection_never_probe(self):
        for initial, expression, options, code in (
            (state(running=True), "actor", {}, "TARGET_RUNNING"),
            (state(), "actor", dict(expected_stop_count=8), "STALE_SNAPSHOT"),
            (state(), "actor;kill_all", {}, "INVALID_ARGUMENT"),
        ):
            manager, session, transport = self.setup_session([initial])
            with self.assertRaisesRegex(RadError, code):
                await manager.read_object(session.id, expression, **options)
            transport.command.assert_not_awaited()

    async def test_original_epoch_lock_and_freshness_cover_every_probe(self):
        manager, session, transport = self.setup_session([state()] * 3)

        class Reader:
            def __init__(self, probe, **kw):
                self.probe = probe

            async def read_object(self, expression, **kw):
                if not session.mutation_lock.locked():
                    raise AssertionError("Lock not held")
                return dict(ok=True, value=(await self.probe(expression))["value"])

        with patch("raddebugger_mcp.sessions.ReflectionReader", Reader):
            r = await manager.read_object(
                session.id, "actor", expected_run_gen=7, expected_stop_count=9
            )
        self.assertEqual(r["value"], "12")
        self.assertEqual(r["process_id"], 101)
        self.assertEqual(r["memory_freshness"], "direct_process_read")
        self.assertFalse(session.mutation_lock.locked())

    async def test_changed_process_or_final_context_rejects_result(self):
        for states in (
            [state(), state(selected_process_id=102)],
            [state(), state(), state(selected_unwind_count=1)],
        ):
            manager, session, _ = self.setup_session(states)

            class Reader:
                def __init__(self, probe, **kw):
                    self.probe = probe

                async def read_object(self, expression, **kw):
                    await self.probe(expression)
                    return dict(ok=True)

            with (
                patch("raddebugger_mcp.sessions.ReflectionReader", Reader),
                self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"),
            ):
                await manager.read_object(session.id, "actor")
            self.assertFalse(session.mutation_lock.locked())

    async def test_cached_backend_rejected(self):
        manager, session, transport = self.setup_session([state()] * 3)
        transport.command.return_value = 'eval: {expr: "sizeof(void *)" value: "8" type: "uint64" msgs: "" memory_freshness: "backend_cached"}'
        with self.assertRaisesRegex(RadError, "UNSUPPORTED_BACKEND"):
            await manager.read_object(session.id, "actor")
        self.assertFalse(session.mutation_lock.locked())

    async def test_cancellation_releases_lock_and_preserves_target(self):
        manager, session, transport = self.setup_session([state()])
        reader = MagicMock()
        reader.read_object = AsyncMock(side_effect=asyncio.CancelledError)
        with (
            patch("raddebugger_mcp.sessions.ReflectionReader", return_value=reader),
            self.assertRaises(asyncio.CancelledError),
        ):
            await manager.read_object(session.id, "actor")
        self.assertFalse(session.mutation_lock.locked())
        self.assertFalse(session.closed)
        transport.command.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
