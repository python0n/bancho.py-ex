from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app
import app.settings
import app.state
from app import commands
from app.logging import Ansi, log
from app.objects.channel import Channel
from app.objects.player import Player
from app.utils import make_safe_name
from app.ws_chat import hub

BOT_NICK = "BanchoBot"
SERVER_NAME = "banchobot"

router = APIRouter()

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

def _normalize_token(raw: str | None) -> str:
    """Best-effort normalize the token coming from query params.

    Users sometimes paste full URLs or prefixes like 'http://'. We try to extract a UUID if present.
    """
    if not raw:
        return ""
    s = raw.strip()
    # Extract a UUID if one exists anywhere in the string.
    m = _UUID_RE.search(s)
    if m:
        return m.group(0)
    # Otherwise just strip common URL scheme prefixes.
    if s.startswith(("http://", "https://")):
        s = s.split("://", 1)[1]
    return s



def _resolve_channel(name: str) -> Optional[Channel]:
    if not name:
        return None

    chan_name = name.strip()
    if not chan_name.startswith("#"):
        chan_name = "#" + chan_name

    chans = getattr(app.state.sessions, "channels", None)
    if chans is None:
        return None

    for candidate in (chan_name, chan_name.lstrip("#")):
        try:
            ch = chans.get_by_name(candidate)
            if ch is not None:
                return ch
        except Exception:
            continue

    return None


async def _ws_login(ws: WebSocket, token: str) -> tuple[Optional[Player], bool]:
    """Authenticate using the same token as IRC (irc_key). Returns (player, owns_session)."""
    if not token:
        return None, False

    try:
        p = await Player.from_irc(token)
    except Exception:
        # e.g. invalid token -> DB lookup returns None and Player.from_irc may raise
        return None, False
    if not p:
        return None, False

    if getattr(p, "restricted", False):
        return None, False

    players = app.state.sessions.players

    existing: Optional[Player] = None
    if hasattr(players, "get"):
        try:
            existing = players.get(p.id)
        except Exception:
            existing = None

    if existing is None:
        try:
            for x in players:
                if getattr(x, "id", None) == p.id:
                    existing = x
                    break
        except Exception:
            existing = None

    if existing is not None:
        p = existing
        owns = False
    else:
        owns = True
        await p.stats_from_sql_full()
        # Keep it simple: no geoloc; WS is not latency-critical.
        players.append(p)
        user_data = app.packets.user_presence(p)  # type: ignore[attr-defined]
        if user_data is not None:
            players.enqueue(user_data)

        # keep metrics consistent (logout() decrements)
        if not getattr(p, "restricted", False):
            try:
                if app.state.services.datadog:
                    app.state.services.datadog.increment("bancho.online_players")  # type: ignore[no-untyped-call]
            except Exception:
                pass
            try:
                if app.metrics.enabled:
                    app.metrics.increment("ex_online_players")
            except Exception:
                pass

    # mark as external chat client (helps future logic, harmless if unused)
    p.ws_client = True  # type: ignore[attr-defined]
    return p, owns


