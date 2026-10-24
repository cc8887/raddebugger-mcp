"""Semantic FText evaluate contracts; mocks only, never launches a debugger."""

import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.sessions import DebugSession, SessionManager


PROBE = "sizeof(void *)"


def snapshot(**changes):
    state = {"running": False, "run_gen": 7, "stop_count": 9, "selected_process_id": 101,
             "selected_thread_id": 201, "selected_unwind_count": 2, "selected_inline_depth": 1,
             "captured_at": "2026-10-24T00:00:00+00:00", "processes": [{"id": 101, "is_dump": 0}],
             "threads": [{"id": 201}], "modules": [], "locals": [], "lines": [], "stack": [],
             "targets": [], "breakpoints": []}
    state.update(changes)
    return copy.deepcopy(state)


def wire_eval(expression="T", *, value="native generic storage", type_name="FText", **changes):
    data = {"expr": expression, "value": value, "type": type_name, "msgs": "",
            "memory_freshness": "direct_process_read", "read_failed": 0,
            "value_encoding_valid": 1, "type_encoding_valid": 1, "invalid_encoding": 0,
            "value_truncated": 0, "type_truncated": 0, "interpretation_code": 0,
            "type_interpretation_code": 0, "display_limit_bytes": 512}
    data.update(changes)
    return "eval: { " + " ".join(
        f"{key}: {json.dumps(item, ensure_ascii=False) if isinstance(item, str) else item}"
        for key, item in data.items()) + " }"


def semantic(value="限帧 🎮🚀", **changes):
    units = len(value.encode("utf-16-le")) // 2
    result = {"ok": True, "kind": "ftext", "type": "FText", "value": value,
              "layout": "ue4_26", "complete": True, "truncated": False,
              "code_units": units, "decoded_units": units, "next_index": None,
              "storage": "localized_string", "probes": 12}
    result.update(changes)
    return result


class FTextEvaluateTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, *, native=None, probe=None, state_hook=None):
        transport = MagicMock()
        transport.close = AsyncMock()
        transport.command = AsyncMock()
        process = MagicMock()
        process.poll.return_value = None
        session = DebugSession("session-text", "mock text", Path("unused-session"),
                               Path("unused-backend.exe"), process, transport)
        manager = SessionManager(Path("unused-backend.exe"), Path("unused-root"))
        manager.sessions[session.id] = session
        states, reads = [], []

        async def state():
            self.assertTrue(session.mutation_lock.locked(), "State probes must remain under the original mutation lock")
            value = snapshot()
            if state_hook:
                value = state_hook(len(states) + 1, value)
            states.append(value)
            return value

        async def command(text):
            self.assertTrue(session.mutation_lock.locked(), "Direct eval probes escaped mutation lock")
            reads.append(text)
            if text == "eval -- " + PROBE:
                return probe if probe is not None else wire_eval(PROBE, value="8", type_name="uint64")
            return native if native is not None else wire_eval()

        session.state = AsyncMock(side_effect=state)
        transport.command.side_effect = command
        return manager, session, transport, states, reads

    def reader_factory(self, session, result, observations, *, after_probe=None):
        test = self

        class Reader:
            def __init__(self, callback, *, layout):
                test.assertEqual(layout, "ue4_26")
                self.callback = callback

            async def read(self, expression, **options):
                test.assertTrue(session.mutation_lock.locked())
                test.assertEqual(options, {"kind": "ftext"})
                # This invokes the real private _evaluate_stopped method, not
                # manager.evaluate/read_value; a reentrant lock would deadlock.
                direct = await self.callback(PROBE)
                test.assertEqual(direct["expression"], PROBE)
                test.assertEqual(direct["run_gen"], 7)
                test.assertEqual(direct["stop_count"], 9)
                observations.append({"expression": expression, "probe": direct})
                if after_probe:
                    after_probe()
                return copy.deepcopy(result)

        return Reader

    async def test_same_lock_original_epoch_direct_callback_without_public_recursion(self):
        manager, session, transport, states, reads = self.make_session()
        private_evaluate = manager._evaluate_stopped
        original_contexts, observations = [], []

        async def tracked_private(current_session, expression, before):
            self.assertIs(current_session, session)
            self.assertTrue(session.mutation_lock.locked())
            original_contexts.append(before)
            return await private_evaluate(current_session, expression, before)

        public_evaluate = manager.evaluate
        with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(), observations)), \
                patch.object(manager, "_evaluate_stopped", new=AsyncMock(side_effect=tracked_private)), \
                patch.object(manager, "evaluate", new=AsyncMock(wraps=public_evaluate)) as public, \
                patch.object(manager, "read_value", new=AsyncMock(side_effect=AssertionError("No public read reentry"))):
            result = await manager.evaluate(session.id, "T", expected_run_gen=7, expected_stop_count=9)
        self.assertTrue(result["ok"])
        self.assertEqual(public.await_count, 1)
        self.assertEqual(len(original_contexts), 2)
        self.assertTrue(all(before is states[0] for before in original_contexts))
        self.assertEqual(reads, ["eval -- T", "eval -- " + PROBE])
        self.assertEqual(len(states), 4)  # before, native after, probe after, final semantic after
        self.assertEqual(len(observations), 1)
        self.assertFalse(session.mutation_lock.locked())

    async def test_native_encoding_diagnostics_preserved_but_exact_semantic_unicode_used(self):
        native = wire_eval(value="binary ObjectStorage diagnostic", msgs="Invalid UTF-8 in generic storage",
                           invalid_encoding=1, value_encoding_valid=0)
        manager, session, _, _, reads = self.make_session(native=native)
        text, observations = "限帧：播放 🎮🚀 / A\0中", []
        with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(text), observations)), \
                patch("raddebugger_mcp.sessions.asyncio.sleep", new=AsyncMock()):
            result = await manager.evaluate(session.id, "T")
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["value"]), text)
        self.assertEqual(result["representation"], "ue4_26_ftext_display_string")
        self.assertEqual(result["messages"], "")
        self.assertTrue(result["value_encoding_valid"])
        self.assertFalse(result["invalid_encoding"])
        self.assertFalse(result["read_failed"])
        diagnostic = result["native_display_diagnostics"]
        self.assertFalse(diagnostic["ok"])
        self.assertTrue(diagnostic["invalid_encoding"])
        self.assertFalse(diagnostic["value_encoding_valid"])
        self.assertEqual(diagnostic["value"], "binary ObjectStorage diagnostic")
        self.assertEqual(diagnostic["messages"], "Invalid UTF-8 in generic storage")
        self.assertNotIn("value", result["semantic_read"], "No uncapped second copy of semantic text")
        self.assertEqual(reads.count("eval -- T"), 3)
        self.assertEqual(reads.count("eval -- " + PROBE), 1)

    async def test_semantic_errors_never_fall_back_to_successful_native_display(self):
        for code in ("UNSUPPORTED_LAYOUT", "INVALID_UTF16", "READ_FAILED"):
            with self.subTest(code=code):
                manager, session, _, _, _ = self.make_session(native=wire_eval(value='"misleading native success"'))
                failed = {"ok": False, "kind": "ftext", "error": {"code": code, "message": "semantic failure"}}
                with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, failed, [])):
                    result = await manager.evaluate(session.id, "T")
                self.assertFalse(result["ok"])
                self.assertEqual(result["value"], "")
                self.assertEqual(result["error"]["code"], code)
                self.assertTrue(result["native_display_diagnostics"]["ok"])
                self.assertIn("semantic failure", result["messages"])
                self.assertEqual(result["read_failed"], code == "READ_FAILED")
                self.assertEqual(result["invalid_encoding"], code == "INVALID_UTF16")
                self.assertFalse(session.mutation_lock.locked())

    async def test_explicit_raw_and_typeof_in_outer_parentheses_remain_native(self):
        for expression in ("raw(T)", "typeof(T)", " ( ( raw(T) ) ) ", "(( typeof(T)))"):
            with self.subTest(expression=expression):
                manager, session, _, _, reads = self.make_session(native=wire_eval(value="native unchanged"))
                with patch("raddebugger_mcp.sessions.ValueReader", side_effect=AssertionError("Explicit native expression")):
                    result = await manager.evaluate(session.id, expression)
                self.assertEqual(result["value"], "native unchanged")
                self.assertNotIn("representation", result)
                self.assertEqual(reads, ["eval -- " + expression])
                self.assertFalse(session.mutation_lock.locked())

    async def test_ordinary_outer_parentheses_still_receive_semantic_text(self):
        manager, session, _, _, _ = self.make_session()
        with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(), [])):
            result = await manager.evaluate(session.id, "((T))")
        self.assertEqual(json.loads(result["value"]), "限帧 🎮🚀")

    async def test_json_quoted_exact_512_utf8_byte_boundary_is_not_truncated(self):
        for text in ("a" * 510, "中" * 170, "🎮" * 127 + "aa", '"' * 255, "\n" * 255):
            with self.subTest(text_prefix=repr(text[:8])):
                self.assertEqual(len(json.dumps(text, ensure_ascii=False).encode("utf-8")), 512)
                manager, session, _, _, _ = self.make_session()
                with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(text), [])):
                    result = await manager.evaluate(session.id, "T")
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["possibly_truncated"])
                self.assertEqual(result["messages"], "")
                self.assertEqual(len(result["value"].encode("utf-8")), 512)
                self.assertEqual(json.loads(result["value"]), text)

    async def test_over_512_utf8_bytes_are_codepoint_safe_and_never_reported_complete(self):
        for text in ("a" * 511, "中" * 171, "🎮" * 128, '"' * 256):
            with self.subTest(text_prefix=repr(text[:8])):
                manager, session, _, _, _ = self.make_session()
                with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(text), [])):
                    result = await manager.evaluate(session.id, "T")
                self.assertFalse(result["ok"])
                self.assertTrue(result["possibly_truncated"])
                self.assertIn("truncated", result["messages"])
                encoded = result["value"].encode("utf-8", errors="strict")
                self.assertLessEqual(len(encoded), 512)
                self.assertEqual(encoded.decode("utf-8", errors="strict"), result["value"])
                self.assertTrue(result["value"].endswith("..."))
                self.assertNotIn("value", result["semantic_read"])

    async def test_incomplete_source_budget_cannot_be_success_even_if_display_fits(self):
        for truncated in (True, False):
            with self.subTest(truncated=truncated):
                manager, session, _, _, _ = self.make_session()
                partial = semantic("前缀", complete=False, truncated=truncated, next_index=2, code_units=100)
                with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, partial, [])):
                    result = await manager.evaluate(session.id, "T")
                self.assertFalse(result["ok"], "Incomplete semantic source must not become successful eval")
                self.assertTrue(result["possibly_truncated"])
                self.assertIn("truncated", result["messages"])
                self.assertFalse(result["semantic_read"]["complete"])
                self.assertEqual(json.loads(result["value"]), "前缀")

    async def test_final_snapshot_change_rejected_and_mutation_lock_released(self):
        for change in ({"stop_count": 10}, {"run_gen": 8}, {"running": True}, {"selected_process_id": 202},
                       {"selected_thread_id": 202}, {"selected_unwind_count": 3}, {"selected_inline_depth": 2}):
            with self.subTest(change=change):
                def state_hook(index, value):
                    return {**value, **change} if index == 4 else value
                manager, session, _, _, reads = self.make_session(state_hook=state_hook)
                with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(), [])):
                    with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
                        await manager.evaluate(session.id, "T")
                self.assertEqual(reads, ["eval -- T", "eval -- " + PROBE])
                self.assertFalse(session.mutation_lock.locked())

    async def test_callback_stale_snapshot_stops_before_final_read_and_releases_lock(self):
        def state_hook(index, value):
            return {**value, "stop_count": 10} if index == 3 else value
        manager, session, _, states, reads = self.make_session(state_hook=state_hook)
        with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(), [])):
            with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
                await manager.evaluate(session.id, "T")
        self.assertEqual(len(states), 3)
        self.assertEqual(len(reads), 2)
        self.assertFalse(session.mutation_lock.locked())

    async def test_cancel_at_semantic_read_or_final_state_releases_lock(self):
        for final_state in (False, True):
            with self.subTest(final_state=final_state):
                def state_hook(index, value):
                    if final_state and index == 4:
                        raise asyncio.CancelledError()
                    return value
                def cancelled():
                    raise asyncio.CancelledError()
                manager, session, _, _, reads = self.make_session(state_hook=state_hook)
                factory = self.reader_factory(session, semantic(), [], after_probe=None if final_state else cancelled)
                with patch("raddebugger_mcp.sessions.ValueReader", factory):
                    with self.assertRaises(asyncio.CancelledError):
                        await manager.evaluate(session.id, "T")
                self.assertEqual(len(reads), 2)
                self.assertFalse(session.mutation_lock.locked())

    async def test_non_direct_semantic_probe_is_rejected_not_cached_fallback(self):
        probe = wire_eval(PROBE, value="8", type_name="uint64", memory_freshness="backend_cached")
        manager, session, _, states, reads = self.make_session(probe=probe)
        with patch("raddebugger_mcp.sessions.ValueReader", self.reader_factory(session, semantic(), [])):
            with self.assertRaisesRegex(RadError, "UNSUPPORTED_BACKEND"):
                await manager.evaluate(session.id, "T")
        self.assertEqual(len(states), 3)
        self.assertEqual(reads, ["eval -- T", "eval -- " + PROBE])
        self.assertFalse(session.mutation_lock.locked())


if __name__ == "__main__":
    unittest.main()
