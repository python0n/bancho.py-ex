from __future__ import annotations
 
import asyncio
import re
import time
import traceback
import ipaddress
from ipaddress import IPv4Address, IPv6Address, ip_network
from typing import Optional
 
import app.packets
import app.settings
import app.state
import app.settings
from app import commands


async def _process_irc_command(player, target, message):
    """Run a chat command marked as typed in IRC (not in the game client)."""
    token = commands.via_irc.set(True)
    try:
        return await commands.process_commands(player, target, message)
    finally:
        commands.via_irc.reset(token)
from app.logging import Ansi
from app.logging import log
from app.objects.channel import Channel
from app.objects.player import Player
from app.utils import make_safe_name

NAME = "banchobot"
FLOOD_MSGS = 10
FLOOD_WINDOW = 5.0


def main_session(user_id: int) -> Player | None:
    """The player's main session (game, IRC or website chat), never a tourney client."""
    for p in app.state.sessions.players:
        if p.id == user_id and not p.is_tourney_client:
            return p
    return None
WHITE_SPACE = re.compile(r"\r?\n")


def match_of_channel(name: str):
    """The live match behind a #multi_<seq> channel name, or None."""
    name = name.lower()
    if not name.startswith("#multi_"):
        return None
    # by the channel itself, not the seq -> slot map (safe even if that map is off)
    for m in app.state.sessions.matches:
        if m is not None and m.chat.real_name.lower() == name:
            return m
    return None


def is_match_ref(player: Player, match) -> bool:
    return match.is_ref(player)


def may_enter_match(player: Player, match) -> bool:
    return match.may_enter_chat(player)

# a connection that has not logged in is closed after this many seconds
# or this many wrong PASS commands
PRE_AUTH_TIMEOUT = 30
MAX_FAILED_LOGINS = 5


class BanchoIRCException(Exception):
    """IRC exception."""
 
    def __init__(self, code_error: int, error: str):
        self.code: int = code_error
        self.error: str = error
 
    def __str__(self) -> str:
        return repr(self.error)
 
