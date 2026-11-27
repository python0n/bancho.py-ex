"""Redis pub/sub receivers for actions sent by Shiina (panel) to bancho.

Every receiver goes through _receiver(): a bad message (wrong JSON, missing key,
an exception in the action) is logged and skipped, and a dropped Redis
connection is retried, so one broken message no longer stops the receiver
until the next bancho restart."""
import asyncio

import orjson

import app
from app.logging import Ansi
from app.logging import log

from .utils import *  # noqa: F403

RETRY_DELAY = 5
# map statuses the panel may set: 0 pending, 2 ranked, 3 approved, 4 qualified, 5 loved
PANEL_STATUSES = {0, 2, 3, 4, 5}


async def start_pubsub_recievers():
    """Start pubsub recievers."""
    log("Starting pubsub recievers-ex...", Ansi.LGREEN)

    for channel, handler in RECEIVERS.items():
        asyncio.create_task(_receiver(channel, handler), name=f"pubsub:{channel}")


def _req_id(data: dict) -> str | None:
    req_id = data.get("req_id")
    if isinstance(req_id, str) and 8 <= len(req_id) <= 64 and req_id.isalnum():
        return req_id
    return None


async def _ack(data: dict, result: str) -> None:
    """AUD-12: when Shiina sent a request id, put the result where it waits for it,
    so the panel shows what really happened instead of always "success"."""
    req_id = _req_id(data)
    if req_id is None:
        return
    try:
        key = f"ex:ack:{req_id}"
        await app.state.services.redis.rpush(key, result[:200])
        await app.state.services.redis.expire(key, 60)
    except Exception as e:
        log(f"EX | could not send the result back: {e!r}", Ansi.LRED)


async def _receiver(channel: str, handler) -> None:
    while True:
        pubsub = app.state.services.redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            log(f"Subscribed to '{channel}' channel.", Ansi.LGREEN)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = None
                try:
                    data = orjson.loads(message["data"])
                    if not isinstance(data, dict):
                        raise ValueError("payload is not a JSON object")
                    result = await handler(data)
                    await _ack(data, result if isinstance(result, str) else "success")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"EX | {channel} | bad message skipped: {e!r}", Ansi.LRED)
                    await _ack(data if isinstance(data, dict) else {}, "error in bancho, see its log")
            log(f"'{channel}' subscription ended - resubscribing in {RETRY_DELAY}s", Ansi.LYELLOW)
        except asyncio.CancelledError:
            log(f"Channel {channel} receiver task cancelled.", Ansi.LYELLOW)
            raise
        except Exception as e:
            log(f"EX | {channel} | receiver error: {e!r} - retrying in {RETRY_DELAY}s", Ansi.LRED)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose() if hasattr(pubsub, "aclose") else await pubsub.close()
            except Exception:
                pass
        await asyncio.sleep(RETRY_DELAY)


async def handle_clan_change(data: dict) -> str | None:
    """Shiina changed someone's clan (join, leave, kick, create, disband):
    refresh the clan of online players so it's right without logging in again.
    Payload: {"ids": [user ids]} and/or {"clan_id": id} for every member of a clan."""
    from app.constants.privileges import ClanPrivileges

    ids = {int(i) for i in data.get("ids") or []}
    clan_id = data.get("clan_id")
    targets = [
        p for p in app.state.sessions.players
        if p.id in ids or (clan_id is not None and p.clan_id == int(clan_id))
    ]
    for p in targets:
        row = await app.state.services.database.fetch_one(
            "SELECT clan_id, clan_priv FROM users WHERE id = :id",
            {"id": p.id},
        )
        if row is None:
            continue
        if row["clan_id"]:
            p.clan_id = row["clan_id"]
            p.clan_priv = ClanPrivileges(row["clan_priv"])
        else:
            p.clan_id = None
            p.clan_priv = None
    log(f"EX | Clan Change | refreshed {len(targets)} online player(s)", Ansi.LBLUE)


