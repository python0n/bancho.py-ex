from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

import app.packets
import app.settings
import app.state
from app.constants.privileges import Privileges

if TYPE_CHECKING:
    from app.objects.player import Player


CHANNEL_HISTORY_SIZE = 100
CHANNEL_HISTORY_TTL = 7 * 24 * 3600


async def save_channel_message(channel: str, sender: str, sender_id: int, msg: str) -> None:
    """Last messages of a public channel in Redis (chat:history:<channel>)."""
    import json as _json
    import time as _time

    key = f"chat:history:{channel}"
    entry = _json.dumps(
        {"time": int(_time.time()), "from": sender, "from_id": sender_id, "msg": msg},
        ensure_ascii=False,
    )
    redis = app.state.services.redis
    try:
        await redis.lpush(key, entry)
        await redis.ltrim(key, 0, CHANNEL_HISTORY_SIZE - 1)
        await redis.expire(key, CHANNEL_HISTORY_TTL)
    except Exception:
        pass


_MP_PASSWORD = re.compile(
    r"^(\s*(?:" + re.escape(app.settings.COMMAND_PREFIX) + r"|/)mp\s+(?:password|passwd|pass|pw)\b).*$",
    re.IGNORECASE | re.DOTALL,
)
_match_chat_error_logged = False


def mask_mp_password(msg: str) -> str:
    """`!mp password <x>` with the password replaced; for logs and history."""
    return _MP_PASSWORD.sub(r"\1 ***", msg)


async def save_match_message(match_id: int, user_id: int, msg: str, hidden: bool) -> None:
    """Match chat for the staff (mp_match_chat), shown on the match page."""
    # never keep a room password in the history
    msg = mask_mp_password(msg)
    try:
        await app.state.services.database.execute(
            "INSERT INTO mp_match_chat (match_id, user_id, msg, hidden) "
            "VALUES (:mid, :uid, :msg, :hidden)",
            {"mid": match_id, "uid": user_id, "msg": msg[:2000], "hidden": int(hidden)},
        )
    except Exception as e:
        # log the first failure, so a broken table does not go unnoticed
        global _match_chat_error_logged
        if not _match_chat_error_logged:
            _match_chat_error_logged = True
            from app.logging import Ansi, log
            log(f"[mp_match_chat] could not save a match chat message: {e!r}", Ansi.LRED)


