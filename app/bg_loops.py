from __future__ import annotations

import asyncio
import time

import app.packets
import app.settings
import app.state
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log

OSU_CLIENT_MIN_PING_INTERVAL = 300000 // 1000  # defined by osu!


async def initialize_housekeeping_tasks() -> None:
    """Create tasks for each housekeeping tasks."""
    log("Initializing housekeeping tasks.", Ansi.LCYAN)

    loop = asyncio.get_running_loop()

    app.state.sessions.housekeeping_tasks.update(
        {
            loop.create_task(task)
            for task in (
                _remove_expired_donation_privileges(interval=30 * 60),
                _update_bot_status(interval=5 * 60),
                _disconnect_ghosts(interval=OSU_CLIENT_MIN_PING_INTERVAL // 3),
                _reconcile_vanilla_ranks(interval=15),
                _recover_staged_vanilla_replays(interval=60),
            )
        },
    )


async def _remove_expired_donation_privileges(interval: int) -> None:
    """Remove donation privileges from users with expired sessions.

    AUD-09: one error (a database hiccup, a user that can't be loaded) used to end
    this task for good, so supporter stopped expiring until the next restart.
    Now every round and every player is guarded and the loop keeps going."""
    while True:
        try:
            await _expire_donors_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"Expiring supporters failed, retrying later: {e!r}", Ansi.LRED)
        await asyncio.sleep(interval)


async def _expire_donors_once() -> None:
    if app.settings.DEBUG:
        log("Removing expired donation privileges.", Ansi.LMAGENTA)

    expired_donors = await app.state.services.database.fetch_all(
        "SELECT id FROM users "
        "WHERE donor_end <= UNIX_TIMESTAMP() "
        "AND priv & :donor_priv",
        {"donor_priv": Privileges.DONATOR.value},
    )

    for expired_donor in expired_donors:
        try:
            player = await app.state.sessions.players.from_cache_or_sql(
                id=expired_donor["id"],
            )

            if player is None:
                # can't be loaded as a player: take the bits away in the database directly
                await app.state.services.database.execute(
                    "UPDATE users SET priv = priv & ~:donor_priv, donor_end = 0 WHERE id = :id",
                    {"donor_priv": Privileges.DONATOR.value, "id": expired_donor["id"]},
                )
                log(f"User {expired_donor['id']}'s supporter status has expired.", Ansi.LMAGENTA)
                continue

            await player.remove_privs(Privileges.DONATOR)
            player.donor_end = 0
            await app.state.services.database.execute(
                "UPDATE users SET donor_end = 0 WHERE id = :id",
                {"id": player.id},
            )

            if player.is_online:
                player.enqueue(
                    app.packets.notification("Your supporter status has expired."),
                )

            log(f"{player}'s supporter status has expired.", Ansi.LMAGENTA)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"Expiring supporter of user {expired_donor['id']} failed: {e!r}", Ansi.LRED)


async def _disconnect_ghosts(interval: int) -> None:
    """Actively disconnect users above the
    disconnection time threshold on the osu! server."""
    while True:
        await asyncio.sleep(interval)
        current_time = time.time()

        for player in app.state.sessions.players:
            if current_time - player.last_recv_time > OSU_CLIENT_MIN_PING_INTERVAL:
                # Don't auto-dc players with an active IRC connection —
                # their last_recv_time is updated by IRC data_received(),
                # but as a safety net, also check the IRC client list.
                if getattr(player, "irc_client", False):
                    irc = getattr(app.state.services, "irc", None)
                    if irc:
                        has_active_irc = any(
                            c.player is player
                            for c in irc.clients
                            if hasattr(c, "player") and c.player is not None
                        )
                        if has_active_irc:
                            # IRC connection is alive, refresh the timer
                            player.last_recv_time = current_time
                            continue
                log(f"Auto-dced {player}.", Ansi.LMAGENTA)
                player.logout()


async def _update_bot_status(interval: int) -> None:
    """Re roll the bot status, every `interval`."""
    while True:
        await asyncio.sleep(interval)
        app.packets.bot_stats.cache_clear()


async def _reconcile_vanilla_ranks(interval: int) -> None:
    """Drain MySQL-backed vanilla rank outbox and periodically audit Redis.

    This task uses only modes 0..3; RX/AP leaderboard logic is untouched.
    The table must be created manually before enabling REVIEW v4.
    """
    from app.usecases.vanilla_rank_sync import (
        audit_rank_batch,
        drain_vanilla_rank_sync,
    )

    cursor_id, cursor_mode = 0, -1
    audit_due = 0.0  # immediate first scan; a complete pass every 30 min
    next_error_log = 0.0

    while True:
        try:
            await drain_vanilla_rank_sync(
                app.state.services.database,
                app.state.services.redis,
                limit=25,
            )
            if time.monotonic() >= audit_due:
                cursor_id, cursor_mode, queued, finished = await audit_rank_batch(
                    app.state.services.database,
                    app.state.services.redis,
                    after_id=cursor_id,
                    after_mode=cursor_mode,
                    limit=50,
                )
                if queued and app.settings.DEBUG:
                    log(f"Vanilla rank audit queued {queued} repair(s).", Ansi.LCYAN)
                if finished:
                    cursor_id, cursor_mode = 0, -1
                    audit_due = time.monotonic() + 30 * 60
        except Exception as exc:
            # Avoid log spam during a prolonged Redis outage. The outbox is
            # durable; a later tick will resume when the service recovers.
            if time.monotonic() >= next_error_log:
                log(
                    f"Vanilla rank reconciliation failed ({type(exc).__name__}); will retry.",
                    Ansi.LRED,
                )
                next_error_log = time.monotonic() + 60
        await asyncio.sleep(interval)


async def _recover_staged_vanilla_replays(interval: int) -> None:
    """Retry pending post-COMMIT vanilla replay renames from durable spool."""
    from app.api.domains.osu import REPLAYS_PATH
    from app.usecases.vanilla_replay_links_preview import (
        recover_linked_vanilla_replays as recover_staged_vanilla_replays,
    )

    while True:
        try:
            count = await recover_staged_vanilla_replays(
                app.state.services.database, REPLAYS_PATH, limit=25
            )
            if count:
                log(f"[vanilla-replay] recovered {count} replay(s)", Ansi.LGREEN)
        except Exception as exc:
            log(f"[vanilla-replay] recovery error: {type(exc).__name__}", Ansi.LRED)
        await asyncio.sleep(interval)