async def _handle_send(ws: WebSocket, player: Player, conn_id: str, target: str, message: str) -> None:
    target = (target or "").strip()
    message = (message or "").strip()
    if not target or not message:
        await ws.send_json({"t": "err", "code": "bad_request", "msg": "target/message required"})
        return

    to_l = target.lower()

    # BanchoBot routing (DM)
    if to_l in {BOT_NICK.lower(), SERVER_NAME.lower(), "banchobot"}:
        bot = getattr(app.state.sessions, "bot", None)
        if not bot:
            await ws.send_json({"t": "err", "code": "no_bot", "msg": "BanchoBot missing"})
            return

        if message.startswith(app.settings.COMMAND_PREFIX):
            try:
                old_ws_cmd = getattr(player, "_ws_cmd", False)
                setattr(player, "_ws_cmd", True)
                try:
                    cmd = await commands.process_commands(player, bot, message)
                finally:
                    try:
                        setattr(player, "_ws_cmd", old_ws_cmd)
                    except Exception:
                        pass
            except Exception as exc:
                log(f"[WSCHAT] command error: {exc}", Ansi.LRED)
                cmd = None

            if cmd and cmd.get("resp") is not None:
                # echo outgoing (osu does this for non-hidden commands)
                if not cmd.get("hidden", False):
                    await ws.send_json(
                        {
                            "t": "dm",
                            "from": player.name,
                            "from_id": player.id,
                            "to": BOT_NICK,
                            "to_id": bot.id,
                            "text": message,
                        },
                    )

                resp_text = str(cmd["resp"])
                # also deliver to in-game client (if connected)
                try:
                    player.send_bot(resp_text)
                except Exception:
                    pass

                for line in resp_text.split("\n"):
                    if line:
                        await ws.send_json(
                            {
                                "t": "dm",
                                "from": BOT_NICK,
                                "from_id": bot.id,
                                "to": player.name,
                                "to_id": player.id,
                                "text": line,
                            },
                        )
        # chat4osu UX: auto-join the match channel after `!mp make` in BanchoBot PM
        if message.lower().startswith(f"{app.settings.COMMAND_PREFIX}mp make"):
            try:
                chan_name = getattr(player, "_ws_last_match_chan", None)
                if not chan_name and getattr(player, "match", None):
                    chan_name = player.match.chat.real_name
                if chan_name:
                    await hub.join(conn_id, chan_name)
                    ch = _resolve_channel(chan_name)
                    if ch:
                        player.join_channel(ch)
                    await ws.send_json({"t": "joined", "chan": chan_name})
            except Exception:
                pass

        return

# Channel
    if target.startswith("#"):
        chan = _resolve_channel(target)
        if not chan:
            await ws.send_json({"t": "err", "code": "no_such_channel", "msg": target})
            return

        # Log (so WS-sent messages show up like in-game chat messages)
        try:
            log(f"{player} @ <{chan.name}>: {message}")
        except Exception:
            pass

        # Join in bancho core so the player can receive channel packets if needed.
        try:
            player.join_channel(chan)
        except Exception:
            pass

        cmd = None
        if message.startswith(app.settings.COMMAND_PREFIX):
            try:
                old_ws_cmd = getattr(player, "_ws_cmd", False)
                setattr(player, "_ws_cmd", True)
                try:
                    cmd = await commands.process_commands(player, chan, message)
                finally:
                    try:
                        setattr(player, "_ws_cmd", old_ws_cmd)
                    except Exception:
                        pass
            except Exception as exc:
                log(f"[WSCHAT] command error: {exc}", Ansi.LRED)
                cmd = None

        if cmd:
            if not cmd.get("hidden", False):
                # to_self=True so the in-game client also sees WS-originated messages
                chan.send(message, sender=player, to_self=True)
                if cmd.get("resp") is not None:
                    chan.send_bot(str(cmd["resp"]))
            else:
                # hidden: don't echo to channel; send response only to the sender
                if cmd.get("resp") is not None:
                    resp_text = str(cmd["resp"])
                    try:
                        player.send_bot(resp_text)
                    except Exception:
                        pass
                    for line in resp_text.split("\n"):
                        if line:
                            await ws.send_json(
                                {
                                    "t": "dm",
                                    "from": BOT_NICK,
                                    "from_id": getattr(app.state.sessions, "bot", None).id if getattr(app.state.sessions, "bot", None) else 0,
                                    "to": player.name,
                                    "to_id": player.id,
                                    "text": line,
                                },
                            )
        else:
            chan.send(message, sender=player, to_self=True)

        return

