"""Durable, idempotent vanilla (modes 0..3) MySQL -> Redis rank projection.

The score commit writes an outbox marker in the SAME MySQL transaction.
The Redis write happens AFTER commit, from fresh MySQL pp, privilege and country.
The outbox row is held FOR UPDATE during the Redis operation and acknowledgement
so that concurrent workers cannot apply older MySQL snapshots out of order.

A Redis Lua generation marker also rejects delayed/out-of-order Redis writes.
RX/AP modes and PP calculation are intentionally not modified here.

This module never runs DDL. See migrations/review_v4_vanilla_rank_sync.sql.
"""
from __future__ import annotations

import asyncio
from typing import Any


# Redis performs all changes for one user+mode atomically. Version key protects
# against an older, delayed write arriving AFTER a more recent generation.
# Keep existing leaderboard key names for consumers (Shiina and osu client).
_SYNC_LUA = """
local generation = tonumber(ARGV[1])
local userid = ARGV[2]
local pp = tonumber(ARGV[3])
local allowed = tonumber(ARGV[4])
local seen = tonumber(redis.call('GET', KEYS[1]) or '-1')
if seen > generation then
  return 0
end
  local capture = ARGV[5] == '1'
  local before = 0
  if capture and seen < generation then
    local pos = redis.call('ZREVRANK', KEYS[2], userid)
    if pos then before = pos + 1 end
  end
local redis_previous = redis.call('GET', KEYS[5])
if redis_previous and redis_previous ~= KEYS[4] then
  redis.call('ZREM', redis_previous, userid)
end
if KEYS[3] ~= KEYS[4] then
  redis.call('ZREM', KEYS[3], userid)
end
if allowed == 1 and pp > 0 then
  redis.call('ZADD', KEYS[2], pp, userid)
  redis.call('ZADD', KEYS[4], pp, userid)
else
  redis.call('ZREM', KEYS[2], userid)
  redis.call('ZREM', KEYS[4], userid)
end
redis.call('SET', KEYS[5], KEYS[4])
redis.call('SET', KEYS[1], generation)
  if capture then
    -- A replay of an already-applied generation must not announce again.
    if seen == generation then return {1, 0, 0, ''} end
    local pos = redis.call('ZREVRANK', KEYS[2], userid)
    local after = 0
    if pos then after = pos + 1 end
    local displaced = ''
    if after > 0 and after <= 50 and (before == 0 or after < before) then
      local users = redis.call('ZREVRANGE', KEYS[2], after, after)
      displaced = users[1] or ''
    end
    return {1, before, after, displaced}
  end
return 1
"""


class RankSyncVersionConflict(RuntimeError):
    """Redis holds a future generation (possibly DB restored from backup)."""


def _check_mode(mode: int) -> None:
    if int(mode) not in (0, 1, 2, 3):
        raise ValueError("vanilla_rank_sync only supports modes 0..3 (not RX/AP)")


async def enqueue_vanilla_rank_sync(db: Any, user_id: int, mode: int) -> None:
    """Call inside the same MySQL transaction as the PP/privilege/country change.

    DO NOT call after committing score stats or a crash could lose the intent.
    """
    _check_mode(mode)
    await db.execute(
        "INSERT INTO vanilla_rank_sync (userid, mode, generation, applied_generation) "
        "VALUES (:user_id, :mode, 1, 0) "
        "ON DUPLICATE KEY UPDATE generation = generation + 1",
        {"user_id": int(user_id), "mode": int(mode)},
    )


def _parse_rank_transition(value: Any) -> tuple[int, int, int | None]:
    if not isinstance(value, (tuple, list)) or len(value) != 4 or value[0] != 1:
        raise RuntimeError("unexpected Redis rank transition response")
    before, after = value[1:3]
    if type(before) is not int or type(after) is not int or min(before, after) < 0:
        raise RuntimeError("invalid Redis rank transition positions")
    raw = value[3]
    if isinstance(raw, bytes):
        raw = raw.decode('ascii')
    if not isinstance(raw, str) or (raw and (not raw.isascii() or not raw.isdecimal())):
        raise RuntimeError("invalid Redis displaced user ID")
    return before, after, int(raw) if raw else None


async def _sync_row(db: Any, redis: Any, user_id: int, mode: int, *,
                    capture_transition: bool = False) -> bool | tuple[int, int, int | None]:
    """Apply pending row, serialized across processes via InnoDB row lock."""
    _check_mode(mode)
    values = {"user_id": int(user_id), "mode": int(mode)}
    async with db.transaction():
        row = await db.fetch_one(
            "SELECT generation, applied_generation, last_country "
            "FROM vanilla_rank_sync WHERE userid=:user_id AND mode=:mode "
            "FOR UPDATE",
            values,
        )
        if row is None or int(row["generation"]) <= int(row["applied_generation"]):
            return False

        # The outbox row stays locked throughout the Redis write.  All score
        # writers must enqueue while their SQL transaction is in progress.
        info = await db.fetch_one(
            "SELECT st.pp AS pp, u.priv AS priv, u.country AS country "
            "FROM stats st JOIN users u ON u.id=st.id "
            "WHERE st.id=:user_id AND st.mode=:mode",
            values,
        )
        new_country = str(info["country"]).lower() if info is not None else "xx"
        old_country = str(row["last_country"]).lower() if row["last_country"] else new_country
        generation = int(row["generation"])
        pp = int(info["pp"]) if info is not None else 0
        allowed = bool(info is not None and (int(info["priv"]) & 1) and pp > 0)
        prefix = f"bancho:leaderboard:{mode}"

        # Bounded Redis wait prevents holding an InnoDB lock indefinitely.
        # Lua's monotonic generation guard protects from late/out-of-order
        # requests after a client-side cancellation/timeout.
        applied = await asyncio.wait_for(
            redis.eval(
                _SYNC_LUA, 5,
                f"bancho:vanilla-sync-gen:{mode}:{user_id}",
                prefix,
                f"{prefix}:{old_country}",
                f"{prefix}:{new_country}",
                f"bancho:vanilla-sync-country:{mode}:{user_id}",
                generation, str(user_id), pp, int(allowed),
                *(("1",) if capture_transition else ()),
            ),
            timeout=5.0,
        )
        transition = None
        if capture_transition and isinstance(applied, (tuple, list)):
            transition = _parse_rank_transition(applied)
        elif int(applied) != 1:
            raise RankSyncVersionConflict(
                "Redis generation ahead of MySQL; manual review required"
            )

        # Redis may have committed even if the SQL ACK fails; retries are safe.
        await db.execute(
            "UPDATE vanilla_rank_sync SET applied_generation=:generation, "
            "last_country=:country "
            "WHERE userid=:user_id AND mode=:mode",
            {**values, "generation": generation, "country": new_country},
        )
        return transition if transition is not None else True