class IRCClient:
    def __init__(
        self,
        server: IRCServer,
        writer: asyncio.StreamWriter,
    ):
        self.player = None
        self.ping_time = int(time.time())
        self.queue = bytearray()
        self.socket = writer
        self.server = server
        self.player: Player
        self._ip = None
        self.ip_obj = None
        self.owns_player = False  # True only when IRC login created a new player session
        self._msg_times: list[float] = []  # PRIVMSG flood limit
        self.failed_logins = 0  # wrong PASS on this connection
        self._flood_warned = 0.0
 
    def __repr__(self) -> str:
        if self.player is not None:
            return f"{self.player.name}@{NAME}"
        else:
            return f"UNAUTHORIZED@{NAME}"
 
    def __str__(self) -> str:
        if self.player is not None:
            return f"{self.player.name}@{NAME}"
        else:
            return f"UNAUTHORIZED@{NAME}"
 
    def __or__(self):
        if not self.player:
            return None
        else:
            return self.player
 
    def dequeue(self) -> bytearray:
        buffer = self.queue
        self.queue = bytearray()
        return buffer
 
    async def add_queue(self, message: str) -> None:
            self.socket.write((message + "\r\n").encode())
            await self.socket.drain()
 
    async def send_welcome_msg(self) -> None:
        if self.player is None:
            raise RuntimeError(
                "Attempted to send welcome message to an unauthenticated IRC client.",
            )
        await self.add_queue(
            f":{NAME} 001 {self.player.name} :Welcome to the Internet Relay Network, {self!r}")
        await self.add_queue(
            f":{NAME} 002 :- Your host is {self.socket.get_extra_info('peername')[0]}")
        await self.add_queue(
            f":{NAME} 251 :- There are {len(app.state.sessions.players)} users")
        await self.add_queue(f":{NAME} 375 :- {NAME} Message of the day - ")
        await self.add_queue(
            f":{NAME} 372 {self.player.name} :- Visit {app.settings.DOMAIN}")
        await self.add_queue(f":{NAME} 376 : End of MOTD command")
 
    async def login(self, irc_key: Optional[str] = "") -> Player | None:
        if not irc_key:
            raise BanchoIRCException(464, f"PASS :Incorrect password")

        player = await Player.from_irc(irc_key)

        if not player:
            raise BanchoIRCException(404, f"PLAYER :Incorrect username")

        return await self.login_player(player)

    async def login_player(self, player: Player) -> Player:
        """Log in `player` (loaded from sql), reusing their session if they are online."""
        if player.restricted:
            raise BanchoIRCException(404, f"PLAYER :You can't login in restricted mode")

        # If the player is already online (game client, IRC or the website chat),
        # reuse their session to avoid duplicate entries in channel NAMES replies.
        # Tourney client sessions are only spectators, never the main session.
        existing = main_session(player.id)
        if existing:
            existing.irc_client = True
            self.owns_player = False
            log(f"IRC login: {existing.name}", Ansi.LCYAN)
            log(f"{existing} logged in from IRC with {self.ip}", Ansi.LCYAN)
            return existing

        player.irc_client = True
        await player.stats_from_sql_full()
        try:
            geoloc = await app.state.services.fetch_geoloc(self.ip_obj)
        except Exception as e:
            log(f"IRC login: geolocation failed for {self.ip}: {e!r}", Ansi.LYELLOW)
            geoloc = None
        if geoloc:
            player.geoloc = geoloc

        user_data = app.packets.user_presence(player)
        app.state.sessions.players.append(player)
        app.state.sessions.players.enqueue(user_data)

        self.owns_player = True
        log(f"IRC login: {player.name}", Ansi.LCYAN)
        log(f"{player} logged in from IRC with {self.ip}", Ansi.LCYAN)

        return player

    async def data_received(self, data: bytes) -> None:
        global cmd
        message = data.decode("utf-8")
        parts, command, args = None, None, None

        # Keep IRC session alive: update last_recv_time on ANY activity
        # so _disconnect_ghosts doesn't auto-dc the IRC user.
        if self.player is not None:
            self.player.last_recv_time = time.time()

        try:
            client_data = WHITE_SPACE.split(message.strip())
            for cmd in client_data:
                if not cmd.strip(): 
                    continue
                parts = cmd.split(" ", 1)
                command = parts[0]
                args = parts[1] if len(parts) > 1 else ""
 
                if command == "CAP":
                    continue

                if command == "PROXY":
                    # PROXY protocol v1 from our own TLS proxy (nginx stream, proxy_protocol on):
                    # "PROXY TCP4 <client ip> <proxy ip> <client port> <proxy port>".
                    # Only before login and only from a local/private address.
                    self.apply_proxy_header(args)
                    continue
 
                if command == "GET":
                    log(f"[WARN]: IRC SERVER GOT GET METHODE FROM ANONYMOUS", Ansi.LYELLOW)
                    continue
 
                if command == "LIST":
                    await self.handler_list(args)
                    continue
 
                if command == "NAMES":
                    await self.handler_names(args)
                    continue
 
                if command == "PASS":
                    try:
                        player = await self.login(args)
                    except BanchoIRCException:
                        self.failed_logins += 1
                        raise
 
                    if player:
                        self.player = player
                        await self.send_welcome_msg()
                        await self.server.restore_matches(self)
                        continue
 
                    raise BanchoIRCException(464, f"{command} :Incorrect password")
 
                handler = getattr(self, f"handler_{command.lower()}", None)
                if not handler:
                    raise BanchoIRCException(421, f"{command} :Unknown Command!")
 
                await handler(args)
 
        except ValueError:
            log(f"[ERROR] Malformed command: {cmd!r}", Ansi.LRED)
            traceback.print_exc()
        except TypeError:
            await self.socket.drain()
            return
        except AttributeError as e:
            if "player" in str(e):
                error_msg = f":{NAME} 451 :You must finish logging in first"
                self.socket.write(error_msg.encode())
                log(f"Unauthenticated client tried to send data: {e}", Ansi.LRED)
            else:
                raise
        except BanchoIRCException as e:
            self.socket.write(f":{NAME} {e.code} {e.error}\r\n".encode())
        except Exception as e:
            self.socket.write(f":{NAME} ERROR {repr(e)}".encode())
            traceback.print_exc()

    def apply_proxy_header(self, args: str) -> None:
        if self.player is not None or getattr(self, "_proxy_seen", False):
            return
        self._proxy_seen = True
        peer = self.ip_obj
        if peer is None or not (peer.is_loopback or peer.is_private):
            log(f"[IRC] ignored PROXY header from {self.ip}", Ansi.LYELLOW)
            return
        parts = args.split()
        if len(parts) >= 2 and parts[0] in ("TCP4", "TCP6"):
            try:
                ipaddress.ip_address(parts[1])
            except ValueError:
                return
            self.ip = parts[1]

    async def handler_nick(self, args: str) -> None:
        pass
 
    async def handler_topic(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered")
        channel_name, *topic_parts = args.split(" :")
        channel = app.state.sessions.channels.get_by_name(channel_name)
 
        if not channel:
            raise BanchoIRCException(403, f"{channel_name} :No such channel")
 
        # GET TOPIC
        if not topic_parts:
            await self.add_queue(f":{NAME} 332 {self.player.name} {channel.real_name} :{channel.topic}")
        # SET TOPIC
        else:
            if not self.player in channel.moderators:
                raise BanchoIRCException(482, f"{channel.real_name} :You're not a channel operator")
            channel.topic = " :".join(topic_parts)
            await self.add_queue(f":{self.player.name} TOPIC {channel.real_name} :{channel.topic}")
 
    async def handler_names(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered") 
 
        channel_name = args.strip()
        channel = app.state.sessions.channels.get_by_name(channel_name)
 
        if not channel:
            raise BanchoIRCException(403, f"{channel_name} :No such channel")
 
        if channel not in self.player.channels:
            raise BanchoIRCException(442, f"{channel_name} :You're not in this channel")
 
        members = " ".join([p.name for p in channel.players])
        await self.add_queue(f":{NAME} 353 {self.player.name} = {channel.real_name} :{members}")
        await self.add_queue(f":{NAME} 366 {self.player.name} {channel.real_name} :End of NAMES list")
 
    async def handler_ping(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered")
        if self.player is not None:
            self.ping_time = int(time.time())
            await self.add_queue(f":{NAME} PONG :{NAME}")
            if self.player.irc_client:
                self.player.last_recv_time = time.time()
 
    async def handler_privmsg(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "PRIVMSG :You must log in first")
 
        recipient, msg = args.split(" ", 1)
        if " " in msg and msg.startswith(":"):
            msg = msg[1:]
        elif msg.startswith(":"):
            msg = msg[1:]

        if self.player.silenced:
            await self.add_queue(f":{NAME} 404 {self.player.name} {recipient} :You are silenced")
            return

        # flood limit like the game chat: at most FLOOD_MSGS messages per FLOOD_WINDOW
        now = time.monotonic()
        self._msg_times = [t for t in self._msg_times if now - t < FLOOD_WINDOW]
        if len(self._msg_times) >= FLOOD_MSGS:
            if now - self._flood_warned > FLOOD_WINDOW:
                self._flood_warned = now
                await self.add_queue(f":{NAME} NOTICE {self.player.name} :You are sending messages too fast, slow down.")
            return
        self._msg_times.append(now)

        msg = msg.strip()
        if not msg:
            return
        if len(msg) > 2000:
            msg = f"{msg[:2000]}... (truncated)"
            await self.add_queue(
                f":{NAME} NOTICE {self.player.name} :Your message was truncated (exceeded 2000 characters).",
            )

        if msg.startswith("!mp close"):
            log(f"Trying to part channel {recipient}", Ansi.LYELLOW)
            await self.handle_mp_close(recipient)
            return

        if recipient.startswith("#") or recipient.startswith("$"):
            channel = app.state.sessions.channels.get_by_name(recipient)
            if not channel:
                raise BanchoIRCException(
                    403,
                    f"{recipient} :Cannot send a message to a non-existing channel",
                )
 
            if channel not in self.player.channels:
                # Try to auto-rejoin the channel if it still exists
                # (a match channel only for people who may be in it)
                m = match_of_channel(channel.real_name)
                if m is not None and not may_enter_match(self.player, m):
                    raise BanchoIRCException(
                        404,
                        f"{recipient} :Cannot send message to the channel",
                    )
                if self.player.join_channel(channel):
                    log(f"[IRC] Auto-rejoined {self.player.name} to {recipient}", Ansi.LYELLOW)
                else:
                    raise BanchoIRCException(
                        404,
                        f"{recipient} :Cannot send message to the channel",
                    )

            if not channel.can_write(self.player.priv):
                # e.g. #announce: read-only for players
                raise BanchoIRCException(
                    404,
                    f"{recipient} :Cannot send message to the channel",
                )

            await self.send_message(self.player, recipient, msg)
 
            for client in await self.server.authorized_clients:
                assert client.player is not None
 
                if channel in client.player.channels and client != self:
                    await client.add_queue(
                        f":{self.player.safe_name} PRIVMSG {recipient} :{msg}",
                    )
        else:
            # who wrote to whom, never the text (same rule as the game client)
            log(f"[IRC] Private message from {self.player.name} to {recipient}: [{len(msg)} chars]", Ansi.LMAGENTA)
            target = await app.state.sessions.players.from_cache_or_sql(name=make_safe_name(recipient))
            if target is not None and not target.is_bot_client:
                if self.player.id in target.blocks or (
                    target.pm_private and self.player.id not in target.friends
                ):
                    await self.add_queue(f":{NAME} 404 {self.player.name} {recipient} :{target.name} doesn't accept your messages")
                    return
                if target.silenced:
                    await self.add_queue(f":{NAME} 404 {self.player.name} {recipient} :{target.name} is silenced")
                    return
                # a player loaded from sql has a token too, so check the session list
                if main_session(target.id) is None:
                    # offline players get it as mail on their next login, like in the game
                    from app.repositories import mail as mail_repo
                    await mail_repo.create(from_id=self.player.id, to_id=target.id, msg=msg)
                    await self.add_queue(
                        f":{NAME} NOTICE {self.player.name} :{target.name} is offline, they will get your message on their next login.",
                    )
                    return
            await self.send_message(self.player, recipient, msg)
            if target is not None and not target.is_bot_client:
                # PM history (the website chat reads it); the game saves its PMs the same way
                from app.repositories import mail as mail_repo
                try:
                    await mail_repo.create(from_id=self.player.id, to_id=target.id, msg=msg)
                except Exception as e:
                    log(f"[IRC] could not save PM history: {e!r}", Ansi.LYELLOW)
 
            for client in await self.server.authorized_clients:
                assert client.player is not None
 
                if msg.startswith("!mp_make"):
                    await self.handle_mp_make()
 
                if client.player.name == recipient:
                    await client.add_queue(f":{self.player.name} PRIVMSG {recipient} :{msg}")
 
    async def handler_list(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered")
        """Send a message with all channels."""
        if self.player is None:
            raise RuntimeError("Unauthenticated client tried to list channels.")
 
        for channel in app.state.sessions.channels:
            if channel.can_read(self.player.priv): 
                await self.add_queue(
                    f":{NAME} 322 {self.player.name} {channel.real_name} "
                    f"{len(channel.players)} :{channel.topic or 'No topic'}"
                )
 
        await self.add_queue(f":{NAME} 323 {self.player.name} :End of LIST")
 
    async def handler_part(self, channel: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered") 
 
        channel = channel.split(" ")[0]
        chan = app.state.sessions.channels.get_by_name(channel)

        if not chan:
            raise BanchoIRCException(
                403,
                f"{channel} :No channel named {channel} has been found",
            )

        if chan in self.player.channels:
            for client in await self.server.authorized_clients:
                assert client.player is not None

                if chan in client.player.channels:
                    await client.add_queue(f":{self.player.name} PART :{chan.real_name}")

            self.player.leave_channel(chan)
        else:
            raise BanchoIRCException(
                442,
                f"{channel} :You're not on that channel",
            )
 
    async def handle_mp_make(self) -> None:
        """Handle !mp make to create a new match."""
        if not hasattr(self.player, 'match') or not self.player.match:
            raise BanchoIRCException(404, "NOTICE :You're not in a match")

        channel_name = self.player.match.chat.real_name
 
        try:
            await self.handler_join(channel_name)
 
        except BanchoIRCException as e:
            if e.code == 403:
                await self.add_queue(f":{NAME} NOTICE {self.player.name} :❌ Lobby {channel_name} doesn't exist")
                await self.socket.drain()
            else:
                raise
 
    async def handle_mp_close(self, recipient: str = "") -> None:
        """Close the current match by routing through the !mp close command."""
        try:
            match = self.player.match

            # If player.match is None (e.g. after reconnect or auto-close), try to
            # recover the match from the channel name the command was sent to.
            if match is None and recipient.startswith("#multi_"):
                candidate = match_of_channel(recipient)
                if candidate is not None and is_match_ref(self.player, candidate):
                    match = candidate
                    self.player.match = match
                    self.player.join_channel(match.chat)

            if match is None:
                await self.add_queue(
                    f":{NAME} NOTICE {self.player.name} :No active match to close."
                )
                return

            match_id = match.id
            channel_name = match.chat.real_name
            match_chat = match.chat

            # Let the normal command handler do all cleanup (slots, DB, dispose)
            cmd = await _process_irc_command(self.player, match_chat, "!mp close")

            # mp_close returns None on success — detect success by checking player.match
            if self.player.match is None:
                # Match was successfully closed; tell IRC client to part the channel
                await self.add_queue(f":{self.player.name} PART {channel_name} :Match closed")
                log(f"[IRC] Match {match_id} closed by {self.player.name}", Ansi.LYELLOW)
            else:
                # Command ran but match wasn't closed — surface the response if any
                if cmd and cmd.get("resp"):
                    bot_name = app.state.sessions.bot.name
                    await self.add_queue(
                        f":{bot_name} PRIVMSG {self.player.name} :{cmd['resp']}"
                    )

        except Exception as e:
            log(f"[WARN] MP close failed: {e}", Ansi.LYELLOW)
 
    async def handler_join(self, channel: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered") 
 
        chan = app.state.sessions.channels.get_by_name(channel)
 
        if not chan:
            raise BanchoIRCException(
                403,
                f"{channel} :No channel named {channel} has been found",
            )
        m = match_of_channel(chan.real_name)
        if m is not None and not may_enter_match(self.player, m):
            # joining a match channel used to make anyone a referee of that match
            raise BanchoIRCException(
                473,
                f"{chan.real_name} :You are not a referee or player of this match",
            )
        try:
            log(f"Joining channel {channel}", Ansi.LYELLOW)
            already_member = self.player in chan.players
            joined = self.player.join_channel(chan)
            if joined or already_member:
                # If joining a match channel, restore player.match reference
                # (referees only get back what they had, nobody becomes one here)
                if m is not None and self.player.match is None and is_match_ref(self.player, m):
                    self.player.match = m
                    self.server.refresh_referee(m, self.player)

                for client in await self.server.authorized_clients:
                    assert client.player is not None

                    if chan in client.player.channels:
                        await client.add_queue(f":{self.player.name} JOIN :{chan.real_name}")

                if chan.topic:
                    await self.add_queue(f"332 {chan.real_name} :{chan.topic}")
                else:
                    await self.add_queue(f"331 {chan.real_name} :No topic is set")

                nicks = " ".join([x.name for x in chan.players])
                await self.add_queue(f":{NAME} 353 {self.player.name} = {chan.real_name} :{nicks}")
                await self.add_queue(
                    f":{NAME} 366 {self.player.name} {chan.real_name} :End of NAMES list",
                )
            else:
                raise BanchoIRCException(
                    403,
                    f"{channel} :No channel named {channel} has been found",
                )
        except BanchoIRCException:
            raise
        except Exception as e:
            log(f"Error {e}", Ansi.LRED)
        


 
 
    async def handler_user(self, args: str) -> None:
        pass
 
    async def handler_away(self, args: str) -> None:
        pass
 
    async def handler_quit(self, args: str) -> None:
        if self.player is None:
            raise BanchoIRCException(451, "You have not registered")

        # Notify all shared-channel clients of the QUIT (deduplicate per client)
        notified: set[int] = set()
        for chan in list(self.player.channels):
            for client in await self.server.authorized_clients:
                if (
                    client.player is not None
                    and client.player.id not in notified
                    and chan in client.player.channels
                ):
                    notified.add(client.player.id)
                    await client.add_queue(f":{self.player.name} QUIT :{args.lstrip(':')}")

        # Logout once, outside the channel loop
        player_name = self.player.name
        self.server.release(self)
        self.player = None  # prevent the finally block from calling logout again
        log(f"{player_name} disconnected from IRC", Ansi.YELLOW)

        self.socket.close()
        await self.socket.wait_closed()
 
    async def handler_mp(self, channel: str) -> None:
        # same as JOIN, with the same checks for match channels
        await self.handler_join(channel)

    async def send_message(self, fro: Player, to: str, message: str) -> int:
        if not self.player: 
            return 451
        if to.startswith("#"):
            channel = app.state.sessions.channels.get_by_name(to)
 
            if not channel:
                return 403
 
            if message.startswith(app.settings.COMMAND_PREFIX):
                cmd = await _process_irc_command(fro, channel, message)
            else:
                cmd = None
 
            bot_name = app.state.sessions.bot.name

            # typed on the website or IRC while the game is open: the game did not
            # see it being typed, so it gets the message too
            echo = fro.client_details is not None

            if cmd:
                # a command was triggered.
                if not cmd["hidden"]:
                    channel.send(message, sender=fro, to_self=echo)
                    if cmd["resp"] is not None:
                        channel.send_bot(cmd["resp"])
                else:
                    staff = app.state.sessions.players.staff
                    channel.send_selective(
                        msg=message,
                        sender=fro,
                        recipients=staff - {fro},
                    )
                    if cmd["resp"] is not None:
                        channel.send_selective(
                            msg=cmd["resp"],
                            sender=app.state.sessions.bot,
                            recipients=staff | {fro},
                        )
            else:
                channel.send(message, fro, to_self=echo)
                log(
                    f"{fro} @ {channel}: {message}",
                    Ansi.LCYAN,
                )
        else:
            recipient = await app.state.sessions.players.from_cache_or_sql(
                name=make_safe_name(to),
            )
 
            if not recipient:
                return 401
 
            if recipient.is_bot_client:
                if message.startswith(app.settings.COMMAND_PREFIX):
                    cmd = await _process_irc_command(fro, recipient, message)
                else:
                    cmd = None
 
                if cmd:
                    recipient.send(message, sender=fro)

                    if cmd["resp"] is not None:
                        fro.send_bot(cmd["resp"])
                        # send_bot already forwards to IRC via bancho_message
                        # but for private messages to BanchoBot, we need direct delivery
                        bot_name = app.state.sessions.bot.name
                        await self.add_queue(
                            f":{bot_name} PRIVMSG {fro.name} :{cmd['resp']}"
                        )
            else:
                recipient.send(message, fro)
                log(
                    f"{fro} @ {recipient}: {message}",
                    Ansi.LCYAN,
                )
 
        return 1
 
    @property
    def ip(self):
        return self._ip
 
    @ip.setter
    def ip(self, value):
        self._ip = value
        try:
            self.ip_obj = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            try:
                self.ip_obj = ipaddress.IPv6Address(value)
            except ipaddress.AddressValueError:
                self.ip_obj = None
                if app.settings.DEBUG:
                    log(f"Invalid IP format: {value}", Ansi.LRED)
 
 
class IRCServer:
    def __init__(self, port: int, host: str, loop: asyncio.AbstractEventLoop) -> None:
        self.socket_server = None
        self.loop = loop
        self.host = host
        self.port = port
        self.socket_server: asyncio.Server
        self.clients: set[IRCClient] = set()
 
    @staticmethod
    def refresh_referee(match, player: Player) -> None:
        """Point a referee entry at the player's current session."""
        old = {r for r in match.referees if r.id == player.id}
        if old:
            match.referees -= old
            match.referees.add(player)

    async def restore_matches(self, client: IRCClient) -> None:
        """After logging in from IRC or the website chat, put the player back into
        the matches they host or referee. A closed tab or a lost connection does not
        take the match away; it ends only with !mp close or the 30 minute timeout
        of an empty room."""
        player = client.player
        if player is None:
            return
        for match in app.state.sessions.matches:
            if match is None or not is_match_ref(player, match):
                continue
            if match.get_slot(player) is not None:
                continue  # playing in it from the game, the game handles that
            chan = match.chat
            # the last member leaving removes an instance channel from the list,
            # JOIN #multi_<n> could not find it any more
            if chan not in app.state.sessions.channels:
                app.state.sessions.channels.append(chan)
            self.refresh_referee(match, player)
            if player.match is None:
                player.match = match
            if player in chan.players or player.join_channel(chan):
                await client.add_queue(f":{player.name} JOIN :{chan.real_name}")
                if chan.topic:
                    await client.add_queue(f":{NAME} 332 {player.name} {chan.real_name} :{chan.topic}")
                nicks = " ".join(p.name for p in chan.players)
                await client.add_queue(f":{NAME} 353 {player.name} = {chan.real_name} :{nicks}")
                await client.add_queue(f":{NAME} 366 {player.name} {chan.real_name} :End of NAMES list")
                log(f"[IRC] {player} is back in {chan.real_name}", Ansi.LCYAN)

    def clients_of(self, player: Player) -> list[IRCClient]:
        """IRC and website chat connections attached to this exact session."""
        return [c for c in self.clients if getattr(c, "player", None) is player]

    def release(self, client: IRCClient) -> None:
        """A chat connection is gone: end the session only if nothing else uses it."""
        player = client.player
        if player is None:
            return
        others = [c for c in self.clients_of(player) if c is not client]
        if others:
            # another IRC/website connection keeps the session alive
            if client.owns_player:
                others[0].owns_player = True
            return
        if client.owns_player:
            player.logout()
        else:
            player.irc_client = False

    def detach(self, player: Player) -> tuple[list[IRCClient], list[Channel]]:
        """Unhook the chat connections of a session that is about to end, so they
        don't get its PART lines; returns them with the channels to rejoin."""
        clients = self.clients_of(player)
        channels = [c for c in player.channels if not c.instance]
        for client in clients:
            client.player = None
        return clients, channels

    async def attach(
        self,
        clients: list[IRCClient],
        channels: list[Channel],
        player: Player,
        owns: bool,
    ) -> None:
        """Hook chat connections to a new session and rejoin their channels."""
        if not clients:
            return
        player.irc_client = True
        for i, client in enumerate(clients):
            client.player = player
            client.owns_player = owns and i == 0
        for chan in channels:
            if player in chan.players or player.join_channel(chan):
                for client in clients:
                    await client.add_queue(f":{player.name} JOIN {chan.real_name}")

    async def keep_chat_after_game_logout(self, player: Player) -> None:
        """The game client logged out but the player still has IRC or the website
        chat open: give those connections a chat-only session of their own."""
        clients, channels = self.detach(player)
        player.logout()
        if not clients:
            return
        new = await app.state.sessions.players.get_sql(id=player.id)
        if new is None or new.restricted:
            for client in clients:
                client.player = None
                client.socket.close()
            return
        await new.stats_from_sql_full()
        new.geoloc = player.geoloc
        app.state.sessions.players.append(new)
        app.state.sessions.players.enqueue(app.packets.user_presence(new))
        await self.attach(clients, channels, new, owns=True)
        log(f"{new} closed the game, chat stays connected", Ansi.LCYAN)

    @property
    async def authorized_clients(self) -> set[IRCClient]:
        return {client for client in self.clients if hasattr(client, "player") and client.player is not None}  # 👈 Sicherer Check
 
    async def bancho_join(self, player: Player, channel: Channel) -> None:
        try:
            for client in await self.authorized_clients:
                assert client.player is not None
 
                if channel in client.player.channels:
                    await client.add_queue(f":{player.name} JOIN {channel.real_name}")
        except TypeError:
            return
 
    async def bancho_part(self, player: Player, channel: Channel) -> None:
        try:
            for client in await self.authorized_clients:
                assert client.player is not None
 
                if channel in client.player.channels:
                    await client.add_queue(f":{player.name} PART {channel.real_name}")
        except TypeError:
            return
 
    async def bancho_message(self, fro: str, to: str, message: str) -> None:
        if to.startswith("#"):
            channel = app.state.sessions.channels.get_by_name(to)
            if channel:
                for client in await self.authorized_clients:
                    #  Nur Clients im Channel benachrichtigen
                    if channel in client.player.channels:
                        if client is None:
                            continue
                        await client.add_queue(f":{fro} PRIVMSG {to} :{message}")
                        """ try:
                               for client in self.authorized_clients:
                                   if client is None:
                                       return
                                    assert client.player is not None
                                    fix sometime i guess
                                    if client.player.name != fro and to in [
                                       x.name for x in client.player.channels
                                   ]:
                                   client.add_queue(f":{fro} PRIVMSG {to} :{message}")
                           except TypeError:
                               return"""
        else:
            for client in await self.authorized_clients:
                # assert client.player is not None
                if client is None:
                    continue
                # private message
                if client.player.name == to:
                    await client.add_queue(f":{fro} PRIVMSG {to} :{message}")

    async def callback_handler(
            self,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peername = writer.get_extra_info("peername")
            client_ip = peername[0] if peername else "0.0.0.0"
 
            client = IRCClient(self, writer)
            client.ip = client_ip
 
            # health checks (status page, nginx stream) open and close a connection
            # every few seconds; log only real logins (and everything with DEBUG)
            if app.settings.DEBUG:
                log(f"[IRC] New connection from {client_ip}", Ansi.LGREEN)
 
            self.clients.add(client)
            connected_at = time.monotonic()
 
            while True:
                if data_to_send := client.dequeue():
                    writer.write(data_to_send)

                if client.player is None:
                    # not logged in yet: the connection gets a fixed time and
                    # a few PASS attempts, then it is closed. without this
                    # anyone could hold connections open forever
                    remaining = PRE_AUTH_TIMEOUT - (time.monotonic() - connected_at)
                    if remaining <= 0 or client.failed_logins >= MAX_FAILED_LOGINS:
                        break
                    try:
                        data = await asyncio.wait_for(reader.read(4096), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                else:
                    data = await reader.read(4096)

                if not data:
                    break
 
                try:
                    await client.data_received(data)
                except BanchoIRCException as e:
                    writer.write(e.error.encode())
                    await writer.drain()
                except Exception as e:
                    log(f"[IRC] Error handling data: {e}", Ansi.LRED)
                    break
 
                if app.settings.DEBUG:
                    sanitized_msg = data.decode("utf-8").replace("\r\n", "; ")
                    log(f"[IRC] Received: {sanitized_msg}", Ansi.LGREEN)
 
                await writer.drain()
 
        except ConnectionResetError:
            log(f"[IRC] Connection reset by {client_ip}", Ansi.LYELLOW)
        except Exception as e:
            log(f"[IRC] Unexpected error: {e}", Ansi.LRED)
        finally:
            try:
                if client in self.clients:
                    self.clients.remove(client)
 
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
 
                if client.player:
                    log(f"[IRC] Client disconnected: {client.player.name} ({client_ip})", Ansi.LYELLOW)
                    self.release(client)
                elif app.settings.DEBUG:
                    log(f"[IRC] Client disconnected: Anonymous ({client_ip})", Ansi.LYELLOW)
 
            except Exception as e:
                log(f"[IRC] Cleanup error: {e}", Ansi.LRED)
 
    async def start(self) -> IRCServer:
        server = await asyncio.start_server(
                    self.callback_handler,
                    self.host,
                    self.port,
        )
 
        sockname = server.sockets[0].getsockname()
        log(
                    f"Serving IRC on {sockname[0]}:{sockname[1]}",
                    Ansi.LCYAN,
        )
 
        self.socket_server = server
        return self
    