# Normal DM
    recipient = await app.state.sessions.players.from_cache_or_sql(name=make_safe_name(target))
    if not recipient:
        await ws.send_json({"t": "err", "code": "no_such_user", "msg": target})
        return

    try:
        recipient.send(message, sender=player, chan=None)
    except Exception:
        # fall back to raw enqueue (should rarely happen)
        try:
            recipient.enqueue(app.packets.send_message(player.name, message, recipient.name, player.id))  # type: ignore[attr-defined]
        except Exception:
            pass

    # Echo to sender WS so UI sees the message immediately
    hub.publish_dm_echo(recipient.id, recipient.name, player.name, player.id, message)


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    token = _normalize_token(ws.query_params.get("token"))

    # Always accept first so auth failures don't surface as 500 during handshake.
    await ws.accept()

    try:
        player, owns_session = await _ws_login(ws, token)
    except Exception:
        # Defensive: token parsing / DB lookup should never crash the WS handshake.
        await ws.send_json({"t": "err", "code": "auth_failed", "msg": "invalid token"})
        await ws.close(code=1008)
        return

    if not player:
        await ws.send_json({"t": "err", "code": "auth_failed", "msg": "invalid token"})
        await ws.close(code=1008)
        return

    conn = await hub.connect(ws, player.id, player.name, owns_session)

    # auto-subscribe + join #osu
    try:
        await hub.join(conn.id, "#osu")
        ch = _resolve_channel("#osu")
        if ch:
            try:
                player.join_channel(ch)
            except Exception:
                pass
    except Exception:
        pass

    await ws.send_json({"t": "hello", "user": {"id": player.id, "name": player.name}})

    # snapshot of currently connected WS users (for chat4osu sidebar)
    try:
        total = await hub.user_total()
        users = await hub.user_list()
        await ws.send_json({"t": "users", "total": total, "users": users})
    except Exception:
        pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_json({"t": "err", "code": "bad_json", "msg": "invalid json"})
                continue

            t = msg.get("t")

            if t == "ping":
                await ws.send_json({"t": "pong"})
                continue

            if t == "users":
                try:
                    total = await hub.user_total()
                    users = await hub.user_list()
                    await ws.send_json({"t": "users", "total": total, "users": users})
                except Exception:
                    await ws.send_json({"t": "users", "total": 0, "users": []})
                continue

            if t == "join":
                chan = str(msg.get("chan", "")).strip()
                if not chan:
                    await ws.send_json({"t": "err", "code": "bad_request", "msg": "chan required"})
                    continue

                await hub.join(conn.id, chan)
                ch = _resolve_channel(chan)
                if ch:
                    try:
                        player.join_channel(ch)
                    except Exception:
                        pass
                await ws.send_json({"t": "joined", "chan": chan if chan.startswith("#") else "#" + chan})
                continue

            if t == "part":
                chan = str(msg.get("chan", "")).strip()
                if not chan:
                    await ws.send_json({"t": "err", "code": "bad_request", "msg": "chan required"})
                    continue
                await hub.part(conn.id, chan)
                await ws.send_json({"t": "parted", "chan": chan if chan.startswith("#") else "#" + chan})
                continue

            if t == "send":
                target = str(msg.get("target", "")).strip()
                text = str(msg.get("text", "")).strip()
                await _handle_send(ws, player, conn.id, target, text)
                continue

            await ws.send_json({"t": "err", "code": "unknown_type", "msg": str(t)})

    except WebSocketDisconnect:
        pass
    finally:
        try:
            await hub.disconnect(conn.id)
        finally:
            # Update ws_client based on remaining connections.
            remaining = 0
            try:
                remaining = await hub.user_conn_count(player.id)
            except Exception:
                remaining = 0

            if remaining <= 0:
                try:
                    player.ws_client = False  # type: ignore[attr-defined]
                except Exception:
                    pass

                # If we deferred logout because WS was still connected (game client disconnected),
                # or if WS created this session, clean it up now.
                deferred = False
                try:
                    deferred = bool(getattr(player, "_deferred_logout", False))
                except Exception:
                    deferred = False

                if deferred or owns_session:
                    try:
                        player._deferred_logout = False
                    except Exception:
                        pass
                    try:
                        player.logout()
                    except Exception:
                        pass