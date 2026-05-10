import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger("bus")

# IRC flood protection: one outbound PRIVMSG per second maximum.
_PUBLISH_INTERVAL = 1.0

BusPublish = Callable[[str], Awaitable[None]]


class BusClient:
    """Asyncio IRC client for the CODA event bus.

    Call publish(message) to enqueue an outbound event — fire and forget.
    Incoming messages are delivered to the on_event callback if provided.
    Maintains its own reconnect loop; safe to run as a long-lived task.
    """

    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        channel: str,
        on_event: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._nick = nick
        self._channel = channel
        self._on_event = on_event
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def publish(self, message: str) -> None:
        """Enqueue a message for delivery to the bus channel."""
        await self._queue.put(message)

    async def run(self) -> None:
        while True:
            try:
                await self._connect_and_run()
            except Exception as e:
                log.error("Bus connection lost: %s — reconnecting in 10s", e)
            await asyncio.sleep(10)

    async def _connect_and_run(self) -> None:
        log.info("Connecting to bus at %s:%d as %s", self._host, self._port, self._nick)
        reader, writer = await asyncio.open_connection(self._host, self._port)

        async def send(msg: str) -> None:
            writer.write(f"{msg}\r\n".encode())
            await writer.drain()

        try:
            await send(f"NICK {self._nick}")
            await send(f"USER {self._nick} 0 * :sig.1852 bus client")
            await asyncio.sleep(2)
            await send(f"JOIN {self._channel}")
            log.info("Bus connected, joined %s", self._channel)

            await asyncio.gather(
                self._reader_loop(reader, send),
                self._writer_loop(send),
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _reader_loop(
        self,
        reader: asyncio.StreamReader,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        buf = ""
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=300)
            except asyncio.TimeoutError:
                await send("PING :keepalive")
                continue
            if not data:
                raise ConnectionError("Server closed connection")
            buf += data.decode(errors="replace")
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                if line.startswith("PING"):
                    await send("PONG " + line[5:])
                elif "PRIVMSG" in line:
                    await self._handle_privmsg(line)

    async def _writer_loop(self, send: Callable[[str], Awaitable[None]]) -> None:
        while True:
            message = await self._queue.get()
            await send(f"PRIVMSG {self._channel} :{message}")
            # Rate limit: give the IRC server a breath between messages.
            await asyncio.sleep(_PUBLISH_INTERVAL)

    async def _handle_privmsg(self, line: str) -> None:
        try:
            prefix, rest = line[1:].split(" PRIVMSG ", 1)
            nick = prefix.split("!")[0]
            if nick.lower() == self._nick.lower():
                return
            channel, message = rest.split(" :", 1)
            channel = channel.strip()
        except ValueError:
            return

        # Never react to [CODA] tagged messages — prevents feedback loops.
        if message.startswith("[CODA]"):
            return

        if self._on_event:
            try:
                await self._on_event(channel, message)
            except Exception:
                log.exception("Error in bus event handler")