async def handle_name_change(data: dict) -> str | None:
    id, name = int(data["id"]), str(data["name"])
    log(f"EX | Name Change | ID: {id}, Name: {name}", Ansi.LBLUE)
    result = await change_user_name(id, name)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_country_change(data: dict) -> str | None:
    id, country = int(data["id"]), str(data["country"])
    log(f"EX | Country Change | ID: {id}, Country: {country}", Ansi.LBLUE)
    result = await change_user_flag(id, country)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_wipe(data: dict) -> str | None:
    id, mode = int(data["id"]), int(data["mode"])
    log(f"EX | Wipe | ID: {id}, Mode: {mode}", Ansi.LBLUE)
    result = await wipe_user(id, mode)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_rank(data: dict) -> str | None:
    beatmap_id = int(data["beatmap_id"])
    status = int(data["status"])
    frozen = bool(data["frozen"])
    if status not in PANEL_STATUSES:
        # AUD-27: only the statuses the panel offers; anything else is a bad message
        log(f"EX | Rank | Beatmap ID: {beatmap_id}, refused status {status}", Ansi.LRED)
        return "status not allowed"
    log(f"EX | Rank | Beatmap ID: {beatmap_id}, Status: {status}, Frozen: {frozen}", Ansi.LBLUE)
    response = await change_bm_status(beatmap_id, status, frozen)
    log("EX | " + response, Ansi.LBLUE)
    if response != "success":
        # nothing changed, so nothing to announce (AUD-29)
        return response

    # Notify Discord bot
    from app.usecases.notifications import notify_map_status_change
    from app.usecases.notifications import rank_type_name

    await app.state.services.redis.publish(
        "ex:map_status_change",
        orjson.dumps({
            "map_ids": [beatmap_id],
            "type": rank_type_name(status),
            "ranktype": "map",
            "status": status,
            "user_id": data.get("user_id"),
            "user_name": data.get("user_name"),
        }).decode(),
    )
    await notify_map_status_change([beatmap_id], status, data.get("user_id"))
    return "success"


async def handle_restrict(data: dict) -> str | None:
    id, admin_id, reason = int(data["id"]), int(data["userId"]), str(data.get("reason") or "")
    log(f"EX | Restrict | ID: {id}, User ID: {admin_id}, Reason: {reason}", Ansi.LBLUE)
    result = await restrict(id, admin_id, reason)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_unrestrict(data: dict) -> str | None:
    id, admin_id, reason = int(data["id"]), int(data["userId"]), str(data.get("reason") or "")
    log(f"EX | Unrestrict | ID: {id}, User ID: {admin_id}", Ansi.LBLUE)
    result = await unrestrict(id, admin_id, reason)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_alert_all(data: dict) -> str | None:
    message = str(data["message"])
    log(f"EX | Alert All | Message: {message}", Ansi.LBLUE)
    result = await alert_all(message)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_givedonator(data: dict) -> str | None:
    id, duration = int(data["id"]), str(data["duration"])
    log(f"EX | Give Donator | ID: {id}, Duration: {duration}", Ansi.LBLUE)
    result = await givedonator(id, duration)
    log("EX | " + result, Ansi.LBLUE)
    return result


def _privs_list(data: dict) -> list:
    privs = data.get("privs")
    if not isinstance(privs, list) or not all(isinstance(p, str) for p in privs):
        raise ValueError("privs must be a list of names")
    return privs


async def handle_addpriv(data: dict) -> str | None:
    id, privs = int(data["id"]), _privs_list(data)
    actor_id = int(data["userId"]) if data.get("userId") else None
    log(f"EX | Add Priv | ID: {id}, Privs: {privs}, By: {actor_id}", Ansi.LBLUE)
    result = await addpriv(id, privs, actor_id)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_removepriv(data: dict) -> str | None:
    id, privs = int(data["id"]), _privs_list(data)
    actor_id = int(data["userId"]) if data.get("userId") else None
    log(f"EX | Remove Priv | ID: {id}, Privs: {privs}, By: {actor_id}", Ansi.LBLUE)
    result = await removepriv(id, privs, actor_id)
    log("EX | " + result, Ansi.LBLUE)
    return result


async def handle_password_changed(data: dict) -> str | None:
    """Shiina set a new password (reset link from e-mail or from the panel).
    Ends every session of that account in bancho: the game client, IRC and the
    website chat. Whoever was logged in with the old password is out, and the
    /web/ endpoints stop accepting the old password, because they check it
    against the online session first. Payload: {"id": user id}."""
    user_id = int(data["id"])
    irc = getattr(app.state.services, "irc", None)
    sessions = [p for p in list(app.state.sessions.players) if p.id == user_id]
    closed = 0
    for player in sessions:
        if irc is not None:
            clients, _ = irc.detach(player)
            for client in clients:
                if client in irc.clients:
                    irc.clients.remove(client)
                try:
                    client.socket.close()
                except Exception:
                    pass
                closed += 1
        if player.is_online:
            player.logout()
    log(
        f"EX | Password Changed | ID: {user_id}, ended {len(sessions)} session(s), "
        f"closed {closed} chat connection(s)",
        Ansi.LBLUE,
    )


RECEIVERS = {
    "rank": handle_rank,
    "restrict": handle_restrict,
    "unrestrict": handle_unrestrict,
    "alert_all": handle_alert_all,
    "givedonator": handle_givedonator,
    "addpriv": handle_addpriv,
    "removepriv": handle_removepriv,
    "wipe": handle_wipe,
    "country_change": handle_country_change,
    "name_change": handle_name_change,
    "clan_change": handle_clan_change,
    "password_changed": handle_password_changed,
}