class Channel:
    """An osu! chat channel.

    Possibly confusing attributes
    -----------
    real_name: `str`
        A name string of the channel.
        The cls.`name` property wraps handling for '#multiplayer' and
        '#spectator' when communicating with the osu! client; only use
        this attr when you need the channel's true name; otherwise you
        should use the `name` property described below.

    instance: `bool`
        Instanced channels are deleted when all players have left;
        this is useful for things like multiplayer, spectator, etc.
    """

    def __init__(
        self,
        name: str,
        topic: str,
        read_priv: Privileges = Privileges.UNRESTRICTED,
        write_priv: Privileges = Privileges.UNRESTRICTED,
        auto_join: bool = True,
        instance: bool = False,
    ) -> None:
        self.real_name = name

        if self.real_name.startswith("#spec_"):
            self.name = "#spectator"
        elif self.real_name.startswith("#multi_"):
            self.name = "#multiplayer"
        else:
            self.name = self.real_name

        self.topic = topic
        self.read_priv = read_priv
        self.write_priv = write_priv
        self.auto_join = auto_join
        self.instance = instance

        self.players: list[Player] = []

    def __repr__(self) -> str:
        return f"<{self.real_name}>"

    def __contains__(self, player: Player) -> bool:
        return player in self.players

    # XXX: should this be cached differently?

    def can_read(self, priv: Privileges) -> bool:
        if not self.read_priv:
            return True

        return priv & self.read_priv != 0

    def _save_history(self, sender: Player, msg: str) -> None:
        """Keep the last messages of public channels for the website chat."""
        # only channels every player can read (never #staff, #admin, #supporter...)
        if self.instance or int(self.read_priv) not in (0, int(Privileges.UNRESTRICTED)):
            return
        try:
            app.state.loop.create_task(
                save_channel_message(self.real_name, sender.name, sender.id, msg),
            )
        except (RuntimeError, AttributeError):
            pass

    def _save_match_chat(self, sender: Player, msg: str, hidden: bool = False) -> None:
        """Every message of a #multi_ channel (game, website chat, IRC, BanchoBot)
        goes to mp_match_chat, readable only by the staff on the match page."""
        if not self.real_name.startswith("#multi_"):
            return
        try:
            match = next(
                (m for m in app.state.sessions.matches if m is not None and m.chat is self),
                None,
            )
            if match is None or not match.web_id:
                return
            app.state.loop.create_task(
                save_match_message(match.web_id, sender.id, msg, hidden),
            )
        except (RuntimeError, AttributeError):
            pass

    def can_write(self, priv: Privileges) -> bool:
        if not self.write_priv:
            return True

        return priv & self.write_priv != 0

    def send(self, msg: str, sender: Player, to_self: bool = False) -> None:
        """Enqueue `msg` to all appropriate clients from `sender`."""
        data = app.packets.send_message(
            sender=sender.name,
            msg=msg,
            recipient=self.name,
            sender_id=sender.id,
        )

        for player in self.players:
            # skip only the sender's own session (the game shows its own messages);
            # a tourney client logged in on the same account is a different session
            # and has to get them
            if sender.id not in player.blocks and (to_self or player is not sender):
                player.enqueue(data)

        self._save_history(sender, msg)
        self._save_match_chat(sender, msg)

        _chat_pub = {"#announce": "ex:announce", "#osu": "ex:chat:osu"}
        if self.real_name in _chat_pub:
            import asyncio, json as _json
            asyncio.create_task(
                app.state.services.redis.publish(
                    _chat_pub[self.real_name],
                    _json.dumps({"player_name": sender.name, "msg": msg}),
                )
            )

    def send_bot(self, msg: str) -> None:
        """Enqueue `msg` to all connected clients from bot."""
        bot = app.state.sessions.bot

        msg_len = len(msg)

        if msg_len >= 31979:  # TODO ??????????
            msg = f"message would have crashed games ({msg_len} chars)"

        self.enqueue(
            app.packets.send_message(
                sender=bot.name,
                msg=msg,
                recipient=self.name,
                sender_id=bot.id,
            ),
        )
        self._save_history(bot, msg)
        self._save_match_chat(bot, msg)

        # Forward to any IRC clients in this channel
        irc = getattr(app.state.services, "irc", None)
        if irc:
            try:
                app.state.loop.create_task(
                    irc.bancho_message(bot.name, self.real_name, msg)
                )
            except RuntimeError:
                pass

    def send_selective(
        self,
        msg: str,
        sender: Player,
        recipients: set[Player],
    ) -> None:
        """Enqueue `sender`'s `msg` to `recipients`."""
        for player in recipients:
            if player in self:
                player.send(msg, sender=sender, chan=self)
        # hidden command or its answer, seen only by the staff (and the sender)
        self._save_match_chat(sender, msg, hidden=True)

    def append(self, player: Player) -> None:
        """Add `player` to the channel's players."""
        self.players.append(player)

    def remove(self, player: Player) -> None:
        """Remove `player` from the channel's players."""
        self.players.remove(player)

        if not self.players and self.instance:
            # if it's an instance channel and this
            # is the last member leaving, just remove
            # the channel from the global list.
            app.state.sessions.channels.remove(self)

    def enqueue(self, data: bytes, immune: Sequence[int] = []) -> None:
        """Enqueue `data` to all connected clients not in `immune`."""
        for player in self.players:
            if player.id not in immune:
                player.enqueue(data)
