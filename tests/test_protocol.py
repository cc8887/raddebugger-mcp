import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from raddebugger_mcp.errors import RadError
from raddebugger_mcp.protocol import HEADER, MAGIC, MAX_REPLY, RadTransport, command_text, parse_reply
from raddebugger_mcp.sessions import check_epoch


class ParserTests(unittest.TestCase):
    def test_real_state_shape(self):
        result = parse_reply('state: { running: 0 run_gen: 2 stop_count: 3 ip: 0x140001000 locals: { input local_value } threads: {{ name: "主线程" id: 8 }} range: [0x10, 0x20) }')
        state = result["state"]
        self.assertEqual(state["ip"], 0x140001000)
        self.assertEqual(state["locals"], ["input", "local_value"])
        self.assertEqual(state["threads"][0]["name"], "主线程")
        self.assertTrue(state["range"]["max_exclusive"])

    def test_eval_escaping(self):
        result = parse_reply(r'eval: { expr: "x[0]" value: "a\n\"b\"\\c" type: "char*" msgs: "" }')
        self.assertEqual(result["eval"]["value"], 'a\n"b"\\c')

    def test_empty_collections(self):
        self.assertEqual(parse_reply("state: { locals: {} }"), {"state": {"locals": {}}})

    def test_truncated_or_duplicate(self):
        for reply in ("state: { running:", "state: { running: 0", "state: { a: 1 a: 2 }", "}"):
            with self.subTest(reply=reply), self.assertRaises(RadError):
                parse_reply(reply)

    def test_command_injection(self):
        for text in ("", "x; kill_all", "x\ncontinue", "x\0", "x\t"):
            with self.subTest(text=text), self.assertRaises(RadError):
                command_text(text)
        self.assertEqual(command_text("F:/path with spaces/main.cpp:10"), "F:/path with spaces/main.cpp:10")

    def test_stale_epoch(self):
        check_epoch({"run_gen": 2, "stop_count": 4}, 2, 4)
        with self.assertRaisesRegex(RadError, "STALE_SNAPSHOT"):
            check_epoch({"run_gen": 2, "stop_count": 4}, 2, 3)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_response_and_authenticated_request(self):
        reader = asyncio.StreamReader()
        writer = AsyncMock()
        writer.write = lambda data: sent.append(data)
        writer.close = lambda: None
        sent = []
        frame = HEADER.pack(MAGIC, 5) + b"hello"

        async def feed():
            for byte in frame:
                reader.feed_data(bytes([byte]))
                await asyncio.sleep(0)

        transport = RadTransport(1234, "a" * 64)
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            feeder = asyncio.create_task(feed())
            self.assertEqual(await transport.command("state"), "hello")
            await feeder
            magic, size = HEADER.unpack(sent[0][:HEADER.size])
            self.assertEqual(magic, MAGIC)
            self.assertEqual(sent[0][HEADER.size:], b"a" * 64 + b"\nstate")
            self.assertEqual(size, 70)
        await transport.close()

    async def test_truncated_frame_fails_without_replay(self):
        reader = asyncio.StreamReader()
        reader.feed_data(HEADER.pack(MAGIC, 100) + b"partial")
        reader.feed_eof()
        writer = AsyncMock()
        writer.write = lambda data: None
        writer.close = lambda: None
        connector = AsyncMock(return_value=(reader, writer))
        with patch("asyncio.open_connection", new=connector):
            with self.assertRaisesRegex(RadError, "IPC_FAILED"):
                await RadTransport(1234, "b" * 64).command("continue")
        self.assertEqual(connector.await_count, 1)

    async def test_oversized_frame(self):
        reader = asyncio.StreamReader()
        reader.feed_data(HEADER.pack(MAGIC, MAX_REPLY + 1))
        writer = AsyncMock()
        writer.write = lambda data: None
        writer.close = lambda: None
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaisesRegex(RadError, "BAD_FRAME"):
                await RadTransport(1234, "b" * 64).command("state")

    async def test_oversized_request(self):
        with self.assertRaisesRegex(RadError, "REQUEST_TOO_LARGE"):
            await RadTransport(1234, "b" * 64).command("eval " + "x" * 4000)


if __name__ == "__main__":
    unittest.main()
