"""Website chat (Shiina /chat): the IRC protocol over a WebSocket.

The browser sends and receives ordinary IRC lines, one per WebSocket text
message. On the server side the connection is a normal IRC client of our IRC
server, so channels, private messages, mail for offline players, silences,
blocks, BanchoBot and !mp commands all work exactly like on IRC.

Logging in: Shiina stores a one-time ticket in Redis
(chat:ticket:<ticket> = user id, valid for 60 seconds) and the browser opens
/ws/chat?ticket=<ticket>. The IRC key never reaches the browser.
"""

from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

import app.state
from app.logging import Ansi
from app.logging import log

router = APIRouter()

TICKET_PREFIX = "chat:ticket:"
_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
MAX_LINE = 4096
# flood protection: at most this many lines per window, otherwise they are dropped
FLOOD_LINES = 20
FLOOD_WINDOW = 10.0


class WebSocketWriter:
    """The small part of asyncio.StreamWriter that IRCClient uses."""

    def __init__(self, ip: str) -> None:
        self._ip = ip
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        for line in data.decode("utf-8", errors="replace").split("\r\n"):
            if line:
                self.lines.put_nowait(line)

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "peername":
            return (self._ip, 0)
        return default

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.lines.put_nowait(None)

    def is_closing(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        return None


def _client_ip(ws: WebSocket) -> str:
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        value = ws.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return ws.client.host if ws.client else "127.0.0.1"


async def _take_ticket(ticket: str) -> int | None:
    if not _TICKET_RE.match(ticket or ""):
        return None
    redis = app.state.services.redis
    key = TICKET_PREFIX + ticket
    # GETDEL: a ticket can be used only once, even by two connections at the same moment
    try:
        value = await redis.execute_command("GETDEL", key)
    except Exception:
        pipe = redis.pipeline(transaction=True)
        pipe.get(key)
        pipe.delete(key)
        value, _ = await pipe.execute()
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    from app.api.ircserver.ircserver import NAME
    from app.api.ircserver.ircserver import BanchoIRCException
    from app.api.ircserver.ircserver import IRCClient

    await ws.accept()

    irc = getattr(app.state.services, "irc", None)
    if irc is None:
        await ws.send_text(f":{NAME} ERROR :Chat is turned off on this server")
        await ws.close(code=1011)
        return

    user_id = await _take_ticket(ws.query_params.get("ticket", ""))
    if user_id is None:
        await ws.send_text(f":{NAME} ERROR :Invalid or expired ticket, reload the page")
        await ws.close(code=1008)
        return

    player = await app.state.sessions.players.get_sql(id=user_id)
    if player is None:
        await ws.send_text(f":{NAME} ERROR :Unknown player")
        await ws.close(code=1008)
        return

    ip = _client_ip(ws)
    writer = WebSocketWriter(ip)
    client = IRCClient(irc, writer)  # type: ignore[arg-type]
    client.ip = ip

    try:
        client.player = await client.login_player(player)
    except BanchoIRCException as e:
        await ws.send_text(f":{NAME} {e.code} {e.error}")
        await ws.close(code=1008)
        return

    client.web_chat = True  # type: ignore[attr-defined]
    irc.clients.add(client)
    log(f"[chat] {client.player} connected from the website ({ip})", Ansi.LCYAN)
    await client.send_welcome_msg()

    async def pump() -> None:
        while True:
            line = await writer.lines.get()
            if line is None:
                break
            await ws.send_text(line)

    pump_task = asyncio.create_task(pump())
    recent: list[float] = []
    warned = 0.0
    try:
        while not writer.is_closing():
            text = await ws.receive_text()
            if len(text) > MAX_LINE:
                text = text[:MAX_LINE]
            for line in text.replace("\r", "").split("\n")[:5]:
                if not line.strip():
                    continue
                now = time.monotonic()
                recent = [t for t in recent if now - t < FLOOD_WINDOW]
                if len(recent) >= FLOOD_LINES and not line.upper().startswith("PING"):
                    if now - warned > FLOOD_WINDOW:
                        warned = now
                        await ws.send_text(f":{NAME} NOTICE {client.player.name if client.player else '*'} :You are sending messages too fast, slow down.")
                    continue
                recent.append(now)
                if line.split(" ", 1)[0].upper() in ("PASS", "USER", "NICK", "CAP"):
                    continue  # already logged in with the ticket
                await client.data_received((line + "\r\n").encode())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log(f"[chat] connection error: {e!r}", Ansi.LRED)
    finally:
        irc.clients.discard(client)
        if client.player is not None:
            name = client.player.name
            irc.release(client)
            client.player = None
            log(f"[chat] {name} left the website chat", Ansi.LYELLOW)
        writer.close()
        try:
            await asyncio.wait_for(pump_task, timeout=2)
        except (asyncio.TimeoutError, Exception):
            pump_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
