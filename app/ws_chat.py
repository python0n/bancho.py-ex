from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, List

from app.logging import Ansi, log

try:
    from fastapi import WebSocket
except Exception:  # pragma: no cover
    WebSocket = Any  # type: ignore[misc,assignment]


@dataclass
class WSConn:
    id: str
    ws: WebSocket
    user_id: int
    username: str
    channels: Set[str] = field(default_factory=set)
    owns_session: bool = False


class ChatWSHub:
    """Hub for mirroring bancho chat events to WebSocket clients.

    This file is intentionally small + conservative:
    - async connect/disconnect/join/part (called by API gateway)
    - fire-and-forget publish_* methods (called from sync bancho codepaths)
    """

    def __init__(self) -> None:
        self._conns: Dict[str, WSConn] = {}
        self._by_user: Dict[int, Set[str]] = {}
        self._lock = asyncio.Lock()

    # -------- connection management --------

    async def connect(self, ws: WebSocket, user_id: int, username: str, owns_session: bool) -> WSConn:
        conn = WSConn(
            id=str(uuid.uuid4()),
            ws=ws,
            user_id=user_id,
            username=username,
            owns_session=owns_session,
        )
        async with self._lock:
            self._conns[conn.id] = conn
            self._by_user.setdefault(user_id, set()).add(conn.id)

        log(f"[WSCHAT] connected {username} ({user_id})", Ansi.LCYAN)
        return conn

    async def disconnect(self, conn_id: str) -> None:
        async with self._lock:
            conn = self._conns.pop(conn_id, None)
            if not conn:
                return
            ids = self._by_user.get(conn.user_id)
            if ids:
                ids.discard(conn_id)
                if not ids:
                    self._by_user.pop(conn.user_id, None)

        log(f"[WSCHAT] disconnected {conn.username} ({conn.user_id})", Ansi.LYELLOW)

    async def join(self, conn_id: str, chan: str) -> None:
        # NOTE: chat.py expects this method to exist & not crash.
        chan = self._norm_chan(chan)
        if not chan:
            return
        async with self._lock:
            conn = self._conns.get(conn_id)
            if conn:
                conn.channels.add(chan)

    async def part(self, conn_id: str, chan: str) -> None:
        chan = self._norm_chan(chan)
        if not chan:
            return
        async with self._lock:
            conn = self._conns.get(conn_id)
            if conn:
                conn.channels.discard(chan)

    # -------- optional helpers used by some WS gateways --------

    def user_conn_count(self, user_id: int) -> int:
        ids = self._by_user.get(user_id)
        return len(ids) if ids else 0

    def user_total(self) -> int:
        return len(self._by_user)

    def user_list(self) -> List[dict[str, Any]]:
        # Best-effort snapshot (no lock); fine for UI counters.
        out: List[dict[str, Any]] = []
        seen: Set[int] = set()
        for cid, conn in self._conns.items():
            if conn.user_id in seen:
                continue
            seen.add(conn.user_id)
            out.append({"id": conn.user_id, "name": conn.username})
        out.sort(key=lambda x: str(x.get("name", "")).lower())
        return out

    # -------- publish API (safe from sync callsites) --------

    def publish_channel(self, chan: str, sender: str, sender_id: int, text: str) -> None:
        payload = {
            "t": "chan_msg",
            "chan": self._norm_chan(chan),
            "from": sender,
            "from_id": sender_id,
            "text": text,
            "ts": int(time.time()),
        }
        self._spawn(self._broadcast_channel(payload["chan"], payload))

    def publish_dm(self, to_id: int, to_name: str, sender: str, sender_id: int, text: str) -> None:
        payload = {
            "t": "dm",
            "from": sender,
            "from_id": sender_id,
            "to": to_name,
            "to_id": to_id,
            "text": text,
            "ts": int(time.time()),
        }
        self._spawn(self._send_to_user(to_id, payload))

    def publish_dm_echo(self, to_id: int, to_name: str, sender: str, sender_id: int, text: str) -> None:
        payload = {
            "t": "dm",
            "from": sender,
            "from_id": sender_id,
            "to": to_name,
            "to_id": to_id,
            "text": text,
            "ts": int(time.time()),
        }
        self._spawn(self._send_to_user(sender_id, payload))

    # -------- internal helpers --------

    def _spawn(self, coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(coro)

    async def _broadcast_channel(self, chan: str, payload: dict[str, Any]) -> None:
        conns: list[WSConn] = []
        async with self._lock:
            for conn in self._conns.values():
                if chan in conn.channels:
                    conns.append(conn)

        await self._fanout(conns, payload)

    async def _send_to_user(self, user_id: int, payload: dict[str, Any]) -> None:
        conns: list[WSConn] = []
        async with self._lock:
            ids = self._by_user.get(user_id, set())
            for cid in ids:
                c = self._conns.get(cid)
                if c:
                    conns.append(c)

        await self._fanout(conns, payload)

    async def _fanout(self, conns: list[WSConn], payload: dict[str, Any]) -> None:
        if not conns:
            return

        dead: list[str] = []
        for conn in conns:
            try:
                await conn.ws.send_json(payload)
            except Exception:
                dead.append(conn.id)

        for cid in dead:
            await self.disconnect(cid)

    @staticmethod
    def _norm_chan(chan: str) -> str:
        """Normalize channel names (the missing method that caused your crash)."""
        c = (chan or "").strip()
        if not c:
            return c
        return c if c.startswith("#") else "#" + c


hub = ChatWSHub()
