"""RAD's framed TCP transport and the Metadesk subset used by its IPC replies."""

from __future__ import annotations

import asyncio
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import anyio

from .errors import RadError

HEADER = struct.Struct("<IQ")
MAGIC = 0x72726300
MAX_REPLY = 4 * 1024 * 1024 - 64
MAX_REQUEST = 3500  # Upstream's Win32 receive buffer is 4096 bytes.


def command_text(value: str, label: str = "argument") -> str:
    if not value or any(ord(c) < 32 or c == ";" for c in value):
        raise RadError("INVALID_ARGUMENT", f"{label} must be nonempty and contain no control characters or semicolons.")
    return value


def decode_quoted(text: str) -> str:
    """Decode RAD's C-style escaping without damaging Unicode or unknown escapes."""
    escapes = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v", '"': '"', "'": "'", "\\": "\\"}
    out: list[str] = []
    i = 1
    while i < len(text) - 1:
        c = text[i]
        i += 1
        if c == "\\" and i < len(text) - 1:
            c = text[i]
            i += 1
            if c == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[i:i + 2]):
                out.append(chr(int(text[i:i + 2], 16)))
                i += 2
            else:
                out.append(escapes.get(c, "\\" + c))
        else:
            out.append(c)
    return "".join(out)


_TOKENS = re.compile(r'\s+|"(?:\\.|[^"\\])*"|\[[^\n\]\)]*[\]\)]|[{}:,]|[^\s{}:,]+')


def _atom(text: str) -> Any:
    if text.startswith('"'):
        return decode_quoted(text)
    if re.fullmatch(r"-?(?:0x[0-9a-fA-F]+|[0-9]+)", text):
        return int(text, 16 if "0x" in text else 10)
    if text.startswith("["):
        a, b = text[1:-1].split(",", 1)
        return {"min": _atom(a.strip()), "max": _atom(b.strip()), "max_exclusive": text.endswith(")")}
    return text


def parse_reply(text: str) -> dict[str, Any]:
    tokens = [m.group() for m in _TOKENS.finditer(text) if not m.group().isspace()]
    pos = 0

    def nodes(close: bool = False, depth: int = 0) -> Any:
        nonlocal pos
        if depth > 24:
            raise RadError("BAD_REPLY", "Metadesk reply exceeds the nesting limit.")
        named: dict[str, Any] = {}
        items: list[Any] = []
        while pos < len(tokens):
            token = tokens[pos]
            pos += 1
            if token == "}":
                if not close:
                    raise RadError("BAD_REPLY", "Unexpected closing brace.")
                return items if items else named
            if token == ",":
                continue
            if token == "{":
                items.append(nodes(True, depth + 1))
                continue
            if pos < len(tokens) and tokens[pos] == ":":
                pos += 1
                if pos >= len(tokens):
                    raise RadError("BAD_REPLY", "Missing field value.")
                value = tokens[pos]
                pos += 1
                key = str(_atom(token))
                if key in named:
                    raise RadError("BAD_REPLY", f"Duplicate field: {key}")
                named[key] = nodes(True, depth + 1) if value == "{" else _atom(value)
            else:
                items.append(_atom(token))
        if close:
            raise RadError("BAD_REPLY", "Truncated Metadesk reply.")
        if items:
            raise RadError("BAD_REPLY", "Expected a named reply object.")
        return named

    try:
        result = nodes()
    except (ValueError, IndexError) as exc:
        raise RadError("BAD_REPLY", "Malformed debugger reply.") from exc
    if not isinstance(result, dict):
        raise RadError("BAD_REPLY", "Expected an object reply.")
    return result


@dataclass
class RadTransport:
    port: int
    token: str = field(repr=False)
    timeout: float = 5.0
    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        writer = self._writer
        self._reader = self._writer = None
        if writer:
            writer.close()
            with anyio.CancelScope(shield=True), anyio.move_on_after(1):
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def command(self, command: str, timeout: float | None = None) -> str:
        # Internal batches are never exposed as a raw-command tool.
        payload = (self.token + "\n" + command).encode("utf-8")
        if len(payload) > MAX_REQUEST:
            raise RadError("REQUEST_TOO_LARGE", f"RAD IPC requests are limited to {MAX_REQUEST} bytes.")
        started = time.monotonic()
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout if timeout is None else timeout):
                    if self._writer is None:
                        self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self.port)
                    self._writer.write(HEADER.pack(MAGIC, len(payload)) + payload)
                    await self._writer.drain()
                    header = await self._reader.readexactly(HEADER.size)
                    magic, size = HEADER.unpack(header)
                    if magic != MAGIC or size > MAX_REPLY:
                        raise RadError("BAD_FRAME", "Invalid or oversized RAD IPC response header.")
                    reply = await self._reader.readexactly(size)
                    return reply.decode("utf-8").replace("\0", "")
            except asyncio.CancelledError:
                await self.close()
                raise
            except (OSError, asyncio.IncompleteReadError, TimeoutError, UnicodeDecodeError, RadError) as exc:
                await self.close()
                if isinstance(exc, RadError):
                    raise
                # Do not automatically resend a mutation after an uncertain response.
                raise RadError("IPC_FAILED", f"RAD communication failed after {time.monotonic() - started:.2f}s; inspect state before retrying the operation ({type(exc).__name__}).") from exc