async def sync_vanilla_rank(db: Any, redis: Any, user_id: int, mode: int) -> int | None:
    """Reconcile one user and return their current global rank, if present.

    An exception leaves the MySQL intent pending for the background retry.
    """
    await _sync_row(db, redis, user_id, mode)
    position = await redis.zrevrank(f"bancho:leaderboard:{int(mode)}", str(user_id))
    return int(position) + 1 if position is not None else 0


async def sync_vanilla_rank_with_transition(
    db: Any, redis: Any, user_id: int, mode: int,
) -> tuple[int, tuple[int, int, int | None] | None]:
    """Foreground only. Return a fresh transition after successful SQL ACK.

    If a worker already drained the row, or the observed position has since
    changed, suppress the optional notification. No historical notifications.
    """
    transition = await _sync_row(db, redis, user_id, mode, capture_transition=True)
    position = await redis.zrevrank(f"bancho:leaderboard:{int(mode)}", str(user_id))
    rank = int(position) + 1 if position is not None else 0
    if not isinstance(transition, tuple) or transition[1] != rank:
        return rank, None
    return rank, transition


async def drain_vanilla_rank_sync(db: Any, redis: Any, limit: int = 25) -> int:
    """One bounded batch; raise on failure so next housekeeping tick retries.

    Concurrent processes may see the same candidates but _sync_row locks each
    row, then rechecks the generation. Processing is idempotent.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit out of range")
    rows = await db.fetch_all(
        "SELECT userid, mode FROM vanilla_rank_sync "
        "WHERE generation > applied_generation AND mode IN (0,1,2,3) "
        "ORDER BY updated_at ASC, userid ASC, mode ASC LIMIT :limit",
        {"limit": limit},
    )
    done = 0
    for row in rows:
        done += int(await _sync_row(db, redis, int(row["userid"]), int(row["mode"])))
    return done


async def audit_rank_batch(
    db: Any, redis: Any, *, after_id: int = 0, after_mode: int = -1,
    limit: int = 50,
) -> tuple[int, int, int, bool]:
    """Read-only scan of MySQL + Redis; queue only mismatches for later retry.

    A periodic full pass catches Redis data loss after previously ACKed jobs,
    changes of country/privileges made outside vanilla score submission, and
    crashes during an external country or restriction update.

    Returns (new_after_id, new_after_mode, queued, exhausted).
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit out of range")
    rows = await db.fetch_all(
        "SELECT s.id AS user_id, s.mode AS mode, s.pp AS pp, "
        "u.country AS country, u.priv AS priv, o.last_country AS last_country "
        "FROM stats s JOIN users u ON u.id=s.id "
        "LEFT JOIN vanilla_rank_sync o ON o.userid=s.id AND o.mode=s.mode "
        "WHERE s.mode IN (0,1,2,3) "
        "AND (s.id > :after_id OR (s.id = :after_id AND s.mode > :after_mode)) "
        "ORDER BY s.id, s.mode LIMIT :limit",
        {"after_id": after_id, "after_mode": after_mode, "limit": limit},
    )
    queued = 0
    for row in rows:
        uid, mode = int(row["user_id"]), int(row["mode"])
        country = str(row["country"]).lower()
        previous_country = str(row["last_country"]).lower() if row["last_country"] else country
        pp = int(row["pp"])
        active = bool(int(row["priv"]) & 1) and pp > 0
        global_key = f"bancho:leaderboard:{mode}"
        current_key = f"{global_key}:{country}"
        previous_key = f"{global_key}:{previous_country}"
        # No mutation to Redis here: audit only enqueues a durable SQL intent.
        global_pp = await redis.zscore(global_key, str(uid))
        current_pp = await redis.zscore(current_key, str(uid))
        previous_pp = (
            await redis.zscore(previous_key, str(uid))
            if previous_key != current_key else None
        )
        expected = float(pp) if active else None
        if (global_pp != expected or current_pp != expected
                or previous_pp is not None):
            async with db.transaction():
                await enqueue_vanilla_rank_sync(db, uid, mode)
            queued += 1

    if not rows:
        return 0, -1, 0, True
    last_id, last_mode = int(rows[-1]["user_id"]), int(rows[-1]["mode"])
    return last_id, last_mode, queued, len(rows) < limit
